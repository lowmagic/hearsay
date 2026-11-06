#!/usr/bin/env bash
# TrueNAS SCALE: datasets on the fast pool and their backups, secrets,
# images, portal and capture containers, the reprocess and check kinds on
# corral's worker, and the hourly check and nightly pipeline.
# Run as root from the clone in /mnt/fast/repos/hearsay. Safe to re-run; also
# how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_root
COMPOSE=(docker compose -f "$REPO_DIR/install/compose.yaml")
CHILDREN=(config raw db audio labels transcripts imports stream records pcm)

say "Checking Docker"
if ! docker info >/dev/null 2>&1; then
    # Manual: picking the Apps pool is a storage decision for the operator.
    die "Docker isn't running. In the TrueNAS UI: Apps -> Configuration -> Choose Pool. Then re-run."
fi

say "Checking the repo is at $NAS_REPO_DIR"
# Compose and corral's kinds run from this clone.
[ "$REPO_DIR" = "$NAS_REPO_DIR" ] || die "this is $REPO_DIR. As admin (not root):
  git clone https://github.com/junglestyle/hearsay.git $NAS_REPO_DIR
then: sudo $NAS_REPO_DIR/install/nas.sh"
echo "ok"

say "Datasets under $NAS_POOL_DATASET"
# midclt (not raw zfs) so the TrueNAS UI knows about the ones made here.
if ! zfs list -H -o name "$NAS_POOL_DATASET" >/dev/null 2>&1; then
    midclt call pool.dataset.create "{\"name\": \"$NAS_POOL_DATASET\"}" >/dev/null
    echo "created: $NAS_POOL_DATASET"
fi
for ds in "${CHILDREN[@]}"; do
    dataset="$NAS_POOL_DATASET/$ds"
    if zfs list -H -o name "$dataset" >/dev/null 2>&1; then
        echo "exists: $dataset"
    else
        midclt call pool.dataset.create "{\"name\": \"$dataset\"}" >/dev/null
        echo "created: $dataset"
    fi
done
for ds in "${CHILDREN[@]}"; do
    [ -d "$NAS_ROOT/$ds" ] || die "$NAS_POOL_DATASET/$ds isn't mounted at $NAS_ROOT/$ds"
done

# Each dataset below guards itself; members of the apps group (the dev box's
# ssh login, consumers) need to get through this one.
chown root:root "$NAS_ROOT"
chmod 755 "$NAS_ROOT"
chown root:root "$NAS_CONFIG"
chmod 700 "$NAS_CONFIG"
chown "$APPS_UID:$APPS_GID" "$NAS_RAW"
chmod 750 "$NAS_RAW"
# What sealed conversations' audio produced (hearsay/records.py): raw's
# stand-in once their audio is gone, so guarded like it.
chown "$APPS_UID:$APPS_GID" "$NAS_RECORDS"
chmod 750 "$NAS_RECORDS"
# Parsed transcripts, assembled and decoded audio and the utterance stream:
# same sensitivity as raw. The apps group (consumers included) can read them.
for dir in "$NAS_DB" "$NAS_AUDIO" "$NAS_STREAM" "$NAS_PCM"; do
    chown "$APPS_UID:$APPS_GID" "$dir"
    chmod 750 "$dir"
done
# Written over ssh by members of the apps group: operator input (enrollment
# windows, labels, imported audio) and transcripts (install/gpu.sh).
# setgid keeps new files in the apps group, which the containers run as.
for dir in "$NAS_LABELS" "$NAS_TRANSCRIPTS" "$NAS_IMPORTS"; do
    chown "$APPS_UID:$APPS_GID" "$dir"
    chmod 2770 "$dir"
done
# The dev box leaves failure markers here, and reprocess deletes them. Made
# here, not by the dev box's ssh login (umask 022), so the containers can
# write in it.
mkdir -p "$NAS_TRANSCRIPTS/failed"
chown "$APPS_UID:$APPS_GID" "$NAS_TRANSCRIPTS/failed"
chmod 2770 "$NAS_TRANSCRIPTS/failed"

say "Backups of the audio datasets"
# Input audio isn't precious: what it gave is kept in records
# (hearsay/records.py), which also hold the dev box's transcripts once sealed,
# and the audio goes at retention. So these datasets have their own daily
# snapshots kept 7 days, replicated to the storage pool and kept there as
# long, and are left out of the NAS-wide tasks (snapshots of fast kept 2
# weeks, replicated to storage for 2 months), which keep records, the
# database, labels and the stream.
# The NAS-wide tasks aren't Hearsay's: it only adds its excludes to them.
# Through midclt, so it all shows in the TrueNAS UI.
AUDIO_DATASETS=""
for ds in raw audio imports pcm transcripts; do
    AUDIO_DATASETS+="$NAS_POOL_DATASET/$ds "
done
AUDIO_REPLICA="storage/hearsay-audio-replica"
export AUDIO_DATASETS AUDIO_REPLICA
# Each call takes a command: tasks, exclude, run, plan FILE, destroy FILE, verify.
backups() {
    python3 - "$@" <<'EOF'
import json
import os
import subprocess
import sys
import time
from datetime import datetime

AUDIO = os.environ["AUDIO_DATASETS"].split()
REPLICA = os.environ["AUDIO_REPLICA"]
# A naming schema of their own, so neither these tasks' retention nor the
# NAS-wide task's ever takes the other's snapshots.
SCHEMA = "hearsay-%Y-%m-%d_%H-%M"
SNAPSHOT_TASK = {"recursive": False, "exclude": [], "lifetime_value": 1, "lifetime_unit": "WEEK",
                 "naming_schema": SCHEMA, "allow_empty": True, "enabled": True,
                 "schedule": {"minute": "0", "hour": "4", "dom": "*", "month": "*", "dow": "*",
                              "begin": "00:00", "end": "23:59"}}


def call(method, *args):
    done = subprocess.run(["midclt", "call", method, *map(json.dumps, args)], stdout=subprocess.PIPE, text=True)
    if done.returncode:
        sys.exit(f"ERROR: midclt call {method} failed (above)")
    return done.stdout


def query(method):
    return json.loads(call(method))


def zfs_list(*args):
    """Rows of `zfs list` in bytes, or None if what's asked for doesn't exist."""
    done = subprocess.run(["zfs", "list", "-H", "-p", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True)
    return [line.split("\t") for line in done.stdout.splitlines()] if done.returncode == 0 else None


def snapshots(dataset):
    return zfs_list("-t", "snapshot", "-d", "1", "-o", "name,used", dataset) or []


def named(snapshot, schema):
    try:
        datetime.strptime(snapshot.split("@", 1)[1], schema)
    except ValueError:
        return False
    return True


def own_snapshots(dataset):
    return sorted(name for name, _ in snapshots(dataset) if named(name, SCHEMA))


def unreplicated(replication):
    """Audio datasets whose newest own snapshot isn't on the replica. A
    snapshot shows on the target only once it's fully received, so this also
    catches a replica dataset left behind by a replication cut short."""
    def newest(d):
        return own_snapshots(d)[-1].split("@", 1)[1] if own_snapshots(d) else None
    return [d for d in AUDIO
            if newest(d) is None
            or newest(d) not in {name.split("@", 1)[1] for name, _ in snapshots(target(replication, d))}]


def below(parent, dataset):
    return dataset.startswith(parent + "/")


def target(replication, dataset):
    """Where a replication task puts a source dataset, as zettarepl works it out."""
    base = os.path.commonpath(replication["source_datasets"])
    return os.path.join(replication["target_dataset"], os.path.relpath(dataset, base))


def size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def wide_snapshot_tasks():
    return [t for t in query("pool.snapshottask.query")
            if t["recursive"] and any(below(t["dataset"], d) for d in AUDIO)]


def wide_replications():
    return [r for r in query("replication.query")
            if r["direction"] == "PUSH" and r["recursive"]
            and any(below(s, d) for s in r["source_datasets"] for d in AUDIO)]


def missing_excludes(task, sources):
    return [d for d in AUDIO if any(below(s, d) for s in sources) and d not in task["exclude"]]


def own_snapshot_tasks():
    return {t["dataset"]: t for t in query("pool.snapshottask.query")
            if t["dataset"] in AUDIO and t["naming_schema"] == SCHEMA}


def own_replication():
    return next((r for r in query("replication.query") if r["target_dataset"] == REPLICA), None)


def replication_wanted(snapshot_tasks):
    return {"name": "hearsay audio", "direction": "PUSH", "transport": "LOCAL", "source_datasets": AUDIO,
            "target_dataset": REPLICA, "recursive": False, "exclude": [], "properties": True,
            "properties_exclude": ["mountpoint"], "periodic_snapshot_tasks": sorted(t["id"] for t in snapshot_tasks),
            "auto": True, "retention_policy": "SOURCE", "lifetime_value": None, "lifetime_unit": None,
            "readonly": "SET", "enabled": True}


def differs(have, want):
    if "periodic_snapshot_tasks" in want:
        have = dict(have, periodic_snapshot_tasks=sorted(t["id"] for t in have["periodic_snapshot_tasks"]))
    return [k for k, v in want.items() if have.get(k) != v]


def old_backups():
    """The NAS-wide tasks' copies of the audio datasets: snapshots on fast
    named by the NAS-wide snapshot task, and whole replicas on storage."""
    schemas = {t["naming_schema"] for t in wide_snapshot_tasks()}
    found = []
    for d in AUDIO:
        found += [("snapshot", name, int(used)) for name, used in snapshots(d)
                  if any(named(name, s) for s in schemas)]
    for r in wide_replications():
        if r["transport"] != "LOCAL":
            print(f"Not checked: replication {r['name']!r} sends them to another host.", file=sys.stderr)
            continue
        for d in AUDIO:
            replica = target(r, d)
            if any(below(s, d) for s in r["source_datasets"]) and (rows := zfs_list("-o", "name,used", replica)):
                found.append(("dataset", replica, int(rows[0][1])))
    return found


def tasks():
    have = own_snapshot_tasks()
    for d in AUDIO:
        task = have.get(d)
        if task is None:
            call("pool.snapshottask.create", {"dataset": d, **SNAPSHOT_TASK})
            print(f"created: snapshot task for {d}")
        elif wrong := differs(task, SNAPSHOT_TASK):
            call("pool.snapshottask.update", task["id"], SNAPSHOT_TASK)
            print(f"updated: snapshot task for {d} ({', '.join(wrong)})")
        else:
            print(f"exists: snapshot task for {d}")
    want = replication_wanted(own_snapshot_tasks().values())
    replication = own_replication()
    if replication is None:
        call("replication.create", want)
        print(f"created: replication to {REPLICA}")
    elif wrong := differs(replication, want):
        call("replication.update", replication["id"], want)
        print(f"updated: replication to {REPLICA} ({', '.join(wrong)})")
    else:
        print(f"exists: replication to {REPLICA}")


def exclude():
    snapshot_tasks, replications = wide_snapshot_tasks(), wide_replications()
    if not snapshot_tasks or not replications:
        # Manual: the NAS-wide backups are the operator's storage decision.
        sys.exit(f"ERROR: no recursive periodic snapshot task and replication cover {AUDIO[0].rsplit('/', 1)[0]}."
                 " Records, the database, labels and the stream are backed up only by them. Create them in the"
                 " TrueNAS UI (Data Protection), then re-run.")
    # Replications first: TrueNAS refuses to update one that sends a dataset
    # its bound snapshot task excludes.
    for r in replications:
        if missing := missing_excludes(r, r["source_datasets"]):
            call("replication.update", r["id"], {"exclude": r["exclude"] + missing})
            print(f"replication {r['name']!r} now excludes {', '.join(missing)}")
        else:
            print(f"replication {r['name']!r} excludes them")
    for t in snapshot_tasks:
        if missing := missing_excludes(t, [t["dataset"]]):
            # A task's retention only prunes datasets it covers, so without
            # fixate_removal_date the snapshots it already took of these
            # would be kept forever. With it they still go when they'd have
            # expired (unless destroyed below first).
            call("pool.snapshottask.update", t["id"], {"exclude": t["exclude"] + missing, "fixate_removal_date": True})
            print(f"snapshot task on {t['dataset']} now excludes {', '.join(missing)}")
        else:
            print(f"snapshot task on {t['dataset']} excludes them")


def run():
    """Take each dataset's first snapshot and replica now, so the new backups
    exist before the old ones are destroyed."""
    have = own_snapshot_tasks()
    # One task at a time: started together, TrueNAS can leave one PENDING
    # and never take its snapshot (seen on 25.10.7 with five at once).
    for d in AUDIO:
        if own_snapshots(d):
            continue
        call("pool.snapshottask.run", have[d]["id"])
        for _ in range(60):
            if own_snapshots(d):
                break
            time.sleep(1)
        else:
            sys.exit(f"ERROR: no {SCHEMA} snapshot of {d} after a minute."
                     " See Data Protection -> Periodic Snapshot Tasks in the TrueNAS UI.")
        print(f"snapshot: {own_snapshots(d)[-1]}")
    replication = own_replication()
    if not unreplicated(replication):
        print(f"snapshots exist and are replicated to {REPLICA}")
        return
    print(f"Replicating to {REPLICA}...")
    if subprocess.run(["midclt", "call", "-j", "-jp", "description", "replication.run", str(replication["id"])],
                      stdout=subprocess.DEVNULL).returncode:
        sys.exit(f"ERROR: replication to {REPLICA} failed (above)")
    if missing := unreplicated(replication):
        sys.exit(f"ERROR: the newest snapshot of {', '.join(missing)} isn't in {REPLICA} after replicating."
                 " See Data Protection -> Replication Tasks in the TrueNAS UI.")


def plan(path):
    found = old_backups()
    with open(path, "w") as f:
        f.writelines(f"{kind}\t{name}\n" for kind, name, _ in found)
    if not found:
        print("No old long backups of them left.")
        return
    print("Their copies in the NAS-wide backups would keep this audio up to 2 months past retention."
          " They now have their own backups, so these go:")
    for d in AUDIO:
        rows = [(name, used) for kind, name, used in found if kind == "snapshot" and name.startswith(d + "@")]
        if rows:
            [(held,)] = zfs_list("-o", "usedbysnapshots", d)
            print(f"\n  {len(rows)} snapshot(s) of {d} (all its snapshots together hold {size(held)}):")
            for name, used in rows:
                print(f"    {name}  ({size(used)} only in it)")
    replicas = [(name, used) for kind, name, used in found if kind == "dataset"]
    if replicas:
        print("\n  Replica datasets, with everything under them:")
        for name, used in replicas:
            under = [n for n, in zfs_list("-r", "-t", "all", "-o", "name", name) if n != name]
            snaps = sum("@" in n for n in under)
            print(f"    {name}  {size(used)}, {snaps} snapshot(s), {len(under) - snaps} child dataset(s)")
    print(f"\nIn all: {sum(k == 'snapshot' for k, _, _ in found)} snapshot(s) on fast and {len(replicas)} replica"
          f" dataset(s) ({size(sum(u for _, u in replicas))}).")


def destroy(path):
    with open(path) as f:
        planned = [line.rstrip("\n").split("\t") for line in f]
    # Only what was listed, and only if it's still what the listing meant.
    current = {name for _, name, _ in old_backups()}
    if any(name not in current for _, name in planned):
        sys.exit("ERROR: the old backups changed since they were listed. Re-run to list them again.")
    # And only while the new backups hold everything.
    if missing := unreplicated(own_replication()):
        sys.exit(f"ERROR: {', '.join(missing)} isn't replicated to {REPLICA} yet, so nothing is destroyed. Re-run.")
    for kind, name in planned:
        if kind == "snapshot":
            call("pool.snapshot.delete", name)
        else:
            # Deleting a dataset also deletes tasks whose own datasets are
            # inside it (TrueNAS's attachments); no task's are inside a replica.
            call("pool.dataset.delete", name, {"recursive": True})
        print(f"destroyed: {name}")


def verify():
    problems = []
    for t in wide_snapshot_tasks():
        problems += [f"the snapshot task on {t['dataset']} doesn't exclude {d}"
                     for d in missing_excludes(t, [t["dataset"]])]
    for r in wide_replications():
        problems += [f"replication {r['name']!r} doesn't exclude {d}"
                     for d in missing_excludes(r, r["source_datasets"])]
    have = own_snapshot_tasks()
    for d in AUDIO:
        if d not in have or differs(have[d], SNAPSHOT_TASK):
            problems.append(f"{d} has no snapshot task as set here")
        elif not own_snapshots(d):
            problems.append(f"{d} has no {SCHEMA} snapshot")
    replication = own_replication()
    if replication is None or differs(replication, replication_wanted(have.values())):
        problems.append(f"no replication to {REPLICA} as set here")
    else:
        problems += [f"the newest {SCHEMA} snapshot of {d} isn't in {target(replication, d)}"
                     for d in unreplicated(replication)]
    problems += [f"{name} is still there" for _, name, _ in old_backups()]
    if problems:
        sys.exit("ERROR: the audio datasets' backups aren't as they should be:\n  " + "\n  ".join(problems))
    for d in AUDIO:
        print(f"{d}: snapshot daily at 04:00, kept 1 week, newest {own_snapshots(d)[-1].split('@')[1]}")
    print(f"replicated by {replication['name']!r} to {REPLICA}, kept as long as on fast")
    print("left out of the NAS-wide snapshot task(s) on "
          + ", ".join(t["dataset"] for t in wide_snapshot_tasks()) + " and replication(s) "
          + ", ".join(repr(r["name"]) for r in wide_replications()))
    print("TrueNAS UI: Data Protection -> Periodic Snapshot Tasks and Replication Tasks;"
          " each dataset's snapshots under Datasets -> (dataset) -> Data Protection -> Manage Snapshots.")


command, *args = sys.argv[1:]
{"tasks": tasks, "exclude": exclude, "run": run, "plan": plan, "destroy": destroy, "verify": verify}[command](*args)
EOF
}
backups tasks
backups exclude
backups run
# For moving the audio datasets to their own backups (the NAS): the NAS-wide
# tasks' copies of them. Delete this, and plan, destroy and old_backups above
# (and its check in verify), once nas.sh has run there.
# Manual: it destroys backups, so the operator reads exactly what goes first.
plan="$(mktemp)"
trap 'rm -f "$plan"' EXIT
backups plan "$plan"
if [ -s "$plan" ]; then
    echo
    confirm "Destroy exactly these? Ctrl-C keeps them and stops here; re-running asks again."
    backups destroy "$plan"
fi
rm -f "$plan"
backups verify

say "Idea Machine's vocab folder $IDEAMACHINE_VOCAB_DIR"
# The portal mounts it read-only. Never created here: Idea Machine owns it,
# and a directory made before its dataset would block the dataset's mount.
if [ -d "$IDEAMACHINE_VOCAB_DIR" ]; then
    COMPOSE+=(-f "$REPO_DIR/install/compose.vocab.yaml")
    echo "mounted in the portal"
else
    echo "WARNING: missing, so the portal starts without it (the Words page shows no"
    echo "words from Lattice). Install Idea Machine, then re-run this script."
fi

say "Portal login in $NAS_PORTAL_ENV"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER hearsay
env_ensure "$NAS_PORTAL_ENV" HEARSAY_PORTAL_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=')"
env_ensure "$NAS_PORTAL_ENV" HEARSAY_SESSION_KEY "$(openssl rand -hex 32)"

say "Capture token in $NAS_CAPTURE_ENV"
env_ensure "$NAS_CAPTURE_ENV" HEARSAY_CAPTURE_TOKEN "$(openssl rand -hex 32)"

say "Portal addresses (LAN and tailnet only)"
# Bound to these two addresses, never all interfaces: the NAS also has a
# public IPv6 address, and non-owner audio must stay on the LAN and tailnet.
HEARSAY_LAN_IP="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
HEARSAY_TAILNET_IP="$(ip -4 -o addr show tailscale0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
[ -n "$HEARSAY_LAN_IP" ] || die "couldn't find the LAN address"
# Manual: joining the tailnet needs the operator's Tailscale account.
[ -n "$HEARSAY_TAILNET_IP" ] \
    || die "no tailscale0 address. Install and log in to the Tailscale app (TrueNAS UI: Apps), then re-run."
echo "LAN $HEARSAY_LAN_IP, tailnet $HEARSAY_TAILNET_IP"
export HEARSAY_LAN_IP HEARSAY_TAILNET_IP

say "Building images"
# Baked into the worker image and recorded by each reprocess run, so a stale
# image is visible. safe.directory: root runs git in a clone owned by admin.
HEARSAY_COMMIT="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" describe --always --dirty)"
export HEARSAY_COMMIT
# --profile tools also builds the one-shot worker (reprocess) and check images.
"${COMPOSE[@]}" --profile tools build

say "Corral (github.com/junglestyle/corral)"
# Reprocess and the check run as kinds on corral's NAS worker, and the capture
# receiver enqueues a check after each upload. Corral is set up from the infra repo.
systemctl is-active -q corral-worker && [ -f "$NAS_CORRAL_DIR/worker.toml" ] \
    || die "no corral worker running from $NAS_CORRAL_DIR. Set up corral's server and NAS worker from the infra repo,
then re-run."
grep -q '"nas"' "$NAS_CORRAL_DIR/worker.toml" \
    || die "the corral worker in $NAS_CORRAL_DIR/worker.toml doesn't offer \"nas\"; add it to offers, then re-run"
corral_token="$(env_get "$NAS_CORRAL_DIR/env" CORRAL_TOKEN)"
[ -n "$corral_token" ] || die "no CORRAL_TOKEN in $NAS_CORRAL_DIR/env"
# The tailnet address, because the capture container reaches the host there.
corral_url="http://$HEARSAY_TAILNET_IP:$NAS_CORRAL_PORT"
curl -fsS -o /dev/null -H "Authorization: Bearer $corral_token" "$corral_url/workers" \
    || die "corral's server doesn't answer at $corral_url with the token in $NAS_CORRAL_DIR/env"
# Held jobs (wait_seconds, quiet_seconds) and release came in together: an
# older server would take the fold's and portal edits' specs but run them at once.
openapi="$(curl -fsS "$corral_url/openapi.json")" || die "corral's server doesn't answer at $corral_url"
grep -qF '/jobs/{job_id}/release' <<<"$openapi" \
    || die "corral's server at $corral_url has no held jobs. Upgrade corral from the infra repo, then re-run."
# Rewritten every run, so a new token reaches the receiver, the portal and the check.
(umask 077; printf 'CORRAL_URL=%s\nCORRAL_TOKEN=%s\n' "$corral_url" "$corral_token" > "$NAS_CORRAL_ENV")
mkdir -p "$NAS_CORRAL_DIR/kinds.d"
# Blocking flock, so a reprocess run by hand delays the job instead of failing
# it. The check takes reprocess's own lock inside its container (hearsay/check.py).
# systemd-cat keeps their output under journalctl -t hearsay-reprocess and -t hearsay-check.
if write_if_changed "$NAS_CORRAL_DIR/kinds.d/hearsay.toml" <<EOF
# Written by hearsay's install/nas.sh on every run.
[kinds."hearsay.reprocess"]
argv = ["systemd-cat", "-t", "hearsay-reprocess", "flock", "/run/hearsay-reprocess.lock",
        "docker", "compose", "-f", "$REPO_DIR/install/compose.yaml", "run", "--rm", "-T", "reprocess"]

[kinds."hearsay.check"]
argv = ["systemd-cat", "-t", "hearsay-check",
        "docker", "compose", "-f", "$REPO_DIR/install/compose.yaml", "run", "--rm", "-T", "ended"]
EOF
then
    systemctl restart corral-worker
    echo "kinds updated, worker restarted"
fi
echo "server $corral_url, kinds in $NAS_CORRAL_DIR/kinds.d/hearsay.toml"

say "Starting portal and capture receiver"
"${COMPOSE[@]}" up -d

say "Verifying the portal answers on both addresses"
for ip in "$HEARSAY_LAN_IP" "$HEARSAY_TAILNET_IP"; do
    wait_for_status GET "http://$ip:$NAS_PORTAL_PORT/login" 200 30
done

say "Verifying the capture receiver answers on the tailnet only"
wait_for_401 "http://$HEARSAY_TAILNET_IP:$NAS_CAPTURE_PORT/capture" 30
if curl -s -o /dev/null --max-time 3 "http://$HEARSAY_LAN_IP:$NAS_CAPTURE_PORT/capture"; then
    die "the capture receiver answers on the LAN address; it must be tailnet only"
fi

say "Decoded audio moves to $NAS_PCM"
# For moving decoded audio out of db into its own dataset (the NAS). It's a
# cache the next reprocess rebuilds (only unsealed uploads are decoded), so
# it's removed, not moved. After the images are built, so no run of the old
# code makes it again, and under the lock, so no run is reading it. Delete
# this once nas.sh has run there.
for old in "$NAS_DB/capture-pcm" "$NAS_DB/imports-pcm"; do
    if [ -d "$old" ]; then
        flock /run/hearsay-reprocess.lock rm -rf "$old"
        echo "removed: $old"
    fi
done

say "Reprocessing now"
# Also the check that every captured payload parses: reprocess replaces the
# database only if all of them do. (The real-capture test in `check` repeats
# the same work uncached, so it's run by hand, not here.)
# -T and /dev/null: never read the terminal, so typing ahead isn't swallowed.
# Waits for a run in progress. Its output is in the journal too:
# journalctl -t hearsay-reprocess
flock /run/hearsay-reprocess.lock "${COMPOSE[@]}" run --rm -T reprocess </dev/null

say "Hourly check and nightly pipeline (TrueNAS cron jobs)"
# Uploads enqueue a check themselves (hearsay/check.py); the hourly one
# catches any that couldn't reach corral, and a recording stopped soon after
# its last speech. The nightly pipeline does what's due by time alone (a hold
# ending, sealing, retention), with nothing recorded or edited, and transcribes
# anything a check missed. Keys coalesce, so neither doubles up. Registered
# through midclt so they show in the TrueNAS UI (System -> Advanced -> Cron
# Jobs) and survive updates. Output goes to the journal, not a file on either
# pool. The middleware appends its own "> /dev/null 2> /dev/null" to the
# command, so the pipe sits inside an outer subshell.
# Takes the description, minute and hour, and what to enqueue (hearsay/enqueue.py).
cron_job() {
    local description="$1" minute="$2" hour="$3" what="$4" payload id
    payload="$(python3 -c '
import json, sys
description, minute, hour, command = sys.argv[1:]
print(json.dumps({"user": "root", "command": command, "description": description,
                  "schedule": {"minute": minute, "hour": hour, "dom": "*", "month": "*", "dow": "*"},
                  "enabled": True, "stdout": True, "stderr": True}))' "$description" "$minute" "$hour" \
        "( docker exec hearsay-capture python -m hearsay.enqueue $what 2>&1 | logger -t hearsay-enqueue )")"
    id="$(midclt call cronjob.query "[[\"description\", \"=\", \"$description\"]]" \
        | python3 -c 'import json, sys; jobs = json.load(sys.stdin); print(jobs[0]["id"] if jobs else "")')"
    if [ -z "$id" ]; then
        midclt call cronjob.create "$payload" >/dev/null
        echo "created: $description, enqueues $what (cron minute $minute, hour $hour)"
    else
        midclt call cronjob.update "$id" "$payload" >/dev/null
        echo "up to date: $description, enqueues $what (cron minute $minute, hour $hour; job $id)"
    fi
}
# For the switch to corral (the NAS): corral's worker runs reprocess now, so
# the hourly reprocess cron job goes. Delete this once nas.sh has run with
# corral up.
cron_id="$(midclt call cronjob.query '[["description", "=", "hearsay reprocess"]]' \
    | python3 -c 'import json, sys; jobs = json.load(sys.stdin); print(jobs[0]["id"] if jobs else "")')"
[ -z "$cron_id" ] || { midclt call cronjob.delete "$cron_id" >/dev/null; echo "removed: hourly reprocess"; }
cron_job "hearsay enqueue" 30 "*" check
# Before the audio datasets' snapshots at 04:00, so they don't keep what retention deletes.
cron_job "hearsay nightly" 0 3 chain
docker exec hearsay-capture python -m hearsay.enqueue check \
    || die "the capture container can't enqueue on corral at $corral_url"
echo "logs: journalctl -t hearsay-enqueue, journalctl -t hearsay-check, journalctl -t hearsay-reprocess"

say "Verifying uploads land in $NAS_RAW"
# Manual: only the operator's phone (or Mac) can send a real upload.
marker="$(mktemp)"
trap 'rm -f "$marker"' EXIT
echo "Record for a minute with the iPhone app (or the Mac's recorder). Waiting up to 5 minutes..."
for _ in $(seq 300); do
    [ -z "$(find "$NAS_RAW/capture" -name '*.json' -newer "$marker" -print -quit 2>/dev/null)" ] || break
    sleep 1
done
[ -n "$(find "$NAS_RAW/capture" -name '*.json' -newer "$marker" -print -quit 2>/dev/null)" ] \
    || die "no upload arrived in $NAS_RAW/capture. Check the app's capture URL and token, then re-run."
echo "received an upload in $NAS_RAW/capture"

say "Hearsay is running from $NAS_ROOT"
# Manual: the password manager is on the operator's devices.
cat <<EOF

Portal for tagging who spoke (save it in your password manager):
  http://$HEARSAY_TAILNET_IP:$NAS_PORTAL_PORT   (tailnet: encrypted; use this from your phone)
  http://$HEARSAY_LAN_IP:$NAS_PORTAL_PORT   (LAN: unencrypted Wi-Fi)
  username: $(env_get "$NAS_PORTAL_ENV" HEARSAY_PORTAL_USER)
  password: sudo sed -n 's/^HEARSAY_PORTAL_PASSWORD=//p' $NAS_PORTAL_ENV

Capture receiver, for the iPhone app (install/ios.sh on the Mac):
  URL:   http://$HEARSAY_TAILNET_IP:$NAS_CAPTURE_PORT/capture
  token: sudo sed -n 's/^HEARSAY_CAPTURE_TOKEN=//p' $NAS_CAPTURE_ENV
EOF

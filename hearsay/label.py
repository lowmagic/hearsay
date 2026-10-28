"""Label turns by ear, and report how well the thresholds match the labels.

Correcting single turns happens in the web portal (hearsay/portal.py).

Runs on the operator's own machine, not in the container: it plays audio
locally and reaches the NAS over `ssh nas`. Standard library only, so any
checkout works (`python3 -m hearsay.label`), or install it as a command with
`uv tool install git+ssh://git@github.com/junglestyle/hearsay.git` and run
`hearsay-label`. Labels are the operator's durable input, in the labels
dir on the NAS, never rebuilt: labels are appended, and enrollment.json is
rewritten only to add a window.

    python -m hearsay.label enroll START END   # add an enrollment window
    python -m hearsay.label import FILE START  # add audio the live stream missed
    python -m hearsay.label                    # label turns me / not me, blind
    python -m hearsay.label review             # re-hear labels the model disagrees with
    python -m hearsay.label check              # label turns labeled by diarization, blind
    python -m hearsay.label report             # precision/recall

START and END are ISO-8601 times, e.g. 2026-09-27T10:05 or "2026-09-27 10:05";
without a UTC offset they are this machine's local time. For an import, START
is when the recording began, as the Omi app shows it. Reprocess afterwards.
"""

import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hearsay.speakers import DIARIZATION_AGREEMENT, NOT_OWNER_THRESHOLD, OWNER_THRESHOLD
from hearsay.turns import wall

NAS = "nas"
DB = "/mnt/fast/hearsay/db/hearsay.sqlite"
AUDIO_DIR = "/mnt/fast/hearsay/audio"
LABELS = "/mnt/fast/hearsay/labels/labels.jsonl"
ENROLLMENT = "/mnt/fast/hearsay/labels/enrollment.json"
IMPORTS = "/mnt/fast/hearsay/imports"
ANSWERS = {"m": "owner", "n": "not_owner", "u": "unsure"}

CANDIDATES_SQL = """
SELECT t.turn_id, t.conversation_id, t.start, t.end, ts.owner_similarity, ca.wav_file, ca.zero_at
FROM turn_speakers ts
JOIN turns t ON t.turn_id = ts.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE ts.embedding IS NOT NULL
"""

# Turns that took their label from their diarized speaker (hearsay/speakers.py).
INHERITED_SQL = """
SELECT t.turn_id, t.conversation_id, t.start, t.end, t.text, ts.label, ca.wav_file, ca.zero_at
FROM turn_speakers ts
JOIN turns t ON t.turn_id = ts.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE ts.basis = 'diarization'
"""

# Speech time by how its speaker was decided.
COVERAGE_SQL = """
SELECT coalesce(ts.basis, 'unlabeled') AS basis, sum(t.end - t.start) AS seconds
FROM turn_speakers ts JOIN turns t ON t.turn_id = ts.turn_id GROUP BY 1
"""

# The operator's labels as reprocess matched them to turns.
LABELS_SQL = "SELECT turn_id, value FROM operator_input WHERE kind = 'label'"

# Runs on the NAS: writes one turn of a conversation WAV to stdout as WAV.
SLICE_SCRIPT = """
import sys, wave
path, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
with wave.open(path) as src:
    rate = src.getframerate()
    src.setpos(max(0, int(start * rate)))
    frames = src.readframes(int((end - start) * rate))
out = wave.open(sys.stdout.buffer, "wb")
out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate)
out.writeframes(frames); out.close()
"""


def ssh(command: list[str], stdin: bytes = b"") -> bytes:
    return subprocess.run(["ssh", NAS, *command], input=stdin, capture_output=True, check=True).stdout


def query(sql: str) -> list[dict]:
    out = ssh(["sqlite3", "-json", DB, f'"{" ".join(sql.split())}"'])
    return json.loads(out or b"[]")


def candidates() -> list[dict]:
    return query(CANDIDATES_SQL)


def existing_labels() -> dict[str, str]:
    """turn_id -> label, as of the last reprocess."""
    return {row["turn_id"]: row["value"] for row in query(LABELS_SQL)}


def play(turn: dict) -> None:
    wav = ssh(["python3", "-", f"{AUDIO_DIR}/{turn['wav_file']}", str(turn["start"]), str(turn["end"])],
              SLICE_SCRIPT.encode())
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav)
        f.flush()
        # paplay on Linux (PulseAudio/PipeWire), afplay on macOS.
        player = shutil.which("paplay") or shutil.which("afplay")
        if not player:
            sys.exit("no audio player found: install PulseAudio/PipeWire (paplay) or use macOS (afplay)")
        subprocess.run([player, f.name], check=True)


def pick(unlabeled: list[dict]) -> dict:
    # Spread picks across the similarity range, so both ends and the
    # ambiguous middle get labels. Before enrollment there are no scores.
    scored = [s for s in unlabeled if s["owner_similarity"] is not None]
    if not scored:
        return random.choice(unlabeled)
    low = min(s["owner_similarity"] for s in scored)
    high = max(s["owner_similarity"] for s in scored)
    target = random.uniform(low, high)
    return min(scored, key=lambda s: abs(s["owner_similarity"] - target))


def ask(turn: dict) -> str | None:
    """Play a turn and return the operator's label, or None to stop."""
    # The transcript shows which words the turn covers; it says nothing of
    # whose voice the model heard, so labeling stays blind.
    text = f"  {turn['text'].strip()!r}" if turn.get("text") else ""
    print(f"\n{turn['end'] - turn['start']:.1f} s{text}")
    play(turn)
    answer = ""
    while answer not in ANSWERS and answer != "s":
        answer = input("> ").strip().lower()
        if answer == "q":
            return None
        if answer == "r":
            play(turn)
    return "skip" if answer == "s" else ANSWERS[answer]


def save_label(turn: dict, label: str) -> None:
    # Absolute times, so the label survives new transcripts and boundaries;
    # the turn id only for reference.
    record = {
        "at": [wall(turn["zero_at"], turn["start"], turn["end"])],
        "turn_ids": [turn["turn_id"]],
        "label": label,
        "labeled_at": datetime.now(timezone.utc).isoformat(),
    }
    ssh(["sh", "-c", f"'cat >> {LABELS}'"], (json.dumps(record) + "\n").encode())


def review_loop() -> None:
    """Re-hear labeled turns the model disagrees with, most confident disagreement first.

    Either the label or the model is wrong; a fresh listen settles which, and
    a new answer replaces the old one (the latest label for a turn wins).
    """
    labeled = existing_labels()
    disagreements = []
    for turn in candidates():
        label, sim = labeled.get(turn["turn_id"]), turn["owner_similarity"]
        if sim is None:
            continue
        if label == "owner" and sim < OWNER_THRESHOLD:
            disagreements.append((OWNER_THRESHOLD - sim, turn))
        elif label == "not_owner" and sim > NOT_OWNER_THRESHOLD:
            disagreements.append((sim - NOT_OWNER_THRESHOLD, turn))
    disagreements.sort(key=lambda d: -d[0])
    print(f"{len(disagreements)} labels the model disagrees with. Answer fresh; your old answer isn't shown.")
    print("m = me, n = not me, u = unsure, s = keep my old answer, r = replay, q = quit")
    changed = 0
    for _, turn in disagreements:
        answer = ask(turn)
        if answer is None:
            break
        if answer != "skip":
            save_label(turn, answer)
            changed += answer != labeled[turn["turn_id"]]
    print(f"\n{changed} label(s) changed. Reprocess on the NAS, then run `report`.")


def label_loop() -> None:
    labeled = existing_labels()
    unlabeled = [s for s in candidates() if s["turn_id"] not in labeled]
    print(f"{len(labeled)} labeled, {len(unlabeled)} to go.")
    print("m = me, n = not me, u = unsure, s = skip, r = replay, q = quit")
    while unlabeled:
        turn = pick(unlabeled)
        answer = ask(turn)
        if answer is None:
            return
        if answer != "skip":
            save_label(turn, answer)
        unlabeled.remove(turn)


def check_loop() -> None:
    """Label turns that inherited their label, in random order, to measure
    how often inheriting is right. The inherited label isn't shown."""
    labeled = existing_labels()
    unchecked = [t for t in query(INHERITED_SQL) if t["turn_id"] not in labeled]
    random.shuffle(unchecked)
    print(f"{len(unchecked)} turns labeled by diarization and not yet by ear.")
    print("m = me, n = not me, u = unsure, s = skip, r = replay, q = quit")
    for turn in unchecked:
        answer = ask(turn)
        if answer is None:
            break
        if answer != "skip":
            save_label(turn, answer)
    print("\nReprocess on the NAS, then run `report`.")


def enroll(start: str, end: str) -> None:
    window = {
        "start": datetime.fromisoformat(start).astimezone(timezone.utc).isoformat(),
        "end": datetime.fromisoformat(end).astimezone(timezone.utc).isoformat(),
    }
    if window["end"] <= window["start"]:
        sys.exit("END must be after START")
    # Missing means none yet; any other failure stops, since the file is rewritten below.
    windows = json.loads(ssh(["sh", "-c", f"'[ ! -e {ENROLLMENT} ] || cat {ENROLLMENT}'"]) or b"[]")
    if window in windows:
        print("Already enrolled.")
        return
    windows.append(window)
    ssh(["sh", "-c", f"'cat > {ENROLLMENT}.tmp && mv {ENROLLMENT}.tmp {ENROLLMENT}'"],
        json.dumps(windows, indent=2).encode())
    print(f"Enrolled {window['start']} to {window['end']} ({len(windows)} window(s)). Now reprocess on the NAS.")


def import_audio(file: str, start: str) -> None:
    """Copy an audio file, unchanged, into the NAS imports dir with its start time."""
    path = Path(file).expanduser()
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    began = datetime.fromisoformat(start).astimezone(timezone.utc)
    # No sidecars yet, or none matching (grep's 1), means not imported; any other failure stops.
    found = ssh(["sh", "-c", f"'set -- {IMPORTS}/*.json; [ ! -e \"$1\" ] || grep -l {sha} \"$@\" || [ $? -eq 1 ]'"])
    if found.strip():
        print(f"{path.name} is already imported.")
        return
    name = f"{began:%Y%m%dT%H%M%SZ}-{sha[:12]}{path.suffix.lower()}"
    ssh(["sh", "-c", f"'cat > {IMPORTS}/{name}.tmp && mv {IMPORTS}/{name}.tmp {IMPORTS}/{name}'"], data)
    # The sidecar goes last: its presence means the file is complete.
    sidecar = {
        "file": name,
        "start": began.isoformat(),
        "original_name": path.name,
        "sha256": sha,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    target = f"{IMPORTS}/{name}.json"
    ssh(["sh", "-c", f"'cat > {target}.tmp && mv {target}.tmp {target}'"], json.dumps(sidecar, indent=2).encode())
    local = began.astimezone()
    print(f"Imported {path.name} as starting {local:%Y-%m-%d %H:%M %Z} ({began:%H:%M} UTC). Reprocess on the NAS.")


def report() -> None:
    built = ssh(["sqlite3", DB], b"SELECT key || '=' || value FROM reprocess_info;").decode().split()
    print("database built by: " + ", ".join(built))
    enrolled = ssh(["sqlite3", DB, '"SELECT start, end, round(coverage, 2), pieces FROM owner_enrollment"'])
    print("enrollment (start | end | coverage | 3 s pieces used):")
    print(enrolled.decode() or "  none; run `python -m hearsay.label enroll` and reprocess\n")
    labeled = existing_labels()
    rows = [(s["owner_similarity"], labeled[s["turn_id"]]) for s in candidates()
            if s["turn_id"] in labeled and s["owner_similarity"] is not None]
    rows = [(sim, label) for sim, label in rows if label != "unsure"]
    owners = sum(1 for _, label in rows if label == "owner")
    print(f"{len(rows)} labeled turns with scores ({owners} owner, {len(rows) - owners} not owner)")
    print(f"thresholds in this checkout: owner >= {OWNER_THRESHOLD}, not_owner <= {NOT_OWNER_THRESHOLD}\n")
    print("owner if similarity >= t        not_owner if similarity <= t")
    print("   t  labeled  precision recall     t  labeled  precision recall")
    for i in range(11):
        t_owner, t_not = 0.3 + i * 0.05, 0.1 + i * 0.04
        print(f"{line(rows, 'owner', lambda sim: sim >= t_owner, t_owner)}   "
              f"{line(rows, 'not_owner', lambda sim: sim <= t_not, t_not)}")
    inheritance_report(labeled)


def inheritance_report(labeled: dict[str, str]) -> None:
    seconds = {row["basis"]: row["seconds"] or 0 for row in query(COVERAGE_SQL)}
    inherited = query(INHERITED_SQL)
    total = sum(seconds.values()) or 1
    print(f"\nspeech time by basis: " + ", ".join(f"{basis} {s / total:.0%}" for basis, s in sorted(seconds.items())))
    checked = [(t["label"], labeled[t["turn_id"]], t["end"] - t["start"]) for t in inherited
               if labeled.get(t["turn_id"]) in ("owner", "not_owner")]
    if not checked:
        print(f"labeled by diarization: {len(inherited)} turns, none checked by ear yet (run `check`)")
        return
    right = [seconds for got, heard, seconds in checked if got == heard]
    # By speech time too: the misses are mostly half-second "yeah"s.
    by_time = sum(right) / (sum(seconds for _, _, seconds in checked) or 1)
    print(f"labeled by diarization (agreement {DIARIZATION_AGREEMENT}): {len(inherited)} turns, "
          f"{len(checked)} checked by ear, precision {len(right) / len(checked):.2f} ({by_time:.2f} by speech time)")
    for label in ("owner", "not_owner"):
        mine = [heard for got, heard, _ in checked if got == label]
        if mine:
            print(f"  {label}: {sum(1 for h in mine if h == label)}/{len(mine)} right")


def line(rows, label, predicate, threshold) -> str:
    predicted = [actual for sim, actual in rows if predicate(sim)]
    correct = sum(1 for actual in predicted if actual == label)
    total = sum(1 for _, actual in rows if actual == label)
    precision = f"{correct / len(predicted):.2f}" if predicted else "  - "
    recall = f"{correct / total:.2f}" if total else "  - "
    return f"{threshold:.2f}  {len(predicted):7d}  {precision:>9} {recall:>6}"


def main() -> None:
    if sys.argv[1:2] == ["enroll"] and len(sys.argv) == 4:
        enroll(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ["import"] and len(sys.argv) == 4:
        import_audio(sys.argv[2], sys.argv[3])
    elif sys.argv[1:] == ["report"]:
        report()
    elif sys.argv[1:] == ["review"]:
        review_loop()
    elif sys.argv[1:] == ["check"]:
        check_loop()
    elif not sys.argv[1:]:
        label_loop()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

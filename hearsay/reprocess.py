"""Rebuild everything derived from raw: database, conversations, conversation
audio, turns, speaker labels, and the utterance stream.

Turns come from transcripts the dev box makes of each WAV (hearsay/transcribe.py),
so new audio takes a reprocess, a transcription run, then another reprocess.
On the NAS corral's worker runs it when an upload enqueues the pipeline, and
an hourly enqueue (install/nas.sh) catches uploads that couldn't.

Every run builds a complete new database from raw in a scratch directory
(a tmpfs in the container), then brings the real one up to date in a single
transaction that changes only the rows that differ. WAVs that changed are
staged beside the real ones and moved into place only after that, and WAVs
and decoded audio it no longer refers to are deleted only then too. So the
portal and the label tool always read a complete database, a failed run
leaves the previous database and audio in place, and a run with nothing new
writes almost nothing to disk (the NAS keeps snapshots, so every rewritten
byte is kept many times). A schema change replaces the changed table inside
the same transaction. Deleting the database is always safe: the next run
fills a new one from raw and the records.

Conversations whose hold has ended (hearsay/hold.py) are sealed at the end of
a run (hearsay/records.py), and later runs take them from their records
without reading their audio, so a run with nothing new reads only the hold's.
Then what's due is deleted or cut from sealed audio: thrown-out stretches,
and audio past retention (hearsay/retention.py). Slow steps (voice
detection, speaker embeddings, the audio models) are cached by audio
content next to the database, and what no run has used for HOLD is dropped.
A run waits for any other to finish (one corral starts and one started by
hand would otherwise overlap and break each other).
"""

import fcntl
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from hearsay import conversations, retention, speakers
from hearsay.assemble import assemble, promote_wavs, remove_stale_wavs
from hearsay.cache import Cache
from hearsay.capture import load_captures, remove_stale_capture_pcm
from hearsay.conversations import find_conversations
from hearsay.forget import forget
from hearsay.hold import HOLD, record_releases
from hearsay.imports import load_imports, remove_stale_import_pcm
from hearsay.media import score_media
from hearsay.music import score_music
from hearsay.parse import ParseFailed, rebuild
from hearsay.affect import score_affect
from hearsay.delivery import measure_delivery
from hearsay.places import find_places
from hearsay.records import late, restore, seal, sealed_payloads, unseal
from hearsay.speakers import label_speakers
from hearsay.stream import write_stream
from hearsay.turns import build_turns, record_operator_input


def report(title: str, counts: dict) -> None:
    print(title)
    for name, n in counts.items():
        print(f"  {name}: {n}")


DIRS = ("raw_dir", "db", "audio_dir", "labels_dir", "model_dir", "transcripts_dir", "imports_dir", "stream_dir",
        "affect_model_dir", "music_model_dir", "records_dir")


# Decoded audio is derived, so it's cached next to the database.
def imports_pcm(env: dict[str, Path]) -> Path:
    return env["db"].parent / "imports-pcm"


def capture_pcm(env: dict[str, Path]) -> Path:
    return env["db"].parent / "capture-pcm"


@contextmanager
def lock(db_path: Path):
    """One run at a time: one corral starts and one started by hand would otherwise break each other."""
    with open(db_path.with_name("reprocess.lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def build(env: dict[str, Path], staging: Path, staged_audio: Path, records_dir: Path | None,
          unseal_late: bool = False) -> None:
    """Build a complete database at staging, and the WAVs that changed at
    staged_audio: from records behind the seal line (hearsay/records.py), or
    with records_dir None all from raw."""
    cache = Cache(env["db"].parent / "cache.sqlite")
    try:
        sealed, imports = sealed_payloads(records_dir)
        report(f"Rebuilt from {env['raw_dir']}:", rebuild(env["raw_dir"], staging, sealed))
        if unseal_late and records_dir:
            when, ignored = late(staging, records_dir, env["imports_dir"], env["labels_dir"])
            if ignored:
                report("Arrived late behind the retention line, so left out:", ignored)
            if when is not None:
                # Audio or an enrollment window that a build from records would miss: redo from just before it.
                report("Arrived late at the seal line; unsealed:", unseal(records_dir, when))
                sealed, imports = sealed_payloads(records_dir)
                report(f"Rebuilt from {env['raw_dir']}:", rebuild(env["raw_dir"], staging, sealed))
        report("Sealed conversations, from their records:", restore(staging, records_dir))
        # Decoded once and cached; the cache is pruned in run, once this database is in place.
        report(f"Imported audio from {env['imports_dir']}:",
               load_imports(env["imports_dir"], staging, imports_pcm(env), imports))
        report("Audio from the recorder:", load_captures(env["raw_dir"], staging, capture_pcm(env)))
        report("Conversations found in the audio stream:", find_conversations(env["raw_dir"], staging, cache))
        report("Where conversations happened:", find_places(staging, env["labels_dir"]))
        report(f"Assembled conversation audio in {staged_audio}:",
               assemble(env["raw_dir"], staging, env["audio_dir"], staged_audio))
        report(f"Turns from transcripts in {env['transcripts_dir']}:", build_turns(staging, env["transcripts_dir"]))
        # Before anything else reads turns, so thrown-out ones count for nothing.
        report("Stretches the operator threw out:", forget(staging, env["labels_dir"]))
        report("Conversations the operator released early:", record_releases(staging, env["labels_dir"]))
        report("Operator labels and tags, matched to turns:", record_operator_input(staging, env["labels_dir"]))
        report("Speaker embeddings and labels:",
               label_speakers(env["raw_dir"], staging, env["labels_dir"], env["model_dir"], cache))
        report("Turns that sound like tagged media:", score_media(staging))
        report("Turns that sound like songs:", score_music(env["raw_dir"], staging, env["music_model_dir"], cache))
        report("The owner's tone of voice:", score_affect(env["raw_dir"], staging, env["affect_model_dir"], cache))
        report("How the owner delivered their turns:", measure_delivery(staging, env["transcripts_dir"]))
        record_run(staging)
    finally:
        cache.close()


def run(env: dict[str, Path]) -> None:
    # On the audio's own filesystem, so moving a WAV into place is a rename.
    staged_audio = env["audio_dir"] / ".staged"
    with lock(env["db"]), tempfile.TemporaryDirectory(prefix="hearsay-reprocess-") as scratch:
        shutil.rmtree(staged_audio, ignore_errors=True)
        staged_audio.mkdir()
        try:
            staging = Path(scratch) / env["db"].name
            build(env, staging, staged_audio, env["records_dir"], unseal_late=True)
            report(f"Updated {env['db']} in place:", update_in_place(staging, env["db"]))
            print(f"Moved {promote_wavs(staged_audio, env['audio_dir'])} changed WAV(s) into place.")
        finally:
            shutil.rmtree(staged_audio, ignore_errors=True)
        # From the database now in place, so the stream always matches it.
        report(f"Utterance stream in {env['stream_dir']}:", write_stream(env["db"], env["stream_dir"]))
        print(f"Removed {remove_stale_wavs(env['audio_dir'], env['db'])} stale WAV(s).")
        print(f"Removed {remove_stale_import_pcm(imports_pcm(env), env['db'])} stale decoded import(s) and "
              f"{remove_stale_capture_pcm(capture_pcm(env), env['db'])} stale decoded capture run(s).")
        report(f"Sealed in {env['records_dir']}:",
               seal(env["db"], env["records_dir"], env["transcripts_dir"], env["labels_dir"]))
        report("Deleted and cut (hearsay/retention.py):", retention.run(env))
        cache = Cache(env["db"].parent / "cache.sqlite")
        try:
            print(f"Dropped {cache.prune(HOLD)} cached result(s) no run has used for {HOLD // 86400} days.")
        finally:
            cache.close()


def schema(db: sqlite3.Connection, name: str) -> dict[str, tuple[str, str]]:
    return {row[0]: (row[1], row[2]) for row in db.execute(
        f"SELECT name, type, sql FROM {name}.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")}


def update_in_place(built: Path, db_path: Path) -> dict:
    """Make db_path hold exactly what built holds, in one transaction.

    Rows are compared whole, as a multiset per table (some tables have no
    key), and only the differences are written, so an unchanged database
    gets no writes at all. A table or index whose definition changed is
    dropped and recreated. Nothing reads the live database in row order.
    """
    counts = {"tables_recreated": 0, "rows_deleted": 0, "rows_inserted": 0}
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        db.execute("ATTACH DATABASE ? AS built", (str(built),))
        db.execute("BEGIN IMMEDIATE")
        new, old = schema(db, "built"), schema(db, "main")
        for name, (kind, sql) in old.items():
            if new.get(name) != (kind, sql):
                # Dropping a table drops its indexes too, hence IF EXISTS.
                db.execute(f'DROP {kind.upper()} IF EXISTS main."{name}"')
        for name, (kind, sql) in new.items():
            if kind == "table" and old.get(name) != (kind, sql):
                db.execute(sql)
                counts["tables_recreated"] += 1
        for name, (kind, _) in new.items():
            if kind != "table":
                continue
            wanted = Counter(db.execute(f'SELECT * FROM built."{name}"'))
            stale = []
            for rowid, *row in db.execute(f'SELECT rowid, * FROM main."{name}"').fetchall():
                row = tuple(row)
                if wanted[row] > 0:
                    wanted[row] -= 1
                else:
                    stale.append((rowid,))
            # Deletes first, so a changed row never collides with its old key.
            db.executemany(f'DELETE FROM main."{name}" WHERE rowid = ?', stale)
            inserts = list(wanted.elements())
            if inserts:
                marks = ",".join("?" * len(inserts[0]))
                db.executemany(f'INSERT INTO main."{name}" VALUES ({marks})', inserts)
            counts["rows_deleted"] += len(stale)
            counts["rows_inserted"] += len(inserts)
        # Indexes last: recreated if changed, or dropped with their table.
        present = schema(db, "main")
        for name, (kind, sql) in new.items():
            if kind != "table" and name not in present:
                db.execute(sql)
        db.execute("COMMIT")
    finally:
        db.close()
    return counts


def record_run(db_path: Path) -> None:
    """Record what produced this database, so a stale image is easy to spot."""
    info = {
        "commit": os.environ.get("HEARSAY_COMMIT", "unknown"),
        "conversation_gap": conversations.GAP,
        "min_speech": conversations.MIN_SPEECH,
        "min_turn": speakers.MIN_TURN,
        "owner_threshold": speakers.OWNER_THRESHOLD,
        "not_owner_threshold": speakers.NOT_OWNER_THRESHOLD,
        "diarization_agreement": speakers.DIARIZATION_AGREEMENT,
    }
    db = sqlite3.connect(db_path)
    try:
        db.executescript("DROP TABLE IF EXISTS reprocess_info; CREATE TABLE reprocess_info (key TEXT PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO reprocess_info VALUES (?, ?)", [(k, str(v)) for k, v in info.items()])
        db.commit()
    finally:
        db.close()
    print(f"Built from commit {info['commit']}.")


def main() -> None:
    env = {name: Path(os.environ[f"HEARSAY_{name.upper()}"]) for name in DIRS}
    try:
        run(env)
    except ParseFailed as e:
        print(e, file=sys.stderr)
        print(f"{env['db']} and {env['audio_dir']} left unchanged.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

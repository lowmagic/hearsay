"""Whether a conversation has ended that the database doesn't have as ended
yet, and if so, enqueue the pipeline (hearsay/enqueue.py CHAIN).

The capture receiver enqueues this after every upload, which the iPhone app
makes every minute while it records, and an hourly cron enqueues it for
uploads that couldn't reach corral (install/nas.sh). Enqueueing the whole
pipeline on every upload instead reprocessed and transcribed a conversation
over and over while it went on. A conversation ends after GAP without speech
or at an end mark (hearsay/conversations.py); until then the database has it
as open, and transcription leaves it alone (hearsay/transcribe.py pending).
One whose recording stopped within GAP of its last speech stays open until
the next recording, so it counts as ended once STOPPED has passed, which the
hourly check finds. So the pipeline runs once a conversation ends, and Idea
Machine gets it then.

It finds conversations with reprocess's own steps up to there
(hearsay/reprocess.py find), into a scratch database: raw parsed, sealed
conversations from their records, the audio decoded and placed, and voice
detection. So it sees what a reprocess would make, and once that reprocess
has run, the database agrees and this finds nothing. Decoded audio and voice
detection are cached by content, so only audio new since the last check or
reprocess costs time; cutting WAVs, speaker embeddings, the audio models and
writing the database and stream, the rest of a reprocess, are skipped.
Audio that arrived late at the seal line (hearsay/records.py late) fires it
too: a reprocess unseals for it, which this, writing nothing, can't.

It writes nothing of reprocess's (the database, WAVs, records, the stream)
and keeps no state of its own: the database is what it compares with. It
shares reprocess's caches, so it takes its lock.
"""

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from hearsay import enqueue
from hearsay.cache import Cache
from hearsay.conversations import STOPPED
from hearsay.parse import ParseFailed, rebuild
from hearsay.records import iso, late, sealed_payloads
from hearsay.reprocess import find, lock, report

DIRS = ("raw_dir", "db", "labels_dir", "imports_dir", "records_dir", "pcm_dir")


def ended(built: Path, db_path: Path, now: float) -> list[str]:
    """Conversations built has as ended that db_path doesn't yet: lacks, has
    as open where built has it closed, or, for one whose recording stopped,
    has with other audio than built's (so transcription hasn't seen it whole)."""
    db = sqlite3.connect(built)
    try:
        found = db.execute("SELECT conversation_id, end, open FROM conversations ORDER BY start").fetchall()
    finally:
        db.close()
    known = {}
    # Before the first reprocess there's no database, and opening it must not make one.
    if db_path.exists():
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            known = {cid: (end, is_open) for cid, end, is_open in db.execute(
                "SELECT conversation_id, end, open FROM conversations")}
        finally:
            db.close()
    new = []
    for cid, end, is_open in found:
        if is_open and end >= now - STOPPED:
            continue  # still going on
        if cid not in known or (known[cid][1] and (not is_open or known[cid][0] != end)):
            new.append(cid)
    return new


def run(env: dict[str, Path], corral_url: str, corral_token: str, now: float) -> None:
    with lock(env["db"]), tempfile.TemporaryDirectory(prefix="hearsay-check-") as scratch:
        staging = Path(scratch) / env["db"].name
        cache = Cache(env["db"].parent / "cache.sqlite")
        try:
            sealed, imports = sealed_payloads(env["records_dir"])
            report(f"Rebuilt from {env['raw_dir']}:", rebuild(env["raw_dir"], staging, sealed))
            when, _ = late(staging, env["records_dir"], env["imports_dir"], env["labels_dir"])
            find(env, staging, env["records_dir"], imports, cache)
        finally:
            cache.close()
        new = ended(staging, env["db"], now)
    if new:
        print(f"Ended since the last reprocess: {', '.join(new)}.")
    if when is not None:
        print(f"Audio arrived late at the seal line, from {iso(when)}.")
    if not new and when is None:
        print("No conversation has ended since the last reprocess.")
        return
    job_id, created = enqueue.post(corral_url, corral_token, enqueue.CHAIN)
    print(f"Enqueued the pipeline: job {job_id}." if created else f"The pipeline was already queued: job {job_id}.")


def main() -> None:
    env = {name: Path(os.environ[f"HEARSAY_{name.upper()}"]) for name in DIRS}
    try:
        run(env, os.environ["CORRAL_URL"], os.environ["CORRAL_TOKEN"], time.time())
    except ParseFailed as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

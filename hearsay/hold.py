"""The hold (roadmap slice 20): a conversation reaches the stream two weeks
after it ends, or sooner when the operator releases it.

Until then consumers don't see it at all (its index entry, file, taps and
places), and the operator can read and hear it in the portal and throw out
what shouldn't go (hearsay/forget.py). Two weeks to start, for ample time to
review; likely shortened once reviewing is routine.

Releasing is the operator's input in labels/release.jsonl, by absolute time
like everything else, so it survives re-transcription:

    {"type": "release", "at": [start, end], "tagged_at": ...}

It releases a conversation that lies wholly inside its span. Only a
conversation that has ended can be released: one still going would release
whatever is said next unread. If boundaries move so that the conversation no
longer fits the span, it's held again, which is the safe way to be wrong.

The stream never withdraws a conversation it has published (hearsay/stream.py):
a consumer would take that for speech that vanished. So whatever was in the
stream before the hold began stays, a conversation late audio gives a new id
isn't held if it takes in published speech, and only throwing out deletes.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

HOLD = 14 * 24 * 3600  # seconds after a conversation ends

SCHEMA = """
DROP TABLE IF EXISTS releases;
-- Spans the operator released early; a conversation wholly inside one isn't held.
CREATE TABLE releases (
    start REAL NOT NULL,             -- unix seconds
    end REAL NOT NULL
);
"""


def read_releases(labels_dir: Path) -> list[tuple[float, float]]:
    path = labels_dir / "release.jsonl"
    return [tuple(datetime.fromisoformat(t).timestamp() for t in json.loads(line)["at"])
            for line in (path.read_text().splitlines() if path.exists() else [])]


def held(start: float, end: float, releases: list[tuple[float, float]], now: float) -> bool:
    """Whether a conversation is still held back from the stream."""
    return end + HOLD > now and not any(s <= start and end <= e for s, e in releases)


def record_releases(db_path: Path, labels_dir: Path) -> dict:
    releases = read_releases(labels_dir)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO releases VALUES (?,?)", releases)
        db.commit()
    finally:
        db.close()
    return {"released": len(releases)}

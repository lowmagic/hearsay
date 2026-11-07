"""The hold (roadmap slice 20): a conversation reaches the stream two weeks
after it ends, or sooner when the operator releases it.

Until then consumers don't see it at all (its index entry, file, taps and
places), and the operator can read and hear it in the portal and throw out
what shouldn't go (hearsay/forget.py). Two weeks to start, for ample time to
review; 7 days once reviewing is routine. The same boundary seals a
conversation (hearsay/records.py) and deletes what was thrown out of it
(hearsay/retention.py), so everything stays open to change while it's
reviewed, and what Idea Machine gets has its final transcript.

Releasing is the operator's input in labels/release.jsonl, by absolute time
like everything else, so it survives re-transcription:

    {"type": "release", "at": [start, end], "tagged_at": ...}
    {"type": "undo", "at": [start, end], "tagged_at": ...}

It releases a conversation that lies wholly inside its span. Only a
conversation that has ended can be released: one still going would release
whatever is said next unread. If boundaries move so that the conversation no
longer fits the span, it's held again, which is the safe way to be wrong.

A release takes effect DELAY after it's made, and until then the operator
can cancel it (an undo with the same span): a reprocess counts only the
releases due when it reads them, so one already running when Release is
pressed doesn't send the conversation (one did, 22 seconds after a press,
on 2026-11-07). The portal enqueues a reprocess for when it's due
(hearsay/enqueue.py after_release).

The stream never withdraws a conversation it has published (hearsay/stream.py):
a consumer would take that for speech that vanished. So whatever was in the
stream before the hold began stays, a conversation late audio gives a new id
isn't held if it takes in published speech, and only throwing out deletes.
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

HOLD = 14 * 24 * 3600  # seconds after a conversation ends
DELAY = 5 * 60  # seconds after Release is pressed, during which it can be cancelled

SCHEMA = """
DROP TABLE IF EXISTS releases;
-- Spans the operator released early; a conversation wholly inside one isn't held.
CREATE TABLE releases (
    start REAL NOT NULL,             -- unix seconds
    end REAL NOT NULL
);
"""


def read_releases(labels_dir: Path) -> list[dict]:
    """The operator's releases, in order, without those cancelled. Each has at
    as written, start and end in unix seconds, and due: when it takes effect."""
    path = labels_dir / "release.jsonl"
    releases = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        record = json.loads(line)
        span = tuple(datetime.fromisoformat(t).timestamp() for t in record["at"])
        if record["type"] == "undo":
            releases.pop(span, None)
        else:  # pressed again before it was due: the first press's time stands
            releases.setdefault(span, {"at": record["at"], "start": span[0], "end": span[1],
                                       "due": datetime.fromisoformat(record["tagged_at"]).timestamp() + DELAY})
    return list(releases.values())


def due(releases: list[dict], now: float) -> list[tuple[float, float]]:
    """The spans of the releases in effect at now."""
    return [(r["start"], r["end"]) for r in releases if r["due"] <= now]


def held(start: float, end: float, releases: list[tuple[float, float]], now: float) -> bool:
    """Whether a conversation is still held back from the stream, given the releases in effect."""
    return end + HOLD > now and not any(s <= start and end <= e for s, e in releases)


def record_releases(db_path: Path, labels_dir: Path, now: float | None = None) -> dict:
    releases = due(read_releases(labels_dir), time.time() if now is None else now)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO releases VALUES (?,?)", releases)
        db.commit()
    finally:
        db.close()
    return {"released": len(releases)}

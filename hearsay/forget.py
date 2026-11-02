"""Stretches the operator threw out (roadmap slice 20), removed on every reprocess.

The operator marks a stretch on a conversation's page in the portal, from
one turn to another or the whole conversation, with an optional reason. A
mark is durable input in labels/forget.jsonl, by absolute time like tags, so
it applies whatever the transcript and conversation boundaries become:

    {"type": "forget", "at": [start, end], "reason": "...", "tagged_at": ...}
    {"type": "undo", "at": [start, end], "tagged_at": ...}

An undo takes back the mark with the same span. Right after turns are built,
every turn a mark overlaps at all is deleted, so its words leave the database
and count for nothing in any later step (clusters, hints, affect, delivery).
Any overlap rather than tags' half (hearsay/turns.py): when a new transcript
moves a boundary, keeping words the operator threw out is worse than losing
a neighbour's.

The marks applied are kept in the forgotten table, with the turns each
removed, for the stream: hearsay/stream.py appends each to forgotten.json the
first time it sees it, and from then on it's final there even if the mark
is taken back, since forgotten.json never shrinks. The portal offers Undo
only until then.

The audio, raw payloads and transcripts of a thrown-out stretch are kept
until retention (slice 12).
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
DROP TABLE IF EXISTS forgotten;
-- Stretches the operator threw out, as applied to this transcript.
CREATE TABLE forgotten (
    start REAL NOT NULL,             -- unix seconds
    end REAL NOT NULL,
    marked_at TEXT NOT NULL,         -- when the operator threw it out
    reason TEXT,
    conversation_id TEXT,            -- the conversation it was in, NULL if none
    turn_ids TEXT NOT NULL           -- JSON list of the turns it removed
);
"""


def read_marks(labels_dir: Path) -> list[dict]:
    """The operator's marks, in order, without those taken back. Each has
    at as written, start and end in unix seconds, reason and marked_at."""
    path = labels_dir / "forget.jsonl"
    marks = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        record = json.loads(line)
        span = tuple(datetime.fromisoformat(t).timestamp() for t in record["at"])
        if record["type"] == "undo":
            marks.pop(span, None)
        else:
            marks[span] = {"at": record["at"], "start": span[0], "end": span[1],
                           "reason": record.get("reason") or None, "marked_at": record["tagged_at"]}
    return list(marks.values())


def forget(db_path: Path, labels_dir: Path) -> dict:
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        turns = [(turn_id, datetime.fromisoformat(zero_at).timestamp() + start,
                  datetime.fromisoformat(zero_at).timestamp() + end)
                 for turn_id, zero_at, start, end in db.execute(
                     "SELECT t.turn_id, ca.zero_at, t.start, t.end FROM turns t"
                     " JOIN conversation_audio ca USING (conversation_id) ORDER BY t.conversation_id, t.start")]
        conversations = db.execute("SELECT conversation_id, start, end FROM conversations ORDER BY start").fetchall()
        removed = set()
        for mark in read_marks(labels_dir):
            hit = [turn_id for turn_id, start, end in turns if start < mark["end"] and end > mark["start"]]
            within = next((c for c, start, end in conversations if start < mark["end"] and end > mark["start"]), None)
            db.execute("INSERT INTO forgotten VALUES (?,?,?,?,?,?)",
                       (mark["start"], mark["end"], mark["marked_at"], mark["reason"], within, json.dumps(hit)))
            removed.update(hit)
        db.executemany("DELETE FROM turns WHERE turn_id = ?", [(t,) for t in sorted(removed)])
        db.commit()
        [(marks,)] = db.execute("SELECT count(*) FROM forgotten").fetchall()
    finally:
        db.close()
    return {"stretches": marks, "turns_removed": len(removed)}

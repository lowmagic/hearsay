"""A key-value cache for results that are slow to compute and depend only on audio.

Keys are a hash of the input audio plus the model and settings that produced
the value, so a hit is always exactly what recomputing would give. The cache
is derived: deleting the file is always safe, the next run just repeats work.

Each entry notes the last day a run used it, and reprocess drops entries no
run has used for HOLD (prune): only audio not yet sealed is read, so what's
left is for sealed audio, which only an unseal reads again, or for audio
retention has deleted (hearsay/retention.py), whose results shouldn't
outlive it. The day is written at most once a day per entry, so a run with
nothing new writes almost nothing here either.
"""

import hashlib
import sqlite3
import time
from pathlib import Path


def audio_key(kind: str, pcm: bytes) -> str:
    return f"{kind}:{hashlib.sha256(pcm).hexdigest()}"


def today() -> int:
    return int(time.time() // 86400)


class Cache:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        # Apart from kv, so a cache made before it needs nothing done: an entry not listed was never used.
        self.db.execute("CREATE TABLE IF NOT EXISTS used (key TEXT PRIMARY KEY, day INTEGER NOT NULL)")

    def get(self, key: str) -> bytes | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row:
            self.use(key)
        return row[0] if row else None

    def put(self, key: str, value: bytes) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.use(key)

    def use(self, key: str) -> None:
        self.db.execute("INSERT INTO used VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET day = excluded.day"
                        " WHERE day < excluded.day", (key, today()))

    def prune(self, seconds: float) -> int:
        """Drop the entries no run has used for this long; their bytes are overwritten, not just freed."""
        since = today() - seconds // 86400
        self.db.execute("PRAGMA secure_delete = ON")
        gone = self.db.execute("DELETE FROM kv WHERE key NOT IN (SELECT key FROM used WHERE day >= ?)",
                               (since,)).rowcount
        self.db.execute("DELETE FROM used WHERE day < ?", (since,))
        return gone

    def close(self) -> None:
        self.db.commit()
        self.db.close()

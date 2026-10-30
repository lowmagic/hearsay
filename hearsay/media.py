"""A hint for the operator: turns that sound like the media they've tagged.

Speaker embeddings carry the channel as well as the voice, and dialogue from
a TV through its speaker sounds unlike people in the room. On every reprocess
a logistic regression learns that from the operator's own input: turns tagged
_media against turns of someone present (a name the operator gave, _stranger,
_me, or the owner by ear). A name still means someone was there, though it no
longer decides who spoke. _mic counts as neither: someone on a microphone is
between the two (an MC, a stand-up set). Each embedded turn gets a score, and
the turns at HINT or more that weren't the owner's by voice or channel
(MEDIA_SQL) are _media unless the operator tagged them (hearsay/turns.py,
categories). The portal keeps offering them, so a wrong one is a tap to put
back.

Left out without a tag since 2026-10-30: of the turns at HINT the operator
had tagged, 240 were _media, 4 _music and none someone present. After the
operator tagged the 18 then untagged, the clip tags at HINT were still only
_media (48) and _music (4).

Measured 2026-10-29, leave one conversation out, per turn: AUC 0.97; at HINT,
195 of 245 media turns (91% of media time) against 4 of 398 turns of people
present (0.7 minutes). Without names, as many media turns are caught but 315
untagged turns are offered rather than 24: voices like the people the
operator named. Adding AudioSet's classes (AST) made it worse (people flagged
3.1 minutes), and on its own AudioSet barely beats chance (2026-10-07),
because the media here is dialogue. Nearly all of it was one TV at home, so
the hint spreads elsewhere only as the operator tags media elsewhere.
"""

import sqlite3
from pathlib import Path

# Score a turn needs for the hint.
HINT = 0.9

# Turns at the hint, unless their own voice or channel said the owner's.
MEDIA_SQL = f"""
SELECT tm.turn_id FROM turn_media tm JOIN turn_speakers ts ON ts.turn_id = tm.turn_id
WHERE tm.score >= {HINT} AND NOT (ts.label IS 'owner' AND ts.basis IN ('voice', 'channel'))
"""
# Tagged turns of each kind needed before there's anything to learn from.
MIN_TRAINING = 20

SCHEMA = """
DROP TABLE IF EXISTS turn_media;
-- How much each embedded turn sounds like the operator's _media tags, 0-1.
CREATE TABLE turn_media (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    score REAL NOT NULL
);
"""


def train(x, y, steps: int = 300, rate: float = 0.5, l2: float = 1e-2):
    """Logistic regression by gradient descent: deterministic, numpy only."""
    import numpy as np

    w, b = np.zeros(x.shape[1]), 0.0
    for _ in range(steps):
        error = 1 / (1 + np.exp(-(x @ w + b))) - y
        w -= rate * (x.T @ error / len(y) + l2 * w)
        b -= rate * error.mean()
    return w, b


def score_media(db_path: Path) -> dict:
    import numpy as np

    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT ts.turn_id, ts.embedding,"
            " (SELECT value FROM operator_input oi WHERE oi.turn_id = ts.turn_id AND oi.kind IN ('name', 'clip')"
            "  ORDER BY oi.kind = 'clip' DESC LIMIT 1),"
            " EXISTS (SELECT 1 FROM operator_input oi WHERE oi.turn_id = ts.turn_id AND oi.kind = 'label' AND oi.value = 'owner')"
            " FROM turn_speakers ts WHERE ts.embedding IS NOT NULL ORDER BY ts.turn_id"
        ).fetchall()
        # 1 media, 0 someone present, -1 not a tag to learn from.
        y = np.array([1 if name == "_media" else 0 if (name and not name.startswith("_")) or name in ("_stranger", "_me") or owner
                      else -1 for _, _, name, owner in rows])
        db.executescript(SCHEMA)
        if (y == 1).sum() < MIN_TRAINING or (y == 0).sum() < MIN_TRAINING:
            db.commit()
            return {"scored": 0, "media_tags": int((y == 1).sum()), "present_tags": int((y == 0).sum())}
        x = np.array([np.frombuffer(embedding, dtype="<f4") for _, embedding, _, _ in rows], dtype=float)
        x = (x - x.mean(0)) / (x.std(0) + 1e-6)
        w, b = train(x[y >= 0], y[y >= 0].astype(float))
        scores = 1 / (1 + np.exp(-(x @ w + b)))
        db.executemany("INSERT INTO turn_media VALUES (?,?)",
                       [(turn_id, round(float(s), 3)) for (turn_id, _, _, _), s in zip(rows, scores)])
        db.commit()
    finally:
        db.close()
    return {"scored": len(rows), "media_tags": int((y == 1).sum()), "present_tags": int((y == 0).sum()),
            "media_like_turns": int((scores >= HINT).sum())}

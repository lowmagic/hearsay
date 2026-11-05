import random
import sqlite3
from array import array

import pytest

pytest.importorskip("numpy")

from hearsay.media import HINT, SCHEMA, score_media  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA  # noqa: E402


def unit(v):
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def build(tmp_path, media, present, extra=()):
    """A TV's voices and room voices: `media` TV turns tagged _media, `present` room turns named, then a room
    turn for each (kind, value) of operator input in `extra`, and 10 more of each untagged."""
    rng = random.Random(1)
    tv, room = unit([rng.gauss(0, 1) for _ in range(192)]), unit([rng.gauss(0, 1) for _ in range(192)])
    db_path = tmp_path / "h.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript(TURNS_SCHEMA + SPEAKERS_SCHEMA + SCHEMA)
    tags = {"tv": [("name", "_media")] * media,
            "room": [("name", f"Person {i % 4}") for i in range(present)] + list(extra)}
    for kind, centre in (("tv", tv), ("room", room)):
        for i in range(len(tags[kind]) + 10):
            turn_id = f"{kind}{i}"
            vector = unit([c + rng.gauss(0, 0.06) for c in centre])
            db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, ?, 0, 'not_owner', 'voice', NULL, NULL)",
                       (turn_id, array("f", vector).tobytes()))
            if i < len(tags[kind]):
                db.execute("INSERT INTO operator_input VALUES (?, ?, ?)", (turn_id, *tags[kind][i]))
    db.commit()
    db.close()
    return db_path


def test_untagged_turns_that_sound_like_tagged_media_score_as_media(tmp_path):
    db_path = build(tmp_path, media=25, present=25)
    score_media(db_path)
    db = sqlite3.connect(db_path)
    scores = dict(db.execute("SELECT turn_id, score FROM turn_media"))
    db.close()
    assert all(scores[f"tv{i}"] >= HINT for i in range(25, 35))  # the untagged ones
    assert all(scores[f"room{i}"] < HINT for i in range(25, 35))


@pytest.mark.parametrize("media, present", [(5, 5), (25, 5), (5, 25)])
def test_too_few_tags_of_either_kind_give_no_hints(tmp_path, media, present):
    db_path = build(tmp_path, media=media, present=present)
    assert score_media(db_path)["scored"] == 0
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT count(*) FROM turn_media").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("extra, learned", [
    ([("label", "owner")] * 5, True),   # the owner by ear was present
    ([("clip", "_me")] * 5, True),
    ([("name", "_mic")] * 5, False),    # on a microphone: neither present nor media
])
def test_someone_present_is_a_name_or_the_owner_but_not_someone_on_a_mic(tmp_path, extra, learned):
    # 15 named turns are short of MIN_TRAINING; the 5 more make it only if they count as someone present.
    db_path = build(tmp_path, media=25, present=15, extra=extra)
    assert (score_media(db_path)["scored"] > 0) == learned

import sqlite3

from hearsay.assemble import SCHEMA as AUDIO_SCHEMA
from hearsay.label import MUSIC_SQL, dropped, music_table
from hearsay.music import SCHEMA as MUSIC_SCHEMA
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.turns import SCHEMA as TURNS_SCHEMA


def test_music_pool_is_quiet_turns_with_their_voice_label():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(AUDIO_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + MUSIC_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-10-01T20:00:00+00:00', 60, 1, 'c1.wav', 'x')")
    for i, (speech, label) in enumerate(((0.1, "owner"), (0.2, None), (0.9, "owner"))):
        db.execute("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, 'la la', 'S0', 0.9)", (f"c1:{i}", i, i * 2, i * 2 + 1))
        db.execute("INSERT INTO turn_music VALUES (?, ?, 0, 0.5)", (f"c1:{i}", speech))
        if label:
            db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, NULL, NULL, ?, 'voice')", (f"c1:{i}", label))
    pool = [dict(row) for row in db.execute(MUSIC_SQL)]
    assert sorted((t["turn_id"], t["label"]) for t in pool) == [("c1:0", "owner"), ("c1:1", None)]


def test_music_table_counts_what_each_cutoff_drops():
    pool = [{"turn_id": "song", "music": 0.8, "start": 0, "end": 60, "label": None},
            {"turn_id": "talk", "music": 0.5, "start": 0, "end": 30, "label": "owner"},
            {"turn_id": "quiet", "music": 0.1, "start": 0, "end": 6, "label": "owner"}]
    rows = {line.split()[0]: line.split() for line in music_table(pool, {"song": "song", "talk": "talk"})[2:]}
    # music >= 0.50 drops the song and the talk: half right, all songs, half a minute of talk.
    assert rows["0.50"] == ["0.50", "2", "1.5", "1", "1", "1", "0.50", "1.00", "0.50"]
    assert rows["0.55"] == ["0.55", "1", "1.0", "0", "1", "0", "1.00", "1.00", "0.00"]
    assert rows["0.85"] == ["0.85", "0", "0.0", "0", "0", "0", "-", "0.00", "0.00"]


def test_dropped_follows_the_music_rule():
    song = {"music": 0.5, "speech": 0.1, "start": 0, "end": 2}
    assert dropped(song)
    assert not dropped({**song, "speech": 0.25})  # too much speech
    assert not dropped({**song, "end": 0.6})      # too short to judge
    assert not dropped({**song, "music": 0.2})

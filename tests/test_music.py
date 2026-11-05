import sqlite3

from hearsay import music
from hearsay.assemble import assemble
from hearsay.parse import rebuild
from hearsay.turns import SCHEMA as TURNS_SCHEMA
from test_assemble import T, add_conversations, audio_burst, seconds


def test_only_turns_long_enough_with_enough_audio_are_scored(tmp_path, monkeypatch):
    raw, db_path, audio_dir = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "audio"
    audio_dir.mkdir()
    # Audio for [0, 10) and [15, 20) s after T: the burst between was lost.
    audio_burst(raw, 0, seconds(5))
    audio_burst(raw, 1, seconds(10))
    audio_burst(raw, 3, seconds(20))
    rebuild(raw, db_path)
    add_conversations(db_path, [("c1", T.timestamp(), seconds(20).timestamp(), 15.0, 0)])
    assemble(raw, db_path, audio_dir)
    db = sqlite3.connect(db_path)
    db.executescript(TURNS_SCHEMA)
    db.executemany("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, 'words', 'S0', 0.9)", [
        ("c1:0000", 0, 1.0, 6.0),    # all received
        ("c1:0001", 1, 8.0, 13.0),   # 2 of its 5 s received
        ("c1:0002", 2, 16.0, 16.4),  # received, but too short to judge
    ])
    db.commit()
    db.close()
    scored = []
    monkeypatch.setattr(music, "load_model", lambda model_dir: "model")
    monkeypatch.setattr(music, "score", lambda model, pcm, cache=None: scored.append(len(pcm)) or (0.1, 0.2, 0.7))

    assert music.score_music(raw, db_path, tmp_path / "model")["scored"] == 1
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT * FROM turn_music").fetchall() == [("c1:0000", 0.1, 0.2, 0.7)]
    db.close()
    assert scored == [5 * 32000]  # the 5 s turn's audio, cut from what was received

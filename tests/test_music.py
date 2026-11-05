import sqlite3

from hearsay import music
from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.records import SCHEMA as RECORDS_SCHEMA
from hearsay.turns import SCHEMA as TURNS_SCHEMA


def test_only_turns_long_enough_with_enough_audio_are_scored(tmp_path, monkeypatch):
    db_path = tmp_path / "h.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript(TURNS_SCHEMA + ASSEMBLE_SCHEMA + RECORDS_SCHEMA + music.SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-09-21T14:13:20+00:00', 100, 1, 'c1.wav', 'sha', 0)")
    # (start, end, fraction of it received)
    turns = [(0.0, 5.0, 0.9), (10.0, 15.0, 0.5), (20.0, 20.4, 1.0)]
    for idx, (start, end, _) in enumerate(turns):
        db.execute("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, 'words', 'S0', 0.9)", (f"c1:{idx:04d}", idx, start, end))
    db.commit()
    db.close()
    zero = music.timestamp("2026-09-21T14:13:20+00:00")
    coverage = {start: received for start, _, received in turns}
    monkeypatch.setattr(music, "place_bursts", lambda db: [])
    monkeypatch.setattr(music, "cut", lambda raw_dir, bursts, start, duration: (b"", coverage[start - zero]))
    monkeypatch.setattr(music, "load_model", lambda model_dir: "model")
    monkeypatch.setattr(music, "score", lambda model, pcm, cache=None: (0.1, 0.2, 0.7))

    assert music.score_music(tmp_path / "raw", db_path, tmp_path / "model")["scored"] == 1
    db = sqlite3.connect(db_path)
    assert db.execute("SELECT * FROM turn_music").fetchall() == [("c1:0000", 0.1, 0.2, 0.7)]
    db.close()

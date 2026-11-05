import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hearsay import reprocess, speakers
from test_assemble import add_conversations, audio_burst


def test_a_failed_run_leaves_the_previous_database_and_audio_in_place(tmp_path, monkeypatch):
    env = {name: tmp_path / name for name in reprocess.DIRS if name != "db"}
    for d in env.values():
        d.mkdir()
    env["db"] = tmp_path / "hearsay.sqlite"
    wav = env["audio_dir"] / "c1.wav"
    # An hour ago: recent enough that the run doesn't seal it, so every run cuts it from raw.
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)

    def conversation(raw_dir, db_path, cache=None):
        """What voice detection would find: one conversation over [0, 10) s."""
        add_conversations(db_path, [("c1", start.timestamp(), start.timestamp() + 10, 10.0, 0)])
        return {}

    def no_model(raw_dir, db_path, *args, **kwargs):
        """Speaker labels without the model: none, as for a conversation not yet transcribed."""
        db = sqlite3.connect(db_path)
        db.executescript(speakers.SCHEMA)
        db.close()
        return {}

    monkeypatch.setattr(reprocess, "find_conversations", conversation)
    monkeypatch.setattr(reprocess, "label_speakers", no_model)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    audio_burst(env["raw_dir"], 0, start + timedelta(seconds=5))  # a burst is posted as its 5 s end
    # An import an hour before, decoded by an earlier run (so no ffmpeg here).
    export = b"synthetic export"
    digest = hashlib.sha256(export).hexdigest()
    (env["imports_dir"] / "a.m4a").write_bytes(export)
    (env["imports_dir"] / "a.m4a.json").write_text(json.dumps({
        "file": "a.m4a", "start": (start - timedelta(hours=1)).isoformat(), "sha256": digest}))
    reprocess.imports_pcm(env).mkdir()
    (reprocess.imports_pcm(env) / f"{digest}.pcm").write_bytes(bytes(32000))
    reprocess.run(env)
    first, before = sha(wav), sha(env["db"])

    def referenced_pcm():
        db = sqlite3.connect(env["db"])
        paths = [path for (path,) in db.execute(
            "SELECT pcm_path FROM imported_audio UNION SELECT pcm_path FROM captured_audio"
            " UNION SELECT pcm_path FROM mac_audio")]
        db.close()
        return paths

    # Decoded audio is kept in its own directory (its own dataset on the NAS), not beside the database.
    assert referenced_pcm() == [str(env["pcm_dir"] / "imports" / f"{digest}.pcm")]
    assert sorted(p.name for p in env["pcm_dir"].iterdir()) == ["capture", "imports"]

    # More audio arrives and the import is removed, and the run fails after
    # cutting the conversation's new WAV.
    audio_burst(env["raw_dir"], 1, start + timedelta(seconds=10))
    (env["imports_dir"] / "a.m4a.json").unlink()

    def fail(*args, **kwargs):
        raise RuntimeError("the speaker model crashed")

    monkeypatch.setattr(reprocess, "label_speakers", fail)
    with pytest.raises(RuntimeError):
        reprocess.run(env)
    assert (sha(wav), sha(env["db"])) == (first, before)
    assert sorted(p.name for p in env["audio_dir"].iterdir()) == ["c1.wav"]
    # The database left in place still finds every bit of decoded audio it refers to.
    assert all(Path(path).exists() for path in referenced_pcm())

    # The next good run moves the new WAV in, as the database now describes it,
    # and drops the removed import's decoded audio.
    monkeypatch.setattr(reprocess, "label_speakers", no_model)
    reprocess.run(env)
    db = sqlite3.connect(env["db"])
    [(wav_sha256,)] = db.execute("SELECT wav_sha256 FROM conversation_audio WHERE conversation_id = 'c1'").fetchall()
    db.close()
    assert sha(wav) != first and sha(wav) == wav_sha256
    assert referenced_pcm() == [] and list(reprocess.imports_pcm(env).iterdir()) == []


def build(path, script):
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(script)
    db.close()


def dump(path):
    db = sqlite3.connect(path)
    try:
        return sorted(db.iterdump())
    finally:
        db.close()


def test_the_database_is_updated_in_place_and_untouched_when_nothing_changed(tmp_path):
    built, live = tmp_path / "built.sqlite", tmp_path / "hearsay.sqlite"
    first = """
        CREATE TABLE turns (turn_id TEXT PRIMARY KEY, text TEXT);
        CREATE INDEX turns_text ON turns(text);
        CREATE TABLE marks (at REAL, kind TEXT);
        INSERT INTO turns VALUES ('c1:0', 'hello'), ('c1:1', NULL);
        INSERT INTO marks VALUES (1.5, 'start'), (1.5, 'start');
        CREATE TABLE gone (x);
    """
    build(built, first)
    reprocess.update_in_place(built, live)
    assert dump(live) == dump(built)

    inode, before = live.stat().st_ino, live.read_bytes()
    build(built, first)
    assert reprocess.update_in_place(built, live) == {"tables_recreated": 0, "rows_deleted": 0, "rows_inserted": 0}
    assert live.stat().st_ino == inode and live.read_bytes() == before

    # A renamed turn, one fewer duplicate mark, a new column and a dropped table.
    build(built, """
        CREATE TABLE turns (turn_id TEXT PRIMARY KEY, text TEXT);
        CREATE INDEX turns_text ON turns(text);
        CREATE TABLE marks (at REAL, kind TEXT, payload TEXT);
        INSERT INTO turns VALUES ('c1:0', 'hello'), ('c1:1', 'there');
        INSERT INTO marks VALUES (1.5, 'start', 'p');
    """)
    reprocess.update_in_place(built, live)
    assert dump(live) == dump(built)
    assert live.stat().st_ino == inode

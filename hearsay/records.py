"""Turns kept without their audio (roadmap slice 19): what each conversation's
audio produced, kept so a settled conversation never needs its audio again.

A conversation is sealed once it ended SETTLE ago, is transcribed with the
current settings (or has no audio, or failed to transcribe for its WAV and
those settings), and every conversation before it is sealed. The seal line is the end
of the last one sealed: everything before it comes from records, everything
after it from raw, as before. So a reprocess with nothing new reads no audio
older than a week, and once retention (slice 12) deletes old audio, nothing
depends on it.

A record holds what audio produced, never what's decided from it: the
conversation's boundaries and audio facts, the transcript as the dev box
wrote it, and per turn its received coverage, voice embeddings (the long
one, and the short one kept for the veto in hearsay/speakers.py), the Mac
channel that was louder, and the music and tone scores. Who is speaking
(enrollment, thresholds, inheritance), the TV hint, music and media rules,
delivery, places, taps, and the operator's tags and throw-outs all re-run on
every turn ever recorded, from these. What came out of the audio is fixed
once sealed: a new speaker model or vocabulary applies to sealed
conversations only when they're unsealed on purpose, while their audio is
still kept (`python -m hearsay.records unseal <date>`). An import or late
pendant audio landing behind the line unseals back to it on its own.

The records dir, beside raw (/mnt/fast/hearsay/records):

    seal.json                    {"line": unix seconds, "sealed_at": unix seconds,
                                  "imports": the imports there were then}
    conversations/<id>.json      one per sealed conversation, written once
    payloads.jsonl               per sealed upload, the rows parsing made
                                 from it except its audio, so its file is
                                 never opened again; per sealed import, its name
    enrollment.jsonl             the owner's enrollment pieces, embedded, for
                                 windows behind the line

seal.json is written last, so a run that stops partway seals nothing: a
record or digest is used only if it lies behind the line.

Reprocess loads the sealed conversations into the database first (restore),
and every step that would cut audio takes a sealed turn's values from there
instead. `python -m hearsay.records check` builds the database from raw
and from records and compares them.
"""

import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from hearsay.affect import AFFECT_VERSION
from hearsay.conversations import VAD_VERSION
from hearsay.music import MUSIC_VERSION
from hearsay.speakers import EMBED_VERSION
from hearsay.transcribe import settings_for
from hearsay.words import boosted, read_committed

SETTLE = 7 * 24 * 3600  # seconds after a conversation ends
MODELS = {"vad": VAD_VERSION, "speaker": EMBED_VERSION, "music": MUSIC_VERSION, "affect": AFFECT_VERSION}
# Parsed rows kept for a sealed upload: everything parse.py makes from one
# except its audio (audio_chunks), keyed by the payload's path.
PAYLOAD_TABLES = {"payloads": "path", "button_events": "payload_path", "marks": "payload_path",
                  "locations": "payload_path"}

SCHEMA = """
DROP TABLE IF EXISTS seal;
DROP TABLE IF EXISTS sealed_conversations;
DROP TABLE IF EXISTS sealed_turns;
DROP TABLE IF EXISTS sealed_enrollment;
CREATE TABLE seal (
    line REAL NOT NULL               -- unix seconds: before it, everything comes from records
);
-- Sealed conversations as their records hold them.
CREATE TABLE sealed_conversations (
    conversation_id TEXT PRIMARY KEY,
    start REAL NOT NULL,             -- unix seconds, as in conversations
    end REAL NOT NULL,
    speech_seconds REAL NOT NULL,
    zero_at TEXT NOT NULL,           -- as in conversation_audio
    duration REAL NOT NULL,
    coverage REAL NOT NULL,
    wav_sha256 TEXT,
    transcript TEXT                  -- the dev box's JSON, NULL if it had no audio
);
-- What each sealed turn's audio produced; NULL where it produced nothing.
CREATE TABLE sealed_turns (
    turn_id TEXT PRIMARY KEY,
    coverage REAL NOT NULL,
    embedding BLOB,
    veto_embedding BLOB,
    channel TEXT,                    -- owner | not_owner | NULL, as hearsay.speakers.by_channel
    speech REAL,
    singing REAL,
    music REAL,
    arousal REAL,
    dominance REAL,
    valence REAL
);
-- The owner's enrollment pieces behind the line, embedded.
CREATE TABLE sealed_enrollment (
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    coverage REAL NOT NULL,
    vector BLOB NOT NULL
);
"""


def blob(text: str | None) -> bytes | None:
    return base64.b64decode(text) if text is not None else None


def text(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data is not None else None


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def read_seal(records_dir: Path | None) -> dict:
    path = records_dir / "seal.json" if records_dir else None
    return json.loads(path.read_text()) if path and path.exists() else {"line": 0.0, "sealed_at": 0.0, "imports": []}


def sealed_payloads(records_dir: Path | None) -> tuple[dict[str, dict], set[str]]:
    """The uploads behind the line, by path, as parsing would load them; and the imports behind it."""
    line = read_seal(records_dir)["line"]
    digests = [d for d in lines(records_dir / "payloads.jsonl") if d["last_at"] < line] if records_dir else []
    return ({d["path"]: d["rows"] for d in digests if "path" in d},
            {d["import"] for d in digests if "import" in d})


def restore(db_path: Path, records_dir: Path | None) -> dict:
    """Load the sealed conversations, their turns' audio results and the
    enrollment pieces behind the line into the database being built."""
    line = read_seal(records_dir)["line"]
    conversations, turns, other_models = [], [], 0
    folder = records_dir / "conversations" if records_dir else None
    for path in sorted(folder.glob("*.json")) if folder and folder.exists() else []:
        record = json.loads(path.read_text())
        c, a = record["conversation"], record["audio"]
        if c["end"] > line:
            continue  # written by a run that stopped before moving the line
        other_models += record["models"] != MODELS
        conversations.append((c["conversation_id"], c["start"], c["end"], c["speech_seconds"], a["zero_at"],
                              a["duration"], a["coverage"], a["wav_sha256"],
                              json.dumps(record["transcript"]) if record["transcript"] is not None else None))
        turns += [(turn_id, t["coverage"], blob(t["embedding"]), blob(t["veto_embedding"]), t["channel"],
                   *(t["music"] or (None,) * 3), *(t["affect"] or (None,) * 3)) for turn_id, t in record["turns"].items()]
    # One entry per window: a run that stopped before moving the line may have appended it already.
    windows = {(e["start"], e["end"]): e for e in (lines(records_dir / "enrollment.jsonl") if records_dir else [])
               if datetime.fromisoformat(e["end"]).timestamp() <= line}
    enrollment = [(e["start"], e["end"], e["coverage"], blob(v)) for e in windows.values() for v in e["vectors"]]
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO seal VALUES (?)", (line,))
        db.executemany("INSERT INTO sealed_conversations VALUES (?,?,?,?,?,?,?,?,?)", conversations)
        db.executemany("INSERT INTO sealed_turns VALUES (?,?,?,?,?,?,?,?,?,?,?)", turns)
        db.executemany("INSERT INTO sealed_enrollment VALUES (?,?,?,?)", enrollment)
        db.commit()
    finally:
        db.close()
    return {"line": datetime.fromtimestamp(line).isoformat(timespec="minutes") if line else None,
            "conversations": len(conversations), "turns": len(turns), "made_with_other_models": other_models}


def seal(db_path: Path, records_dir: Path, transcripts_dir: Path, labels_dir: Path, now: float | None = None) -> dict:
    """Seal, in order, each conversation that has settled, from the database a run just finished."""
    now = time.time() if now is None else now
    settings = settings_for(boosted(read_committed(labels_dir)))

    def made(path: Path, wav_sha256: str) -> dict | None:
        """The dev box's output at path, if made from this WAV with the current settings."""
        found = json.loads(path.read_text()) if path.exists() else None
        return found if found and [found["wav_sha256"], found["settings"]] == [wav_sha256, settings] else None

    db = sqlite3.connect(db_path)
    try:
        candidates = db.execute(
            "SELECT c.conversation_id, c.start, c.end, c.speech_seconds, c.open, ca.zero_at, ca.duration,"
            " ca.coverage, ca.wav_file, ca.wav_sha256 FROM conversations c"
            " JOIN conversation_audio ca USING (conversation_id) WHERE NOT ca.sealed ORDER BY c.start").fetchall()
        records = []
        for cid, start, end, speech, is_open, zero_at, duration, coverage, wav_file, wav_sha256 in candidates:
            transcript = None
            if wav_file:
                transcript = made(transcripts_dir / f"{cid}.json", wav_sha256)
                # One that failed for this WAV and settings won't be retried: sealed untranscribed.
                if transcript is None and made(transcripts_dir / "failed" / f"{cid}.json", wav_sha256) is None:
                    break  # waits for the dev box
            if is_open or end > now - SETTLE:
                break
            turns = {}
            for turn_id, cov, embedding, veto, channel, *scores in db.execute(
                "SELECT ts.turn_id, ts.coverage, ts.embedding, ts.veto_embedding, ts.channel,"
                " tm.speech, tm.singing, tm.music, ta.arousal, ta.dominance, ta.valence FROM turn_speakers ts"
                " LEFT JOIN turn_music tm USING (turn_id) LEFT JOIN turn_affect ta USING (turn_id)"
                " WHERE ts.conversation_id = ? ORDER BY ts.turn_id", (cid,)):
                turns[turn_id] = {"coverage": cov, "embedding": text(embedding), "veto_embedding": text(veto),
                                  "channel": channel, "music": scores[:3] if scores[0] is not None else None,
                                  "affect": scores[3:] if scores[3] is not None else None}
            records.append({"conversation": {"conversation_id": cid, "start": start, "end": end,
                                             "speech_seconds": speech},
                            "audio": {"zero_at": zero_at, "duration": duration, "coverage": coverage,
                                      "wav_sha256": wav_sha256},
                            "transcript": transcript, "turns": turns, "models": MODELS})
        if not records:
            return {"sealed": 0}
        line = records[-1]["conversation"]["end"]
        old = read_seal(records_dir)["line"]
        digests = []
        for path, last_at in db.execute(
                "SELECT path, last_at FROM payloads WHERE type IN ('capture', 'audio') AND NOT sealed"
                " AND last_at < ?", (line,)).fetchall():
            digests.append({"path": path, "last_at": last_at, "rows": {
                table: db.execute(f"SELECT * FROM {table} WHERE {key} = ?", (path,)).fetchall()
                for table, key in PAYLOAD_TABLES.items()}})
        digests += [{"import": file, "last_at": end} for file, end in db.execute(
            "SELECT file, start + duration FROM imported_audio WHERE start + duration < ?", (line,))]
        # Every import there is now, so only one added later counts as late.
        known = sorted(set(read_seal(records_dir)["imports"])
                       | {file for (file,) in db.execute("SELECT file FROM imported_audio")})
        pieces = {}
        for start, end, coverage, vector in db.execute(
                "SELECT ep.start, ep.end, oe.coverage, ep.vector FROM enrollment_pieces ep"
                " JOIN owner_enrollment oe USING (start, end) ORDER BY ep.rowid"):
            if old < datetime.fromisoformat(end).timestamp() <= line:
                pieces.setdefault((start, end, coverage), []).append(text(vector))
    finally:
        db.close()

    (records_dir / "conversations").mkdir(parents=True, exist_ok=True)
    for record in records:
        write_json(records_dir / "conversations" / f"{record['conversation']['conversation_id']}.json", record)
    with open(records_dir / "payloads.jsonl", "a") as f:
        f.writelines(json.dumps(d) + "\n" for d in digests)
    with open(records_dir / "enrollment.jsonl", "a") as f:
        f.writelines(json.dumps({"start": s, "end": e, "coverage": c, "model": EMBED_VERSION, "vectors": v}) + "\n"
                     for (s, e, c), v in pieces.items())
    # Last: until the line moves, none of the above is used.
    write_json(records_dir / "seal.json", {"line": line, "sealed_at": now, "imports": known})
    return {"sealed": len(records), "uploads": sum(1 for d in digests if "path" in d), "line":
            datetime.fromtimestamp(line).isoformat(timespec="minutes")}


def late(db_path: Path, records_dir: Path, imports_dir: Path) -> float | None:
    """The earliest audio behind the line that arrived after it was drawn: an
    import, or pendant audio downloaded late. None if there's none."""
    seal_state = read_seal(records_dir)
    found = []
    db = sqlite3.connect(db_path)
    try:
        found += [first for first, received in db.execute(
            "SELECT first_at, received_at FROM payloads WHERE type IN ('capture', 'audio') AND NOT sealed"
            " AND first_at < ?", (seal_state["line"],))
            if datetime.fromisoformat(received).timestamp() > seal_state["sealed_at"]]
    finally:
        db.close()
    for sidecar in imports_dir.glob("*.json"):
        record = json.loads(sidecar.read_text())
        start = datetime.fromisoformat(record["start"]).timestamp()
        if record["file"] not in seal_state["imports"] and start < seal_state["line"]:
            found.append(start)
    return min(found, default=None)


def unseal(records_dir: Path, before: float) -> dict:
    """Move the line back to the end of the last sealed conversation that ended
    before `before`, so the next reprocess redoes everything after it from raw.
    Their records are deleted: the audio they came from must still be kept."""
    state = read_seal(records_dir)
    line = state["line"]
    if before >= line:
        return {"unsealed": 0}
    folder = records_dir / "conversations"
    records = {path: json.loads(path.read_text())["conversation"] for path in folder.glob("*.json")}
    keep = max((c["end"] for c in records.values() if c["end"] < before), default=0.0)
    gone = [path for path, c in records.items() if c["end"] > keep]
    write_json(records_dir / "seal.json", {"line": keep, "sealed_at": time.time(), "imports": state["imports"]})
    for path in gone:
        path.unlink()
    # Digests and enrollment pieces past the new line are ignored until it passes them again; drop them.
    write_lines(records_dir / "payloads.jsonl", [d for d in lines(records_dir / "payloads.jsonl")
                                                 if d["last_at"] < keep])
    write_lines(records_dir / "enrollment.jsonl", [e for e in lines(records_dir / "enrollment.jsonl")
                                                   if datetime.fromisoformat(e["end"]).timestamp() <= keep])
    return {"unsealed": len(gone), "line": datetime.fromtimestamp(keep).isoformat(timespec="minutes") if keep else None}


def write_lines(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, path)


# Left out of check: the records themselves, which build, and the audio
# decoded behind the line, which a build from records never reads.
NOT_COMPARED = {"seal", "sealed_conversations", "sealed_turns", "sealed_enrollment", "reprocess_info",
                "captured_audio", "mac_audio", "imported_audio", "audio_chunks"}


def check(env: dict[str, Path]) -> int:
    """Build the database from raw and from records, and say where they differ."""
    from hearsay.reprocess import build, lock

    with lock(env["db"]), tempfile.TemporaryDirectory(prefix="hearsay-check-") as scratch:
        built = {}
        for name, records_dir in (("raw", None), ("records", env["records_dir"])):
            built[name] = Path(scratch) / f"{name}.sqlite"
            # WAVs that differ from the live ones are written here, never over them.
            staged_audio = Path(scratch) / f"{name}-audio"
            staged_audio.mkdir()
            print(f"Building from {name}:")
            build(env, built[name], staged_audio, records_dir)
        db = sqlite3.connect(built["raw"])
        db.execute("ATTACH DATABASE ? AS other", (str(built["records"]),))
        differ = 0
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            if table in NOT_COMPARED:
                continue
            cols = ", ".join(f'"{c}"' for _, c, *_ in db.execute(f'PRAGMA table_info("{table}")') if c != "sealed")
            only_raw = db.execute(f'SELECT count(*) FROM (SELECT {cols} FROM main."{table}"'
                                  f' EXCEPT SELECT {cols} FROM other."{table}")').fetchone()[0]
            only_records = db.execute(f'SELECT count(*) FROM (SELECT {cols} FROM other."{table}"'
                                      f' EXCEPT SELECT {cols} FROM main."{table}")').fetchone()[0]
            if only_raw or only_records:
                differ += 1
                print(f"  {table}: {only_raw} row(s) only from raw, {only_records} only from records")
        db.close()
    print("Same." if not differ else f"{differ} table(s) differ.")
    return 1 if differ else 0


def main() -> None:
    env = {name: Path(os.environ[f"HEARSAY_{name.upper()}"]) for name in
           ("raw_dir", "db", "audio_dir", "labels_dir", "model_dir", "transcripts_dir", "imports_dir",
            "stream_dir", "affect_model_dir", "music_model_dir", "records_dir")}
    if sys.argv[1:2] == ["unseal"] and len(sys.argv) == 3:
        before = datetime.fromisoformat(sys.argv[2]).astimezone().timestamp()
        print(unseal(env["records_dir"], before))
    elif sys.argv[1:] == ["check"]:
        sys.exit(check(env))
    else:
        sys.exit("usage: python -m hearsay.records unseal YYYY-MM-DD | check")


if __name__ == "__main__":
    main()

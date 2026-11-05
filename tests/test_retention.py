import hashlib
import json
import shutil
import sqlite3
import struct
import time
import wave
from datetime import datetime, timezone

import pytest

from hearsay import cache, conversations, label, records, reprocess, retention, speakers
from hearsay.affect import SCHEMA as AFFECT_SCHEMA
from hearsay.assemble import SAMPLE_RATE, SCHEMA as ASSEMBLE_SCHEMA, write_wav
from hearsay.capture import AUDIO, LOCATION
from hearsay.conversations import GAP, SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.forget import forget
from hearsay.hold import HOLD
from hearsay.imports import SCHEMA as IMPORTS_SCHEMA
from hearsay.music import SCHEMA as MUSIC_SCHEMA
from hearsay.parse import rebuild
from hearsay.records import late, restore, seal, sealed_payloads, unseal
from hearsay.retention import RETAIN
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.transcribe import settings_for
from hearsay.turns import build_turns

from test_assemble import audio_burst, segment, write_payload
from test_capture import record, upload
from test_records import dirs, rows, transcript, unit, word

DAY = 86400
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
T0 = NOW - 70 * DAY  # c0: ended more than RETAIN ago
T1 = NOW - 20 * DAY  # c1: sealed, inside the window
WINDOW = (T0 + 10, T0 + 15)  # the owner's enrollment, in c0
# Thrown out of c1 after it was sealed: a turn ("hi back"), and a segment kept whole ("mm hm").
STRETCHES = [(T1 + 3.9, T1 + 8.0), (T1 + 24.5, T1 + 26.5)]
TONE = 1000


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def c1_transcript(conversation_id, wav_sha256):
    return {"conversation_id": conversation_id, "wav_sha256": wav_sha256, "settings": settings_for([]), "segments": [
        {"start": 1.0, "end": 3.5, "text": "hello there", "speaker": "SPEAKER_00",
         "words": [word("hello", 1.0, 1.5, "SPEAKER_00"), word("there", 1.6, 3.5, "SPEAKER_00")]},
        # One sentence, two speakers: the first turn is thrown out, the second kept.
        {"start": 4.0, "end": 12.6, "text": "hi back so so anyway", "speaker": "SPEAKER_01",
         "words": [word("hi", 4.0, 4.5, "SPEAKER_01"), word("back", 4.6, 7.0, "SPEAKER_01"),
                   word("so", 10.0, 10.3, "SPEAKER_00"), word("so", 10.4, 10.7, "SPEAKER_00"),
                   word("anyway", 10.8, 12.6, "SPEAKER_00")]},
        {"start": 18.0, "end": 20.0, "text": "later on", "speaker": "SPEAKER_01",
         "words": [word("later", 18.0, 18.5, "SPEAKER_01"), word("on", 18.6, 20.0, "SPEAKER_01")]},
        # Alignment failed: one turn of the whole segment.
        {"start": 25.0, "end": 26.0, "text": "mm hm", "speaker": "SPEAKER_00", "words": []},
    ]}


def capture(d, key, times, *more):
    """A pendant upload with a live frame arriving at each time; its path in raw."""
    received = datetime.fromtimestamp(times[-1] + 1, timezone.utc)
    upload(d["raw"], received, key, b"".join([record(t, AUDIO, b"\x00\x00\x00frame") for t in times] + list(more)))
    return f"capture/{received:%Y-%m-%d}/{received:%Y%m%dT%H%M%S.%fZ}-{key[-8:]}.body"


def env_of(d, db_path):
    return {"raw_dir": d["raw"], "db": db_path, "audio_dir": d["audio"], "labels_dir": d["labels"],
            "transcripts_dir": d["transcripts"], "imports_dir": d["imports"], "records_dir": d["records"]}


def world(tmp_path):
    """c0 (70 days ago) and c1 (20 days ago) sealed, with every turn embedded,
    then two stretches of c1 thrown out and applied by a run. Returns the
    dirs, the database, and the raw paths of the uploads by name."""
    d = dirs(tmp_path)
    reading = record(T1 + 5.5, LOCATION, struct.pack("<ddf", -26.12, 80.14, 10.0))
    uploads = {
        # Into the first stretch by its records, though its first frame can be placed a second before them.
        "before": capture(d, "k-before", [T1 + 0.5, T1 + 1.5, T1 + 2.5, T1 + 3.5]),
        "inside": capture(d, "k-inside", [T1 + 5.0, T1 + 6.0], reading),
        "edge": capture(d, "k-edge", [T1 + 7.5, T1 + 8.5, T1 + 9.5]),
        "later": capture(d, "k-later", [T1 + 30 + i for i in range(5)]),
        "old": capture(d, "k-old", [T0 + 30 + i for i in range(5)]),
        "enrolled": capture(d, "k-enroll", [T0 + 11 + i for i in range(4)]),
    }
    # Omi's live transcript, from before the pendant app: text, never deleted.
    write_payload(d["raw"], "transcript", datetime.fromtimestamp(T0 + 20, timezone.utc),
                  json.dumps({"segments": [segment("s1", 0.0, 2.0)]}).encode(), [["uid", "u"]])
    imported = [("inside.m4a", T1 + 4.5, 3.0), ("edge.m4a", T1 + 7.0, 5.0), ("old.m4a", T0 + 40, 10.0),
                ("enrolled.m4a", T0 + 5, 20.0)]
    for name, start, _ in imported:
        (d["imports"] / name).write_bytes(b"audio")
        (d["imports"] / f"{name}.json").write_text(json.dumps({"file": name, "start": iso(start)}))
    (d["labels"] / "enrollment.json").write_text(json.dumps([{"start": iso(WINDOW[0]), "end": iso(WINDOW[1])}]))

    db_path = tmp_path / "hearsay.sqlite"
    rebuild(d["raw"], db_path)
    restore(db_path, None)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + ASSEMBLE_SCHEMA + SPEAKERS_SCHEMA + MUSIC_SCHEMA + AFFECT_SCHEMA
                     + IMPORTS_SCHEMA)
    db.executemany("INSERT INTO imported_audio VALUES (?,?,?,'sha','unused.pcm')", imported)
    for cid, start, made in (("c0", T0, transcript), ("c1", T1, c1_transcript)):
        wav = d["audio"] / f"{cid}.wav"
        sha, _ = write_wav(wav, TONE.to_bytes(2, "little") * (60 * SAMPLE_RATE), wav)
        db.execute("INSERT INTO conversations VALUES (?,?,?,30,0)", (cid, start, start + 60))
        db.execute("INSERT INTO conversation_audio VALUES (?,?,60,1,?,?,0)", (cid, iso(start), wav.name, sha))
        (d["transcripts"] / f"{cid}.json").write_text(json.dumps(made(cid, sha)))
    (d["transcripts"] / "failed").mkdir()
    (d["transcripts"] / "failed" / "c0.json").write_text("{}")
    db.commit()
    db.close()
    build_turns(db_path, d["transcripts"])
    db = sqlite3.connect(db_path)
    turns = db.execute("SELECT turn_id, conversation_id FROM turns ORDER BY turn_id").fetchall()
    db.executemany("INSERT INTO turn_speakers VALUES (?,?,1.0,?,NULL,NULL,NULL,NULL,NULL)",
                   [(turn_id, cid, unit(i)) for i, (turn_id, cid) in enumerate(turns)])
    db.commit()
    db.close()
    assert seal(db_path, d["records"], d["transcripts"], d["labels"], now=NOW)["sealed"] == 2

    (d["labels"] / "forget.jsonl").write_text("".join(json.dumps(
        {"type": "forget", "at": [iso(s), iso(e)], "reason": "", "tagged_at": iso(NOW)}) + "\n" for s, e in STRETCHES))
    forget(db_path, d["labels"])
    return d, db_path, uploads


def files_in(*folders):
    """Every file under the folders, by its path from their parent."""
    return {str(p.relative_to(f.parent)) for f in folders for p in f.rglob("*") if p.is_file()}


def kept_files(d):
    return files_in(d["raw"], d["imports"], d["audio"], d["transcripts"])


def raw_files(path, raw="raw"):
    return {f"{raw}/{path}", f"{raw}/{path[:-len('.body')]}.json"}


def import_files(name, imports="imports"):
    return {f"{imports}/{name}", f"{imports}/{name}.json"}


def c1_record(d):
    return json.loads((d["records"] / "conversations" / "c1.json").read_text())


def test_a_thrown_out_stretch_deletes_the_raw_wholly_inside_it_and_is_cut_from_what_s_kept(tmp_path):
    d, db_path, uploads = world(tmp_path)
    env = env_of(d, db_path)
    found = retention.plan(env, d["records"], NOW)
    assert {(x.kind, x.name) for x in found.deletions if x.why == "thrown out"} == {
        ("upload", uploads["inside"]), ("import", "inside.m4a")}
    # Uploads and imports reaching in stay until retention, so an unseal still has the audio around the stretch.
    assert found.kept == {"reaching into a thrown-out stretch, until retention": 2, "covering an enrollment window": 2}
    before = kept_files(d)
    original = c1_record(d)
    transcript_file = json.loads((d["transcripts"] / "c1.json").read_text())

    retention.run(env, now=NOW)

    # Exactly these, with what retention deletes (test below).
    assert before - kept_files(d) == (
        raw_files(uploads["inside"]) | import_files("inside.m4a") | raw_files(uploads["old"]) | import_files("old.m4a")
        | {"audio/c0.wav", "transcripts/c0.json", "transcripts/failed/c0.json"})

    # The stretches, and all of the turns they removed, are silent in the WAV; the rest is untouched.
    wav = d["audio"] / "c1.wav"
    with wave.open(str(wav)) as w:
        samples = struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes()))
    silent = [i / SAMPLE_RATE for i, s in enumerate(samples) if s == 0]
    assert (min(silent), max(silent)) == (pytest.approx(3.9, abs=1e-3), pytest.approx(26.5, abs=1e-3))
    assert len(silent) == pytest.approx((8.0 - 3.9 + 26.5 - 24.5) * SAMPLE_RATE, abs=4)
    assert all(s in (0, TONE) for s in samples)
    record = c1_record(d)
    assert record["audio"]["wav_sha256"] == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert rows(db_path, "SELECT wav_sha256 FROM conversation_audio WHERE conversation_id = 'c1'") == [
        (record["audio"]["wav_sha256"],)]
    assert rows(db_path, "SELECT conversation_id FROM conversation_audio WHERE wav_file IS NULL") == [("c0",)]

    # The removed turns' words are blank, with their timings and speakers, and their vectors are gone.
    assert set(record["turns"]) == set(original["turns"]) - {"c1:0001", "c1:0004"}
    segments = record["transcript"]["segments"]
    assert [w["word"] for w in segments[1]["words"]] == ["", "", "so", "so", "anyway"]
    assert segments[1]["text"] == "so so anyway"
    assert [{k: v for k, v in w.items() if k != "word"} for w in segments[1]["words"]] == [
        {k: v for k, v in w.items() if k != "word"} for w in original["transcript"]["segments"][1]["words"]]
    assert segments[3]["text"] == ""
    assert segments[0] == original["transcript"]["segments"][0] and segments[2] == original["transcript"]["segments"][2]
    assert record["discarded"] == [list(s) for s in rows(db_path, "SELECT start, end FROM forgotten ORDER BY start")]
    # The dev box's file is cut the same, still describing the WAV and settings it was made from.
    cut = json.loads((d["transcripts"] / "c1.json").read_text())
    assert cut["segments"] == segments
    assert (cut["wav_sha256"], cut["settings"]) == (transcript_file["wav_sha256"], transcript_file["settings"])

    # What parsing made of a deleted upload still loads: its reading during the stretch, and its payload row.
    db_after = tmp_path / "after.sqlite"
    rebuild(d["raw"], db_after, sealed_payloads(d["records"])[0])
    assert rows(db_after, "SELECT payload_path, at FROM locations") == [(uploads["inside"], T1 + 5.5)]
    sql = "SELECT path, received_at, body_sha256, first_at, last_at FROM payloads ORDER BY path"
    assert rows(db_after, sql) == rows(db_path, sql)


def turns_from(records_dir, d, path):
    """The turns a reprocess builds from the records, with throw-outs applied."""
    restore(path, records_dir)
    db = sqlite3.connect(path)
    db.executescript(CONVERSATIONS_SCHEMA + ASSEMBLE_SCHEMA)
    db.execute("INSERT INTO conversations SELECT conversation_id, start, end, speech_seconds, 0 FROM sealed_conversations")
    db.execute("INSERT INTO conversation_audio SELECT conversation_id, zero_at, duration, coverage,"
               " conversation_id || '.wav', wav_sha256, 1 FROM sealed_conversations")
    db.commit()
    db.close()
    build_turns(path, d["transcripts"])
    forget(path, d["labels"])
    return rows(path, "SELECT * FROM turns ORDER BY turn_id")


def test_turns_built_from_a_cut_record_are_those_from_before_less_the_thrown_out(tmp_path):
    d, db_path, _ = world(tmp_path)
    shutil.copytree(d["records"], tmp_path / "records-before")
    retention.run(env_of(d, db_path), now=NOW)

    before = turns_from(tmp_path / "records-before", d, tmp_path / "before.sqlite")
    assert turns_from(d["records"], d, tmp_path / "after.sqlite") == before
    assert [(t[0], t[5]) for t in before if t[1] == "c1"] == [
        ("c1:0000", "hello there"), ("c1:0002", "so so anyway"), ("c1:0003", "later on")]


def snapshot(*folders):
    """Every file under the folders, with its bytes and when it was last written."""
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for f in folders for p in f.rglob("*") if p.is_file()}


def test_a_second_run_deletes_and_rewrites_nothing(tmp_path):
    d, db_path, _ = world(tmp_path)
    env = env_of(d, db_path)
    retention.run(env, now=NOW)
    after = snapshot(tmp_path)

    again = retention.plan(env, d["records"], NOW)
    assert (again.deletions, again.cuts) == ([], [])
    assert retention.run(env, now=NOW)["sealed conversations cut, thrown out"].startswith("0:")
    assert snapshot(tmp_path) == after


def test_the_report_says_what_the_next_run_would_do_and_changes_nothing(tmp_path):
    d, db_path, _ = world(tmp_path)
    env = env_of(d, db_path)
    before = snapshot(tmp_path)
    reported = retention.report(env, NOW)
    assert snapshot(tmp_path) == before
    assert reported["raw uploads deleted, thrown out"].startswith("1, ")
    assert retention.run(env, now=NOW) == reported
    # The database is still the one the run that sealed c0 and c1 built, with them unsealed;
    # the report seals only what's past the line, so it finds them cut already.
    assert retention.report(env, NOW)["sealed conversations cut, thrown out"].startswith("0:")


def test_a_transcript_naming_no_conversation_goes_and_the_rest_stay(tmp_path):
    d, db_path, _ = world(tmp_path)
    env = env_of(d, db_path)
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO conversation_audio VALUES ('c2', ?, 60, 1, 'c2.wav', 'sha-c2', 0)", (iso(NOW - DAY),))
    db.commit()
    db.close()
    # c2's, held; one made before boundaries moved, and its failure marker; one named in Omi's days.
    orphans = {"c20261015T101500Z.json", "failed/c20261015T101500Z.json", "055e16ff-892c-4491-81f5-289cfd8311f1.json"}
    for name in {"c2.json"} | orphans:
        (d["transcripts"] / name).write_text("{}")

    found = retention.plan(env, d["records"], NOW)
    assert {x.name for x in found.deletions if x.why == "no conversation"} == orphans
    assert retention.summary(found)["transcript files deleted, no conversation"].startswith("3, ")
    retention.run(env, now=NOW)
    # c1 is sealed and less than 60 days old, c2 isn't sealed; c0's went past retention.
    assert files_in(d["transcripts"]) == {"transcripts/c1.json", "transcripts/c2.json"}
    assert {e["name"]: e["end"] for e in records.deleted(d["records"]) if e["why"] == "no conversation"} == {
        name: None for name in orphans}


def test_retention_deletes_audio_60_days_after_its_conversation_but_the_owner_s_enrollment(tmp_path):
    d, db_path, uploads = world(tmp_path)
    env = env_of(d, db_path)
    found = retention.plan(env, d["records"], NOW)
    assert {(x.kind, x.name) for x in found.deletions if x.why == "retention"} == {
        ("upload", uploads["old"]), ("import", "old.m4a"), ("wav", "c0.wav"), ("transcript", "c0.json"),
        ("transcript", "failed/c0.json")}
    # Audio covering the enrollment window, and Omi's text, stay for good.
    summary = retention.summary(found)
    assert summary["raw uploads and imports kept, covering an enrollment window"] == "2"
    assert summary["raw uploads deleted, retention"].startswith("1, ")

    # A conversation's WAV and transcripts go RETAIN after it ends, not before.
    c0_end = T0 + 60
    for now, gone in ((c0_end + RETAIN, set()), (c0_end + RETAIN + 1, {"c0.wav", "c0.json", "failed/c0.json"})):
        found = retention.plan(env, d["records"], now)
        assert {x.name for x in found.deletions if x.kind in ("wav", "transcript")} == gone


def test_a_deleted_upload_stays_loaded_from_its_digest_across_an_unseal(tmp_path):
    d, db_path, uploads = world(tmp_path)
    retention.run(env_of(d, db_path), now=NOW)
    assert unseal(d["records"], T1 + 30, now=NOW) == {"unsealed": 1, "line": records.iso(T0 + 60)}

    # c1 is redone from raw: the deleted upload is read from what parsing made of it, never opened.
    db_after = tmp_path / "after.sqlite"
    sealed = sealed_payloads(d["records"])[0]
    assert uploads["inside"] in sealed and uploads["edge"] not in sealed
    rebuild(d["raw"], db_after, sealed)
    assert (uploads["inside"], 1) in rows(db_after, "SELECT path, sealed FROM payloads")
    assert rows(db_after, "SELECT payload_path FROM locations") == [(uploads["inside"],)]


def write_record(records_dir, cid, start, end):
    folder = records_dir / "conversations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{cid}.json").write_text(json.dumps(
        {"conversation": {"conversation_id": cid, "start": start, "end": end, "speech_seconds": 30}}))


def test_unsealing_never_reaches_audio_retention_may_have_deleted(tmp_path):
    cutoff = NOW - RETAIN
    # One conversation runs across the retention line, the next is a day after it.
    write_record(tmp_path, "across", cutoff - 3600, cutoff + 3600)
    write_record(tmp_path, "after", cutoff + DAY, cutoff + DAY + 60)
    (tmp_path / "seal.json").write_text(json.dumps({"line": cutoff + DAY + 60, "sealed_at": NOW, "imports": []}))

    with pytest.raises(ValueError, match="can't move back that far"):
        unseal(tmp_path, cutoff - 1, now=NOW)
    # Past the line, but it would redo the conversation that began before it from raw.
    with pytest.raises(ValueError, match="would redo across from raw"):
        unseal(tmp_path, cutoff + 3600 + GAP / 2, now=NOW)
    assert sorted(p.stem for p in (tmp_path / "conversations").iterdir()) == ["across", "after"]

    assert unseal(tmp_path, cutoff + DAY, now=NOW) == {"unsealed": 1, "line": records.iso(cutoff + 3600)}


def test_the_unseal_command_says_why_it_refuses(tmp_path, monkeypatch, capsys):
    for name in reprocess.DIRS:
        monkeypatch.setenv(f"HEARSAY_{name.upper()}", str(tmp_path / name))
    (tmp_path / "records_dir" / "conversations").mkdir(parents=True)
    monkeypatch.setattr("sys.argv", ["records", "unseal", "2020-01-01"])
    with pytest.raises(SystemExit, match="Not unsealed: .* can't move back that far"):
        records.main()


def test_audio_arriving_late_behind_the_retention_line_is_reported_and_left_out(tmp_path):
    d, db_path, _ = world(tmp_path)
    cutoff = NOW - RETAIN
    for name, start in (("ancient.m4a", cutoff - DAY), ("recent.m4a", T1 - 3600)):
        (d["imports"] / f"{name}.json").write_text(json.dumps({"file": name, "start": iso(start)}))
    # An enrollment window added long after its audio went.
    (d["labels"] / "enrollment.json").write_text(json.dumps(
        [{"start": iso(WINDOW[0]), "end": iso(WINDOW[1])}, {"start": iso(cutoff - 2 * DAY), "end": iso(cutoff - DAY)}]))

    when, ignored = late(db_path, d["records"], d["imports"], d["labels"], now=NOW)
    assert when == T1 - 3600
    assert set(ignored) == {"import ancient.m4a", f"enrollment window {iso(WINDOW[0])} to {iso(WINDOW[1])}",
                            f"enrollment window {iso(cutoff - 2 * DAY)} to {iso(cutoff - DAY)}"}

    # With only those, nothing is unsealed.
    (d["imports"] / "recent.m4a.json").unlink()
    assert late(db_path, d["records"], d["imports"], d["labels"], now=NOW)[0] is None


def test_an_import_older_than_retention_is_refused(tmp_path, monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("nothing reaches the NAS")

    monkeypatch.setattr(label, "ssh", unreachable)
    recording = tmp_path / "old.mp3"
    recording.write_bytes(b"audio")
    began = datetime.fromtimestamp(time.time() - RETAIN - DAY, timezone.utc).isoformat()
    with pytest.raises(SystemExit, match="days ago"):
        label.import_audio(str(recording), began)


def test_the_model_cache_drops_what_no_run_has_used_for_the_hold(tmp_path, monkeypatch):
    path = tmp_path / "cache.sqlite"
    old = sqlite3.connect(path)  # made before entries noted their use: never used, as far as it knows
    old.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
    old.execute("INSERT INTO kv VALUES ('vad:before', x'00')")
    old.commit()
    old.close()
    day = 20000
    monkeypatch.setattr(cache, "today", lambda: day)
    store = cache.Cache(path)
    store.put("vad:sealed", b"1")
    store.put("vad:held", b"2")
    day += HOLD // DAY + 1
    assert store.get("vad:held") == b"2"  # a run still reads its audio
    assert store.prune(HOLD) == 2
    assert (store.get("vad:sealed"), store.get("vad:before"), store.get("vad:held")) == (None, None, b"2")
    store.close()


def bursts(raw, first, start, count):
    """count of Omi's 5 s bursts laid end to end from start, numbered from first."""
    for k in range(count):
        audio_burst(raw, first + k, datetime.fromtimestamp(start + 5 * (k + 1), timezone.utc))


def test_a_reprocess_deletes_exactly_what_was_thrown_out_or_is_past_retention(tmp_path, monkeypatch):
    env = {name: tmp_path / name for name in reprocess.DIRS if name != "db"}
    for path in env.values():
        path.mkdir()
    env["db"] = tmp_path / "hearsay.sqlite"
    now = float(int(time.time()))
    # c0 is past retention, c1 and c2 have settled, c3 is still held.
    starts = {"c0": now - 70 * DAY, "c1": now - 20 * DAY, "c2": now - 16 * DAY, "c3": now - DAY}
    for i, start in enumerate(starts.values()):
        bursts(env["raw_dir"], 8 * i, start, 8)
    speech = [(start + 1, start + 41) for start in starts.values()]
    monkeypatch.setattr(conversations, "detect_speech", lambda raw_dir, found, cache=None: speech)

    def no_model(schema):
        def run(raw_dir, db_path, *args, **kwargs):
            db = sqlite3.connect(db_path)
            db.executescript(schema)
            db.close()
            return {}
        return run

    monkeypatch.setattr(reprocess, "label_speakers", no_model(speakers.SCHEMA))
    monkeypatch.setattr(reprocess, "score_music", no_model(MUSIC_SCHEMA))
    monkeypatch.setattr(reprocess, "score_affect", no_model(AFFECT_SCHEMA))
    c0, c1 = starts["c0"], starts["c1"]
    # The owner's enrollment, during c0; an import and Omi's text from c0's day.
    window = (c0 + 1, c0 + 3)
    (env["labels_dir"] / "enrollment.json").write_text(json.dumps([{"start": iso(window[0]), "end": iso(window[1])}]))
    export = b"synthetic export"
    (env["imports_dir"] / "old.m4a").write_bytes(export)
    (env["imports_dir"] / "old.m4a.json").write_text(json.dumps(
        {"file": "old.m4a", "start": iso(c0 + 60), "sha256": hashlib.sha256(export).hexdigest()}))
    reprocess.imports_pcm(env).mkdir(parents=True)
    (reprocess.imports_pcm(env) / f"{hashlib.sha256(export).hexdigest()}.pcm").write_bytes(bytes(64000))
    write_payload(env["raw_dir"], "transcript", datetime.fromtimestamp(c0 + 10, timezone.utc),
                  json.dumps({"segments": [segment("s1", 0.0, 2.0)]}).encode(), [["uid", "u"]])

    reprocess.run(env)
    # The dev box transcribes what was cut: c1 has a turn on each side of the stretch thrown out.
    audio = {cid: (zero, sha) for cid, zero, sha in rows(env["db"], "SELECT conversation_id, zero_at, wav_sha256"
                                                         " FROM conversation_audio ORDER BY conversation_id")}
    ids = sorted(audio)
    assert len(ids) == 4
    for cid, start in zip(ids[:3], starts.values()):
        zero = datetime.fromisoformat(audio[cid][0]).timestamp()
        said = [("kept", 2, 4, "SPEAKER_00"), ("thrown", 6, 9, "SPEAKER_01"), ("after", 12, 14, "SPEAKER_00")]
        segments = [{"start": s + start - zero, "end": e + start - zero, "text": w, "speaker": who,
                     "words": [word(w, s + start - zero, e + start - zero, who)]} for w, s, e, who in said]
        (env["transcripts_dir"] / f"{cid}.json").write_text(json.dumps(
            {"conversation_id": cid, "wav_sha256": audio[cid][1], "settings": settings_for([]), "segments": segments}))
    (env["labels_dir"] / "forget.jsonl").write_text(json.dumps(
        {"type": "forget", "at": [iso(c1 + 5.5), iso(c1 + 10)], "reason": "", "tagged_at": iso(now)}) + "\n")
    # Made before the conversation's boundaries moved: no conversation has its id now.
    (env["transcripts_dir"] / "c20261001T000000Z.json").write_text("{}")
    kept = (env["raw_dir"], env["imports_dir"], env["audio_dir"], env["transcripts_dir"])
    before = files_in(*kept)

    reprocess.run(env)

    gone = before - files_in(*kept)
    deleted = records.deleted(env["records_dir"])
    uploads = {e["name"]: e["why"] for e in deleted if e["kind"] == "upload"}
    assert gone == ({f for name in uploads for f in raw_files(name, "raw_dir")} | import_files("old.m4a", "imports_dir")
                    | {f"audio_dir/{ids[0]}.wav", f"transcripts_dir/{ids[0]}.json",
                       "transcripts_dir/c20261001T000000Z.json"})
    # c0's 40 s of audio but the two seconds in the enrollment window; Omi's text stays.
    assert list(uploads.values()).count("retention") == 40 - 2
    assert len(list((env["raw_dir"] / "transcript").rglob("*.body"))) == 1
    # c1's four seconds wholly inside the stretch, not the one reaching into it.
    thrown = sorted(e["end"] - c1 for e in deleted if e["why"] == "thrown out")
    assert thrown == pytest.approx([7, 8, 9, 10])

    # A run with nothing new deletes and rewrites nothing, and nothing decoded from deleted audio is left.
    after = snapshot(*kept, env["records_dir"])
    reprocess.run(env)
    assert snapshot(*kept, env["records_dir"]) == after
    assert list(reprocess.imports_pcm(env).glob("*.pcm")) == []
    # c0 has no audio now (the portal says so); c1 keeps the turns either side of the stretch.
    assert rows(env["db"], "SELECT conversation_id FROM conversation_audio WHERE wav_file IS NULL") == [(ids[0],)]
    assert rows(env["db"], f"SELECT text FROM turns WHERE conversation_id = '{ids[1]}' ORDER BY idx") == [
        ("kept",), ("after",)]

    # A build from records still equals one from raw, wherever raw is whole.
    assert records.check(env) == 0

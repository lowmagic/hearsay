import json
import sqlite3
import struct
from array import array
from datetime import datetime, timedelta, timezone

import pytest

from hearsay import conversations
from hearsay.affect import SCHEMA as AFFECT_SCHEMA, score_affect
from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA, assemble
from hearsay.capture import ACTION, BUTTON, LOCATION, load_captures
from hearsay.conversations import GAP, SCHEMA as CONVERSATIONS_SCHEMA, find_conversations
from hearsay.delivery import measure_delivery
from hearsay.imports import SCHEMA as IMPORTS_SCHEMA, load_imports
from hearsay.music import SCHEMA as MUSIC_SCHEMA, score_music
from hearsay.parse import rebuild
from hearsay.records import SETTLE, late, restore, seal, sealed_payloads, unseal
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA, label_speakers, owner_voice
from hearsay.transcribe import settings_for
from hearsay.turns import build_turns, record_operator_input

from test_capture import record, upload

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp()
T0 = NOW - 10 * 24 * 3600  # c1 starts here; c2 and c3 a day apart after it
C3_END = T0 + 2 * 86400 + 60


def window(start, end):
    return [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in (start, end)]


WINDOW = window(T0 - 3600, T0 - 3540)


def enroll(labels_dir, *windows):
    (labels_dir / "enrollment.json").write_text(json.dumps([{"start": s, "end": e} for s, e in windows]))


def sealed_ids(records_dir):
    return sorted(p.stem for p in (records_dir / "conversations").iterdir())


def unit(i):
    v = [0.0] * 192
    v[i] = 1.0
    return array("f", v).tobytes()


def word(text, start, end, speaker):
    return {"word": text, "start": start, "end": end, "score": 0.9, "speaker": speaker}


def transcript(conversation_id, wav_sha256):
    # Three turns: SPEAKER_00, SPEAKER_01, then SPEAKER_00 again after a pause.
    return {"conversation_id": conversation_id, "wav_sha256": wav_sha256, "settings": settings_for([]), "segments": [
        {"start": 1.0, "end": 3.5, "text": "hello there", "speaker": "SPEAKER_00",
         "words": [word("hello", 1.0, 1.5, "SPEAKER_00"), word("there", 1.6, 3.5, "SPEAKER_00")]},
        {"start": 4.0, "end": 7.0, "text": "hi back", "speaker": "SPEAKER_01",
         "words": [word("hi", 4.0, 4.5, "SPEAKER_01"), word("back", 4.6, 7.0, "SPEAKER_01")]},
        {"start": 10.0, "end": 12.6, "text": "so so anyway", "speaker": "SPEAKER_00",
         "words": [word("so", 10.0, 10.3, "SPEAKER_00"), word("so", 10.4, 10.7, "SPEAKER_00"),
                   word("anyway", 10.8, 12.6, "SPEAKER_00")]},
    ]}


def dirs(tmp_path):
    names = ("raw", "audio", "labels", "transcripts", "imports", "records", "pcm")
    found = {name: tmp_path / name for name in names}
    for d in found.values():
        d.mkdir()
    return found


def first_build(tmp_path, d):
    """What a reprocess from raw would have built: c1 settled and transcribed, c2
    waiting for the dev box, c3 settled; their audio results as the models gave them."""
    tap = struct.pack("<ii", 1, 0)
    # An upload during c1 (a start mark and a reading), and one a day ago.
    upload(d["raw"], datetime.fromtimestamp(T0 + 120, timezone.utc), "k-c1", b"".join([
        record(T0 + 2, BUTTON, tap), record(T0 + 2, ACTION, bytes([1, 1])),
        record(T0 + 5, LOCATION, struct.pack("<ddf", -26.12, 80.14, 65.0))]))
    upload(d["raw"], datetime.fromtimestamp(NOW - 86400, timezone.utc), "k-new",
           record(NOW - 86400, LOCATION, struct.pack("<ddf", -26.12, 80.14, 65.0)))
    db_path = tmp_path / "first.sqlite"
    rebuild(d["raw"], db_path)
    restore(db_path, None)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + ASSEMBLE_SCHEMA + SPEAKERS_SCHEMA + MUSIC_SCHEMA + AFFECT_SCHEMA
                     + IMPORTS_SCHEMA)
    for i, cid in enumerate(("c1", "c2", "c3")):
        start = T0 + i * 86400
        db.execute("INSERT INTO conversations VALUES (?,?,?,30,0)", (cid, start, start + 60))
        db.execute("INSERT INTO conversation_audio VALUES (?,?,60,1,?,?,0)",
                   (cid, datetime.fromtimestamp(start, timezone.utc).isoformat(), f"{cid}.wav", f"sha-{cid}"))
        (d["audio"] / f"{cid}.wav").write_bytes(b"kept until retention")
        if cid != "c2":
            (d["transcripts"] / f"{cid}.json").write_text(json.dumps(transcript(cid, f"sha-{cid}")))
    db.execute("INSERT INTO conversation_taps VALUES ('c1', ?)", (T0 + 2,))
    db.commit()
    db.close()
    build_turns(db_path, d["transcripts"])
    db = sqlite3.connect(db_path)
    for cid in ("c1", "c3"):
        # The owner's voice, someone else's, and a short turn the Mac's mic said was the owner's.
        db.executemany("INSERT INTO turn_speakers VALUES (?,?,1.0,?,NULL,NULL,NULL,?,?)", [
            (f"{cid}:0000", cid, unit(0), None, None), (f"{cid}:0001", cid, unit(1), None, None),
            (f"{cid}:0002", cid, None, unit(2), "owner")])
        db.executemany("INSERT INTO turn_music VALUES (?,0.9,0.01,0.1)", [(f"{cid}:{i:04d}",) for i in range(3)])
        db.executemany("INSERT INTO turn_affect VALUES (?,?,0.5,0.5)", [(f"{cid}:0000", 0.7), (f"{cid}:0001", 0.2)])
    db.execute("INSERT INTO owner_enrollment VALUES (?,?,1.0,1)", WINDOW)
    db.execute("INSERT INTO enrollment_pieces VALUES (?,?,?)", (*WINDOW, unit(0)))
    db.commit()
    db.close()
    enroll(d["labels"], WINDOW)
    return db_path


def rows(db_path, sql):
    db = sqlite3.connect(db_path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_a_settled_conversation_is_sealed_in_order_and_rebuilds_from_its_record_alone(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    # c2 waits for the dev box, so c3 waits for it.
    assert seal(first, d["records"], d["transcripts"], d["labels"], now=NOW) == {"sealed": 1, "uploads": 1, "line":
                                                                   datetime.fromtimestamp(T0 + 60).isoformat(timespec="minutes")}
    assert sealed_ids(d["records"]) == ["c1"]

    # Rebuild with nothing but the records behind the line: the sealed upload's
    # file and c1's transcript are no longer read.
    sealed, imports = sealed_payloads(d["records"])
    [path] = sealed
    (d["raw"] / path).write_bytes(b"gone")
    (d["transcripts"] / "c1.json").write_text(json.dumps(transcript("c1", "replaced by a new vocabulary")))
    second = tmp_path / "second.sqlite"
    rebuild(d["raw"], second, sealed)
    restore(second, d["records"])
    load_imports(d["imports"], second, d["pcm"], imports)
    load_captures(d["raw"], second, d["pcm"])
    find_conversations(d["raw"], second)
    assemble(d["raw"], second, d["audio"])
    build_turns(second, d["transcripts"])
    record_operator_input(second, d["labels"])
    # No speaker, music or tone model is loaded: everything they'd give is in the record.
    label_speakers(d["raw"], second, d["labels"], tmp_path / "no-model")
    score_music(d["raw"], second, tmp_path / "no-model")
    score_affect(d["raw"], second, tmp_path / "no-model")
    measure_delivery(second, d["transcripts"])

    for sql in ("SELECT * FROM conversations WHERE conversation_id = 'c1'",
                "SELECT * FROM conversation_taps",
                "SELECT conversation_id, zero_at, duration, coverage, wav_file, wav_sha256 FROM conversation_audio"
                " WHERE conversation_id = 'c1'",
                "SELECT * FROM turns WHERE conversation_id = 'c1'",
                "SELECT turn_id, coverage, embedding, veto_embedding, channel FROM turn_speakers"
                " WHERE conversation_id = 'c1'",
                "SELECT * FROM turn_music WHERE turn_id LIKE 'c1:%'",
                "SELECT * FROM marks", "SELECT * FROM locations"):
        assert rows(second, sql) == rows(first, sql), sql
    assert rows(second, "SELECT sealed FROM conversation_audio") == [(1,)]
    # Who spoke re-runs from what was kept: the enrolled voice, the other voice, the Mac's channel.
    assert rows(second, "SELECT turn_id, owner_similarity, label, basis FROM turn_speakers ORDER BY turn_id") == [
        ("c1:0000", 1.0, "owner", "voice"), ("c1:0001", 0.0, "not_owner", "voice"), ("c1:0002", None, "owner", "channel")]
    # Tone only for turns now the owner's, and only as kept: the short one never had a score.
    assert rows(second, "SELECT turn_id, arousal FROM turn_affect") == [("c1:0000", 0.7)]
    assert rows(second, "SELECT turn_id FROM turn_delivery ORDER BY turn_id") == [("c1:0000",), ("c1:0002",)]
    assert rows(second, "SELECT turn_id, kind FROM turn_stutters") == [("c1:0002", "repeat")]


def test_audio_landing_behind_the_line_unseals_back_to_it(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    # An import of two hours before c1, there when sealing.
    (d["imports"] / "known.m4a.json").write_text(json.dumps(
        {"file": "known.m4a", "start": datetime.fromtimestamp(T0 - 7200, timezone.utc).isoformat()}))
    db = sqlite3.connect(first)
    db.execute("INSERT INTO imported_audio VALUES ('known.m4a', ?, 60, 'sha-known', 'known.pcm')", (T0 - 7200,))
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    # Neither that import, nor the enrollment window, nor the upload a day ago (past the line) is late.
    assert late(first, d["records"], d["imports"], d["labels"]) is None

    # An import of an hour before c1, made after sealing.
    start = datetime.fromtimestamp(T0 - 1800, timezone.utc)
    (d["imports"] / "old.m4a").write_bytes(b"audio")
    (d["imports"] / "old.m4a.json").write_text(json.dumps({"file": "old.m4a", "start": start.isoformat()}))
    assert late(first, d["records"], d["imports"], d["labels"]) == T0 - 1800

    assert unseal(d["records"], T0 - 1800) == {"unsealed": 1, "line": None}
    assert not list((d["records"] / "conversations").iterdir())
    assert sealed_payloads(d["records"]) == ({}, set())
    assert json.loads((d["records"] / "seal.json").read_text())["line"] == 0.0


def test_an_enrollment_window_added_behind_the_line_unseals_back_to_it(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)

    # Enrolled after sealing, from a reading two hours before c1: a build from records no longer reads its audio.
    added = window(T0 - 7200, T0 - 7140)
    enroll(d["labels"], WINDOW, added)
    assert late(first, d["records"], d["imports"], d["labels"]) == T0 - 7200
    unseal(d["records"], T0 - 7200)

    # The next run cuts it from raw, and sealing keeps it with the window enrolled before.
    db = sqlite3.connect(first)
    db.execute("INSERT INTO owner_enrollment VALUES (?,?,1.0,1)", added)
    db.execute("INSERT INTO enrollment_pieces VALUES (?,?,?)", (*added, unit(3)))
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    assert late(first, d["records"], d["imports"], d["labels"]) is None
    restored = tmp_path / "restored.sqlite"
    restore(restored, d["records"])
    assert rows(restored, "SELECT start, end, vector FROM sealed_enrollment ORDER BY start") == [
        (*added, unit(3)), (*WINDOW, unit(0))]


def test_an_enrollment_window_is_sealed_once_any_of_it_is_behind_the_line(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    # One running past c1's end, where the line will be, and one with no usable piece.
    across, empty = window(T0 + 30, T0 + 90), window(T0 - 600, T0 - 540)
    enroll(d["labels"], WINDOW, across, empty)
    db = sqlite3.connect(first)
    db.executemany("INSERT INTO owner_enrollment VALUES (?,?,?,?)", [(*across, 1.0, 1), (*empty, 0.2, 0)])
    db.execute("INSERT INTO enrollment_pieces VALUES (?,?,?)", (*across, unit(3)))
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)

    # The audio behind the line isn't read, so all three come from the records as the first build had them.
    second = tmp_path / "second.sqlite"
    restore(second, d["records"])
    db = sqlite3.connect(second)
    db.executescript(SPEAKERS_SCHEMA)
    owner_voice(None, d["raw"], [], d["labels"], db)
    db.commit()
    db.close()
    sql = "SELECT start, end, coverage, pieces FROM owner_enrollment ORDER BY start"
    assert rows(second, sql) == rows(first, sql)
    assert rows(second, "SELECT start, vector FROM enrollment_pieces ORDER BY start") == [
        (WINDOW[0], unit(0)), (across[0], unit(3))]


def test_speech_landing_less_than_a_gap_after_the_line_joins_the_sealed_conversation_as_from_raw(
        tmp_path, monkeypatch):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)  # the line is at c1's end
    # A recording imported after sealing, starting past the line but less than GAP after c1's last speech.
    landed = T0 + 60 + GAP / 2
    (d["imports"] / "later.m4a.json").write_text(json.dumps(
        {"file": "later.m4a", "start": datetime.fromtimestamp(landed, timezone.utc).isoformat()}))
    assert late(first, d["records"], d["imports"], d["labels"]) == landed
    assert unseal(d["records"], landed)["unsealed"] == 1

    # c1's 30 s of speech, then the recording's minute: from raw, one conversation.
    speech = [(T0 + 1, T0 + 16), (T0 + 44, T0 + 59), (landed, landed + 60)]
    monkeypatch.setattr(conversations, "detect_speech", lambda raw_dir, bursts, cache=None: speech)

    def found(records_dir):
        db_path = tmp_path / ("from-records.sqlite" if records_dir else "from-raw.sqlite")
        rebuild(d["raw"], db_path, sealed_payloads(records_dir)[0])
        restore(db_path, records_dir)
        find_conversations(d["raw"], db_path)
        return rows(db_path, "SELECT start, end, speech_seconds FROM conversations")

    assert found(d["records"]) == found(None) == [(T0, landed + 61, 90.0)]

    # The next run finds nothing late, nor does one after the line passes the recording again.
    assert late(first, d["records"], d["imports"], d["labels"]) is None
    db = sqlite3.connect(first)
    db.execute("INSERT INTO imported_audio VALUES ('later.m4a', ?, 60, 'sha-later', 'later.pcm')", (landed,))
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    assert late(first, d["records"], d["imports"], d["labels"]) is None


@pytest.mark.parametrize("now, opened, sealed", [
    (C3_END + SETTLE - 1, (), ["c1", "c2"]),  # c3 ended a second short of SETTLE ago
    (C3_END + SETTLE, (), ["c1", "c2", "c3"]),
    (NOW, ("c2",), ["c1"]),  # c2's audio ends less than GAP after its speech
])
def test_sealing_stops_before_a_conversation_still_settling_or_open(tmp_path, now, opened, sealed):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    (d["transcripts"] / "c2.json").write_text(json.dumps(transcript("c2", "sha-c2")))
    db = sqlite3.connect(first)
    db.executemany("UPDATE conversations SET open = 1 WHERE conversation_id = ?", [(cid,) for cid in opened])
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=now)
    assert sealed_ids(d["records"]) == sealed


def test_a_record_past_the_line_is_not_restored(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    line = (d["records"] / "seal.json").read_text()
    # The next run seals c2 and c3, but stops before writing seal.json.
    (d["transcripts"] / "c2.json").write_text(json.dumps(transcript("c2", "sha-c2")))
    db = sqlite3.connect(first)
    db.execute("UPDATE conversation_audio SET sealed = 1 WHERE conversation_id = 'c1'")
    db.commit()
    db.close()
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    (d["records"] / "seal.json").write_text(line)

    restored = tmp_path / "restored.sqlite"
    restore(restored, d["records"])
    assert rows(restored, "SELECT conversation_id FROM sealed_conversations") == [("c1",)]


def test_unsealing_to_between_conversations_keeps_the_records_before(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    (d["transcripts"] / "c2.json").write_text(json.dumps(transcript("c2", "sha-c2")))
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    assert sealed_ids(d["records"]) == ["c1", "c2", "c3"]

    assert unseal(d["records"], T0 + 86400) == {
        "unsealed": 2, "line": datetime.fromtimestamp(T0 + 60).isoformat(timespec="minutes")}
    assert sealed_ids(d["records"]) == ["c1"]
    assert json.loads((d["records"] / "seal.json").read_text())["line"] == T0 + 60
    # c1's upload and the enrollment window stay sealed with it.
    assert len(sealed_payloads(d["records"])[0]) == 1
    restored = tmp_path / "restored.sqlite"
    restore(restored, d["records"])
    assert rows(restored, "SELECT start, end FROM sealed_enrollment") == [tuple(WINDOW)]
    # Unsealing to past the line plus GAP unseals nothing.
    assert unseal(d["records"], T0 + 60 + GAP + 1) == {"unsealed": 0}
    assert sealed_ids(d["records"]) == ["c1"]


def test_sealing_waits_for_the_current_vocabulary_but_not_for_a_conversation_that_failed(tmp_path):
    d = dirs(tmp_path)
    first = first_build(tmp_path, d)
    seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)
    db = sqlite3.connect(first)
    db.execute("UPDATE conversation_audio SET sealed = 1 WHERE conversation_id = 'c1'")  # as the next run has it
    db.commit()
    db.close()

    # c2 failed for its WAV, but with words since committed it'll be retried: still waiting.
    (d["transcripts"] / "failed").mkdir()
    (d["transcripts"] / "failed" / "c2.json").write_text(json.dumps(
        {"conversation_id": "c2", "wav_sha256": "sha-c2", "settings": settings_for([]), "error": "boom"}))
    (d["labels"] / "words.json").write_text(json.dumps({"typed": ["Xanthe"], "lattice": []}))
    assert seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)["sealed"] == 0
    # Without the words it won't be retried, so it seals untranscribed, and c3 with it.
    (d["labels"] / "words.json").unlink()
    assert seal(first, d["records"], d["transcripts"], d["labels"], now=NOW)["sealed"] == 2
    record = json.loads((d["records"] / "conversations" / "c2.json").read_text())
    assert record["transcript"] is None and record["turns"] == {}

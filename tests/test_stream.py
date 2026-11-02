import json
import sqlite3

from hearsay.affect import SCHEMA as AFFECT_SCHEMA
from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.delivery import SCHEMA as DELIVERY_SCHEMA
from hearsay.forget import SCHEMA as FORGET_SCHEMA
from hearsay.hold import HOLD, SCHEMA as HOLD_SCHEMA
from hearsay.media import SCHEMA as MEDIA_SCHEMA
from hearsay.music import SCHEMA as MUSIC_SCHEMA
from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.parse import SCHEMA as PARSE_SCHEMA
from hearsay.places import SCHEMA as PLACES_SCHEMA
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.stream import write_stream
from hearsay.turns import SCHEMA as TURNS_SCHEMA

T = 1_790_000_000.0  # unix seconds; conversation c1's WAV starts here

# (text, diarized voice, owner/not_owner label, owner similarity, the operator's
# name tag); a label is by voice unless the text says inherited.
TURNS = [
    ("mine", "S0", "owner", 0.71, None),
    ("walter's", "S1", "not_owner", 0.05, "Walter"),  # a name from before 2026-10-28: counts for nothing
    ("first anon", "S2", "not_owner", 0.08, None),
    ("second anon", "S3", "not_owner", 0.02, None),
    ("first anon again", "S2", "not_owner", 0.09, None),
    ("a cashier", "S4", "not_owner", 0.11, "_stranger"),
    ("a car alarm", None, "not_owner", 0.01, "_noise"),
    ("too short to tell", "S0", None, 0.25, None),
    ("yeah (inherited)", "S2", "not_owner", None, None),
]


def build(tmp_path):
    db_path = tmp_path / "h.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + CONVERSATIONS_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA
                    + PLACES_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA + MUSIC_SCHEMA + MEDIA_SCHEMA + FORGET_SCHEMA
                    + HOLD_SCHEMA)
    # The owner's tone of voice, scored on their turn only (hearsay/affect.py).
    db.execute("INSERT INTO turn_affect VALUES ('c1:0000', 0.42, 0.45, 0.6)")
    db.execute("INSERT INTO turn_delivery VALUES ('c1:0000', 4.1, 1, 0.6)")
    db.execute("INSERT INTO turn_stutters VALUES ('c1:0000', 2.0, 'repeat', 4.8, 1)")
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 60, 0)", (T, T + 100))
    db.execute("INSERT INTO conversations VALUES ('c2', ?, ?, 40, 1)", (T + 500, T + 560))  # not transcribed yet
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-09-21T14:13:20+00:00', 100, 1, 'c1.wav', 'sha', 0)")
    for idx, (text, voice, label, similarity, name) in enumerate(TURNS):
        turn_id = f"c1:{idx:04d}"
        db.execute("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, ?, ?, 0.8)", (turn_id, idx, 10.0 * idx, 10.0 * idx + 5, text, voice))
        basis = "diarization" if "inherited" in text else "voice" if label else None
        db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, NULL, ?, ?, ?, NULL, NULL)", (turn_id, similarity, label, basis))
        if name:
            db.execute("INSERT INTO operator_input VALUES (?, 'name', ?)", (turn_id, name))
    db.commit()
    db.close()
    return db_path


def read(stream_dir):
    index = json.loads((stream_dir / "index.json").read_text())["conversations"]
    lines = (stream_dir / "conversations/c1.jsonl").read_text().splitlines()
    return index, [json.loads(line) for line in lines]


def test_stream_attributes_speakers_and_leaves_out_non_speech(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    assert write_stream(db_path, stream_dir) == {"conversations": 2, "transcribed": 1, "utterances": 8, "held": 0}
    index, utterances = read(stream_dir)

    assert [(u["text"], u["speaker"]["kind"], u["speaker"]["name"], u["speaker"]["label"]) for u in utterances] == [
        ("mine", "owner", None, None),
        ("walter's", "anonymous", None, "anon A"),
        ("first anon", "anonymous", None, "anon B"),
        ("second anon", "anonymous", None, "anon C"),
        ("first anon again", "anonymous", None, "anon B"),
        ("a cashier", "stranger", None, "stranger A"),
        ("too short to tell", "unknown", None, None),
        ("yeah (inherited)", "anonymous", None, "anon B"),
    ]
    assert utterances[0]["start"] == "2026-09-21T14:13:20.000Z" and utterances[1]["end"] == "2026-09-21T14:13:35.000Z"
    assert utterances[0]["speaker_confidence"] == {"basis": "voice", "owner_similarity": 0.71}
    assert utterances[7]["speaker_confidence"] == {"basis": "diarization", "owner_similarity": None}
    assert utterances[0]["affect"] == {"arousal": 0.42}  # only arousal: dominance and valence aren't checked
    assert all("affect" not in u for u in utterances[1:])
    assert utterances[0]["delivery"] == {"words_per_second": 4.1, "pauses": 1, "pause_seconds": 0.6, "stutters": [
        {"kind": "repeat", "at": "2026-09-21T14:13:22.000Z", "words_per_second_before": 4.8, "word": 1}]}
    assert all("delivery" not in u for u in utterances[1:])
    assert [(e["conversation_id"], e["transcribed"], e["open"], e["utterances"]) for e in index] == [
        ("c1", True, False, 8), ("c2", False, True, 0)]


def test_a_song_s_words_leave_the_stream_unless_the_operator_says_otherwise(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    db = sqlite3.connect(db_path)
    db.executemany("INSERT INTO turn_music VALUES (?, ?, 0, ?)", [
        ("c1:0000", 0.25, 0.6),  # talked over loud music: too much speech to be the song
        ("c1:0002", 0.1, 0.6),   # the song
        ("c1:0003", 0.1, 0.6),   # scores as the song, but the operator heard talk
        ("c1:0004", 0.25, 0.1),  # scores as talk, but the operator heard the song
        ("c1:0005", 0.1, 0.6),   # scores as the song, but tagged someone present
    ])
    db.executemany("INSERT INTO operator_input VALUES (?, 'music', ?)", [("c1:0003", "talk"), ("c1:0004", "song")])
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    _, utterances = read(stream_dir)
    assert [u["text"] for u in utterances] == [
        "mine", "walter's", "second anon", "a cashier", "too short to tell", "yeah (inherited)"]


def test_turns_that_sound_like_tagged_media_leave_the_stream_unless_tagged(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    db = sqlite3.connect(db_path)
    db.executemany("INSERT INTO turn_media VALUES (?, ?)", [
        ("c1:0000", 0.95),  # the owner's by voice: never media
        ("c1:0001", 0.95),  # the TV
        ("c1:0002", 0.85),  # below the hint
        ("c1:0005", 0.95),  # sounds like the TV, but tagged someone present
    ])
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    _, utterances = read(stream_dir)
    assert [u["text"] for u in utterances] == [
        "mine", "first anon", "second anon", "first anon again", "a cashier", "too short to tell", "yeah (inherited)"]


def test_turns_the_operator_says_are_theirs_are_the_owners(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    db = sqlite3.connect(db_path)
    db.executemany("INSERT INTO operator_input VALUES (?, 'name', '_me')", [("c1:0002",), ("c1:0004",), ("c1:0008",)])
    # Loud on the call: the channel said someone else, so it isn't the basis.
    db.execute("UPDATE turn_speakers SET basis = 'channel' WHERE turn_id IN ('c1:0001', 'c1:0004')")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    _, utterances = read(stream_dir)
    found = {u["text"]: (u["speaker"]["kind"], u["speaker"]["label"], u["speaker_confidence"]["basis"])
             for u in utterances}
    assert found["first anon"] == ("owner", None, "named")
    assert found["first anon again"] == ("owner", None, "named")
    assert found["yeah (inherited)"] == ("owner", None, "diarization")
    assert found["walter's"] == ("anonymous", "anon A", "channel")
    assert found["second anon"] == ("anonymous", "anon B", "cluster")


def test_a_clip_s_tag_corrects_the_owner_but_a_speaker_s_tag_does_not(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    db = sqlite3.connect(db_path)
    # A speaker's tag put on a whole anonymous speaker never held the owner's turns.
    db.execute("INSERT INTO operator_input VALUES ('c1:0000', 'name', '_noise')")
    db.execute("INSERT INTO operator_input VALUES ('c1:0007', 'clip', '_me')")
    db.execute("INSERT INTO operator_input VALUES ('c1:0002', 'clip', '_stranger')")
    db.execute("INSERT INTO operator_input VALUES ('c1:0004', 'clip', '_mic')")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    _, utterances = read(stream_dir)
    found = {u["text"]: (u["speaker"]["kind"], u["speaker_confidence"]["basis"]) for u in utterances}
    assert found["mine"] == ("owner", "voice")
    assert found["too short to tell"] == ("owner", "named")
    assert found["first anon"] == ("stranger", "named")
    assert found["first anon again"] == ("stranger", "named")  # _mic streams as someone else present


def test_revisions_change_only_when_a_conversation_does(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    write_stream(db_path, stream_dir)
    assert json.loads((stream_dir / "index.json").read_text())["format_version"] == 1
    index, utterances = read(stream_dir)
    first, transcript = index[0]["revision"], index[0]["transcript_revision"]
    ids = [u["utterance_id"] for u in utterances]
    write_stream(db_path, stream_dir)
    assert read(stream_dir)[0][0]["revision"] == first

    # The operator's correction applies to past turns, so the conversation's
    # revision changes, but the transcript and so the utterance ids don't.
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO operator_input VALUES ('c1:0002', 'clip', '_me')")
    db.commit()
    write_stream(db_path, stream_dir)
    index, utterances = read(stream_dir)
    assert index[0]["revision"] != first
    assert index[0]["transcript_revision"] == transcript
    assert [u["utterance_id"] for u in utterances] == ids
    assert utterances[2]["speaker"] == {"kind": "owner", "name": None, "label": None}

    # Re-transcription: the same ids now name different speech.
    db.execute("UPDATE turns SET end = end + 2, text = 'mine, longer' WHERE idx = 0")
    db.commit()
    write_stream(db_path, stream_dir)
    assert read(stream_dir)[0][0]["transcript_revision"] != transcript

    # A conversation that no longer exists loses its file.
    db.execute("DELETE FROM conversations WHERE conversation_id = 'c1'")
    db.execute("DELETE FROM turns")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    assert list((stream_dir / "conversations").iterdir()) == []


def test_a_conversation_is_held_two_weeks_unless_released_and_once_published_stays(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"

    def published(now: float) -> list[str]:
        write_stream(db_path, stream_dir, now=now)
        return [e["conversation_id"] for e in json.loads((stream_dir / "index.json").read_text())["conversations"]]

    def release(start: float, end: float | None = None) -> None:
        db = sqlite3.connect(db_path)
        db.execute("DELETE FROM releases")
        if end is not None:
            db.execute("INSERT INTO releases VALUES (?, ?)", (start, end))
        db.commit()
        db.close()

    # c1 ends at T + 100, c2 at T + 560.
    assert write_stream(db_path, stream_dir, now=T + 3600)["held"] == 2
    assert published(T + 3600) == [] and not (stream_dir / "conversations/c1.jsonl").exists()
    release(T + 1, T + 100)  # doesn't cover all of c1
    assert published(T + 3600) == []
    release(T, T + 100)
    assert published(T + 3600) == ["c1"] and (stream_dir / "conversations/c1.jsonl").exists()
    release(T)  # taken out of the labels: c1 is already published, so it stays
    assert published(T + 3600) == ["c1"]
    assert published(T + 560 + HOLD - 1) == ["c1"]
    assert published(T + 560 + HOLD) == ["c1", "c2"]


def test_forgotten_list_exists_and_is_never_rewritten(tmp_path):
    db_path, stream_dir = build(tmp_path), tmp_path / "stream"
    write_stream(db_path, stream_dir)
    assert json.loads((stream_dir / "forgotten.json").read_text()) == []

    entry = {"forgotten_at": "2026-10-01T12:00:00.000Z", "start": "2026-09-21T14:13:20.000Z",
             "end": "2026-09-21T14:13:25.000Z", "conversation_id": "c1", "utterance_ids": ["c1:0000"],
             "reason": "asked"}
    (stream_dir / "forgotten.json").write_text(json.dumps([entry]))
    db = sqlite3.connect(db_path)
    db.execute("DELETE FROM conversations")
    db.execute("DELETE FROM turns")
    db.commit()
    db.close()
    write_stream(db_path, stream_dir)
    assert (stream_dir / "forgotten.json").read_text() == json.dumps([entry])  # not even reformatted

import json
import sqlite3

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.delivery import measure_delivery, positions, speaking_rate, stutters
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.turns import SCHEMA as TURNS_SCHEMA


def words(spec):
    """'I@0.0 I@0.3 ...': each word 0.2 s long, starting at the given second."""
    return [(float(t), float(t) + 0.2, w) for w, t in (item.split("@") for item in spec.split())]


def test_stutters_are_found_by_timing_and_deliberate_doubles_are_not():
    turn = words("So@0.0 I@0.25 I@0.5 went@0.75 uh@1.0 wh@1.25 what@1.5 yeah,@1.75 yeah@2.0 the@3.0 the@4.0")
    found = [(at, kind) for at, kind, _, _ in stutters(turn)]
    # "the ... the" a second apart is two separate words, not a stutter.
    assert found == [(0.25, "repeat"), (1.0, "filler"), (1.25, "restart")]


def test_each_stutter_is_placed_in_the_text_so_it_can_be_dropped():
    # "5" got no timestamp, so it is in the text but not among the timed words.
    turn = words("I@0.0 I@0.25 had@0.5 uh@0.75 wh@1.0 what@1.25 friends@1.75")
    text = "I I had 5 uh wh what friends"
    where = positions(turn, text)
    dropped = {where[i] for _, _, _, i in stutters(turn)}
    assert " ".join(w for n, w in enumerate(text.split()) if n not in dropped) == "I had 5 what friends"


def test_speaking_rate_leaves_pauses_out():
    # 4 words from 0 to 2.0 s, with one pause (1.05 s, from 0.45 to 1.5): 0.95 s of speaking.
    assert round(speaking_rate(words("one@0.0 two@0.25 three@1.5 four@1.8")), 2) == round(4 / 0.95, 2)


def test_delivery_is_measured_on_the_owner_s_own_turns_long_enough_to_judge(tmp_path):
    # (start, end, label, basis, timed words as word@start-end)
    turns = [
        # The owner by voice. "went" to "to" is exactly PAUSE, not a pause; then pauses of 0.5 and 1.0 s.
        (0.0, 5.0, "owner", "voice",
         "so@0-0.25 I@0.25-0.5 went@0.5-1 to@1.25-1.5 the@1.5-1.75 the@1.75-2 store@2.5-3 today@4-4.5"),
        (5.0, 6.0, "owner", "voice", "hi@5.2-5.5 there@5.6-5.9"),             # too short to judge
        (10.0, 15.0, "owner", "diarization", "yes@10-10.5 we@10.5-11 did@11-11.5"),  # not by its own voice
        (20.0, 25.0, "owner", "channel", "one@20-20.5 two@20.5-21"),
        (30.0, 35.0, "not_owner", "voice", "not@30-30.5 mine@30.5-31"),
    ]
    db_path, transcripts = tmp_path / "h.sqlite", tmp_path / "transcripts"
    db = sqlite3.connect(db_path)
    db.executescript(TURNS_SCHEMA + SPEAKERS_SCHEMA + ASSEMBLE_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-09-21T14:13:20+00:00', 40, 1, 'c1.wav', 'sha', 0)")
    timed = []
    for idx, (start, end, label, basis, spec) in enumerate(turns):
        said = [(w, *map(float, span.split("-"))) for w, span in (item.split("@") for item in spec.split())]
        timed += [{"word": w, "start": s, "end": e} for w, s, e in said]
        turn_id = f"c1:{idx:04d}"
        db.execute("INSERT INTO turns VALUES (?, 'c1', ?, ?, ?, ?, 'S0', 0.9)",
                   (turn_id, idx, start, end, " ".join(w for w, _, _ in said)))
        db.execute("INSERT INTO turn_speakers VALUES (?, 'c1', 1, NULL, NULL, ?, ?, NULL, NULL)", (turn_id, label, basis))
    db.commit()
    db.close()
    transcripts.mkdir()
    (transcripts / "c1.json").write_text(json.dumps({"conversation_id": "c1", "wav_sha256": "sha", "settings": {},
                                                     "segments": [{"words": timed}]}))

    measure_delivery(db_path, transcripts)
    db = sqlite3.connect(db_path)
    # 8 words over 4.5 s less 1.5 s of pauses; the pauses' mean, not their total.
    assert db.execute("SELECT * FROM turn_delivery ORDER BY turn_id").fetchall() == [
        ("c1:0000", round(8 / 3.0, 2), 2, 0.75), ("c1:0003", 2.0, 0, None)]
    # The lead-in is the 4 words before the stutter over 1.5 s, not the stutter itself.
    assert db.execute("SELECT * FROM turn_stutters").fetchall() == [("c1:0000", 1.5, "repeat", round(4 / 1.5, 2), 4)]
    db.close()

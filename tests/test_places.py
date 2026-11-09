import json
import sqlite3
import struct
from datetime import datetime, timedelta, timezone

from hearsay.affect import SCHEMA as AFFECT_SCHEMA
from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA
from hearsay.delivery import SCHEMA as DELIVERY_SCHEMA
from hearsay.forget import SCHEMA as FORGET_SCHEMA
from hearsay.hold import SCHEMA as HOLD_SCHEMA
from hearsay.media import SCHEMA as MEDIA_SCHEMA
from hearsay.music import SCHEMA as MUSIC_SCHEMA
from hearsay.capture import ACTIVITY, LOCATION, MOVEMENT, ROUTE
from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
from hearsay.parse import rebuild
from hearsay.places import find_places
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA
from hearsay.stream import write_stream
from hearsay.turns import SCHEMA as TURNS_SCHEMA
from test_capture import record, upload

T = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
BAR = (-26.12000, 80.14000)
HOME = (-26.13000, 80.15000)  # about 1.5 km from the bar


def fix(at, lat, lon, accuracy=65.0):
    return record(at, LOCATION, struct.pack("<ddf", lat, lon, accuracy))


def test_a_conversation_lists_the_named_places_it_happened_at(tmp_path):
    raw, db_path, labels, stream = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels", tmp_path / "stream"
    labels.mkdir()
    t0 = T.timestamp()
    # The phone's readings: at the bar from before the conversation starts,
    # 40 m off on one reading, then somewhere unnamed, then home.
    readings = [fix(t0 - 60, *BAR), fix(t0 + 60, BAR[0] + 0.00036, BAR[1]), fix(t0 + 600, 26.2, -80.2),
                fix(t0 + 900, *HOME)]
    upload(raw, T + timedelta(minutes=20), "k-places", b"".join(readings))
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(ASSEMBLE_SCHEMA + CONVERSATIONS_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA
                     + MUSIC_SCHEMA + MEDIA_SCHEMA + FORGET_SCHEMA + HOLD_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0, t0 + 1200))
    db.execute("INSERT INTO conversations VALUES ('c2', ?, ?, 60, 0)", (t0 + 7200, t0 + 7300))  # no readings
    db.commit()
    db.close()
    (labels / "places.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "place", "name": "Back", "latitude": BAR[0], "longitude": BAR[1]},
        {"type": "place", "name": "home", "latitude": HOME[0], "longitude": HOME[1]},
        {"type": "rename", "from": "Back", "to": "Back Room"},  # retroactive
    ]))

    find_places(db_path, labels)
    write_stream(db_path, stream)

    index = {e["conversation_id"]: e for e in json.loads((stream / "index.json").read_text())["conversations"]}
    assert index["c1"]["places"] == [
        {"name": "Back Room", "start": "2026-10-06T20:00:00.000Z", "end": "2026-10-06T20:10:00.000Z"},
        {"name": "home", "start": "2026-10-06T20:15:00.000Z", "end": "2026-10-06T20:20:00.000Z"},
    ]
    assert index["c2"]["places"] == []
    assert "-26.1" not in (stream / "index.json").read_text()  # never coordinates


def test_new_stays_keep_the_fix_accuracy_and_legacy_stays_keep_the_radius(tmp_path):
    raw, db_path = tmp_path / "raw", tmp_path / "h.sqlite"
    t0 = T.timestamp()
    latitude, longitude = 0.5, 0.5
    legacy = struct.pack("<BBBddf", 2, 90, 0, latitude, longitude, 100.0)
    current = struct.pack("<BBBddff", 2, 90, 0, latitude, longitude, 100.0, 7.25)
    records = [record(t0, MOVEMENT, legacy),
               fix(t0 + 60, latitude, longitude, accuracy=9),
               record(t0 + 300, MOVEMENT, current),
               record(t0 + 600, MOVEMENT, struct.pack("<BBB", 3, 90, 4))]
    upload(raw, T + timedelta(minutes=11), "k-mixed-stays", b"".join(records))
    rebuild(raw, db_path)

    with sqlite3.connect(db_path) as db:
        readings = db.execute("SELECT at, latitude, longitude, accuracy FROM locations ORDER BY at").fetchall()
    assert readings == [(t0, latitude, longitude, 100.0),
                        (t0 + 60, latitude, longitude, 9.0),
                        (t0 + 300, latitude, longitude, 7.25)]


def test_staying_put_keeps_the_place_without_fresh_fixes(tmp_path):
    raw, db_path, labels, stream = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels", tmp_path / "stream"
    labels.mkdir()
    t0 = T.timestamp()
    anchor = struct.pack("<BBB", 2, 90, 0) + struct.pack("<ddf", *BAR, 100.0)  # stationary, 90%, heartbeat
    readings = [fix(t0, *BAR, accuracy=12.0),
                record(t0 + 5, ACTIVITY, struct.pack("<dBB", t0, 1, 2)),  # stationary, high confidence
                record(t0 + 130, MOVEMENT, struct.pack("<BBB", 2, 90, 3) + struct.pack("<ddf", *BAR, 100.0)),  # settled
                *[record(t0 + 130 + 300 * i, MOVEMENT, anchor) for i in range(1, 30)],
                record(t0 + 9000, ROUTE, b"\x01" + "BluetoothA2DPOutput\tMy Car".encode()),
                record(t0 + 9010, MOVEMENT, struct.pack("<BBB", 5, 40, 5))]  # driving, a guess from the car
    upload(raw, T + timedelta(hours=3), "k-stay", b"".join(readings))
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(ASSEMBLE_SCHEMA + CONVERSATIONS_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA
                     + MUSIC_SCHEMA + MEDIA_SCHEMA + FORGET_SCHEMA + HOLD_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0 + 7200, t0 + 7800))  # two hours in
    db.commit()
    db.close()
    (labels / "places.jsonl").write_text(json.dumps({"type": "place", "name": "Back", "latitude": BAR[0], "longitude": BAR[1]}) + "\n")

    find_places(db_path, labels)
    write_stream(db_path, stream)

    [entry] = json.loads((stream / "index.json").read_text())["conversations"]
    assert [p["name"] for p in entry["places"]] == ["Back"]


def test_a_place_with_a_wider_area_takes_readings_further_out(tmp_path):
    raw, db_path, labels = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels"
    labels.mkdir()
    t0 = T.timestamp()
    upload(raw, T + timedelta(minutes=20), "k-wide", fix(t0 + 60, BAR[0] + 0.0027, BAR[1]))  # 300 m off
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0, t0 + 600))
    db.commit()
    db.close()

    def places_with(radius):
        place = {"type": "place", "name": "Boatyard", "latitude": BAR[0], "longitude": BAR[1]}
        (labels / "places.jsonl").write_text(json.dumps({**place, **({"radius": radius} if radius else {})}) + "\n")
        return find_places(db_path, labels)["conversations_with_a_place"]

    assert places_with(None) == 0
    assert places_with(400) == 1


def test_wrong_readings_are_corrected_by_time_and_stay_corrected(tmp_path):
    raw, db_path, labels = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels"
    labels.mkdir()
    t0 = T.timestamp()
    # At home all along, but one fix bounced to the bar, one was too vague to
    # say anything, and one indoors landed 300 m off.
    readings = [fix(t0 - 60, *HOME), fix(t0 + 300, *BAR), fix(t0 + 400, *BAR, accuracy=900),
                fix(t0 + 600, HOME[0] + 0.0027, HOME[1])]
    upload(raw, T + timedelta(minutes=20), "k-wrong", b"".join(readings))
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0, t0 + 900))
    db.commit()
    db.close()
    named = [{"type": "place", "name": "Bar", "latitude": BAR[0], "longitude": BAR[1]},
             {"type": "place", "name": "Hom", "latitude": HOME[0], "longitude": HOME[1]}]

    def places(*records):
        (labels / "places.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [*named, *records]))
        find_places(db_path, labels)
        db = sqlite3.connect(db_path)
        found = [p for (p,) in db.execute("SELECT place FROM conversation_places ORDER BY start")]
        db.close()
        return found

    assert places() == ["Hom", "Bar"]  # the bounce; the vague one and the far one say nothing
    corrections = [{"type": "drop", "start": t0 + 300, "end": t0 + 300},
                   {"type": "at", "start": t0 + 600, "end": t0 + 600, "name": "Hom"}]
    assert places(*corrections) == ["Hom"]
    # A wider bar doesn't take the dropped reading back, and a rename carries the move.
    wider = {**named[0], "radius": 2000}
    named[0] = wider
    assert places(*corrections, {"type": "rename", "from": "Hom", "to": "Home"}) == ["Home"]


def test_a_span_put_at_a_place_holds_from_its_start_without_a_reading_there(tmp_path):
    raw, db_path, labels = tmp_path / "raw", tmp_path / "h.sqlite", tmp_path / "labels"
    labels.mkdir()
    t0 = T.timestamp()
    # Home before the conversation, and no reading after: the phone settled and stopped taking fixes.
    upload(raw, T + timedelta(minutes=20), "k-settled", fix(t0 - 60, *HOME))
    rebuild(raw, db_path)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 600, 0)", (t0, t0 + 900))
    db.commit()
    db.close()
    named = [{"type": "place", "name": "Bar", "latitude": BAR[0], "longitude": BAR[1]},
             {"type": "place", "name": "Home", "latitude": HOME[0], "longitude": HOME[1]}]
    at_bar = {"type": "at", "start": t0 + 300, "end": t0 + 900, "name": "Bar"}

    def places(*records):
        (labels / "places.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [*named, *records]))
        find_places(db_path, labels)
        db = sqlite3.connect(db_path)
        found = db.execute("SELECT place, start - ?, end - ? FROM conversation_places ORDER BY start", (t0, t0)).fetchall()
        db.close()
        return found

    assert places(at_bar) == [("Home", 0, 300), ("Bar", 300, 900)]
    # A later correction over its start wins, as over any reading.
    assert places(at_bar, {"type": "drop", "start": t0 + 300, "end": t0 + 300}) == [("Home", 0, 900)]

import asyncio
import html
import io
import json
import re
import sqlite3
import time
import wave

import httpx2
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA  # noqa: E402
from hearsay.forget import SCHEMA as FORGET_SCHEMA  # noqa: E402
from hearsay.hold import SCHEMA as HOLD_SCHEMA  # noqa: E402
from hearsay.media import SCHEMA as MEDIA_SCHEMA  # noqa: E402
from hearsay.music import SCHEMA as MUSIC_SCHEMA  # noqa: E402
from hearsay.parse import SCHEMA as PARSE_SCHEMA  # noqa: E402
from hearsay.places import SCHEMA as PLACES_SCHEMA, read_places  # noqa: E402
from hearsay.portal import create_app  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA, record_operator_input  # noqa: E402


@pytest.fixture
def portal(tmp_path, monkeypatch):
    """Eight 4 s turns in one conversation WAV: Hearsay couldn't tell who said the a turns, the b turns aren't the owner.
    The portal shows its host's time zone, here UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    db_path, audio_dir, labels_dir = tmp_path / "h.sqlite", tmp_path / "audio", tmp_path / "labels"
    audio_dir.mkdir()
    labels_dir.mkdir()
    # Each second of audio holds its own index as the sample value.
    with wave.open(str(audio_dir / "c1.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(sec.to_bytes(2, "little") * 16000 for sec in range(40)))

    db = sqlite3.connect(db_path)
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + PLACES_SCHEMA + MEDIA_SCHEMA
                    + MUSIC_SCHEMA + FORGET_SCHEMA + HOLD_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-01-01T00:00:00+00:00', 40, 1, 'c1.wav', 'sha', 0)")
    for idx in range(8):
        turn_id = f"{'ab'[idx % 2]}{idx}"
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, "c1", idx, 5.0 * idx, 5.0 * idx + 4, "synthetic", None, None))
        label = (None, "not_owner")[idx % 2]
        db.execute("INSERT INTO turn_speakers VALUES (?,?,?,?,?,?,?, NULL, NULL)",
                   (turn_id, "c1", 1.0, None, 0.2 if label is None else 0.05, label, "voice" if label else None))
    db.commit()
    db.close()
    record_operator_input(db_path, labels_dir)

    app = create_app(db_path, audio_dir, labels_dir, labels_dir / "stream", "me", "correct horse", "k" * 64, app_token="capture-token")
    return TestClient(app), db_path, labels_dir


def log_in(client):
    return client.post("/login", data={"username": "me", "password": "correct horse"}, follow_redirects=False)


# Every route but /login and /logout, with a form that would write labels if it got through. Pages send
# the browser to log in; audio and the app's map answer 401, since a player or the app can't show a page.
@pytest.mark.parametrize("method, path, form, refused", [
    ("GET", "/", None, "/login"),
    ("GET", "/conversations", None, "/login"),
    ("GET", "/conversations/c1", None, "/login"),
    ("GET", "/day", None, "/login"),
    ("GET", "/day/2026-01-01", None, "/login"),
    ("GET", "/places", None, "/login"),
    ("GET", "/words", None, "/login"),
    ("GET", "/conversations/c1/audio", None, 401),
    ("GET", "/audio/a0", None, 401),
    ("GET", "/places.json", None, 401),
    ("POST", "/turn", {"turn_id": "a0", "category": "_media"}, "/login"),
    ("POST", "/turn/undo", {"turn_id": "a0"}, "/login"),
    ("POST", "/turn/delete", {"turn_id": "a0"}, "/login"),
    ("POST", "/turn/delete/undo", {"turn_id": "a0"}, "/login"),
    ("POST", "/forget", {"conversation_id": "c1", "action": "whole"}, "/login"),
    ("POST", "/forget/undo", {"conversation_id": "c1", "at": "2026-01-01T00:00:00+00:00|2026-01-01T00:00:04+00:00"}, "/login"),
    ("GET", "/release?conversation_id=c1", None, "/login"),
    ("POST", "/release", {"conversation_id": "c1"}, "/login"),
    ("POST", "/release/undo", {"conversation_id": "c1"}, "/login"),
    ("POST", "/places", {"action": "name", "name": "Back Room", "latitude": "-26.12", "longitude": "80.14"}, "/login"),
    ("POST", "/words", {"action": "save", "words": "Xanthe"}, "/login"),
    ("POST", "/apply", {"back": "/"}, "/login"),
    ("POST", "/turns/tag", {"conversation_id": "c1", "turn_id": "a0", "category": "_me"}, "/login"),
    ("POST", "/conversations/place", {"conversation_id": "c1", "place": "Back Room"}, "/login"),
])
def test_nothing_is_reachable_or_written_without_logging_in(portal, method, path, form, refused):
    client, _, labels_dir = portal

    def files() -> dict:
        return {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()}

    before = files()
    response = client.request(method, path, data=form, follow_redirects=False)
    if refused == 401:
        assert response.status_code == 401
    else:
        assert response.status_code == 303 and response.headers["location"] == refused
    assert files() == before


# Each form with a byte that isn't UTF-8 in a field that decides what's written, as no browser sends it.
@pytest.mark.parametrize("path, body", [
    ("/login", b"username=me&password=correct horse\xff"),
    ("/turn", b"turn_id=a0&category=_media\xff"),
    ("/forget", b"conversation_id=c1&action=whole\xff"),
    ("/forget/undo", b"conversation_id=c1&at=2026-01-01T00:00:00%2B00:00|2026-01-01T00:00:04\xff"),
    ("/release", b"conversation_id=c1\xff"),
    ("/places", b"action=name&name=Back Room&latitude=-26.12\xff&longitude=80.14"),
    ("/words", b"action=save\xff&words=Xanthe"),
])
def test_a_form_that_isnt_utf_8_is_bad_input_and_writes_nothing(portal, path, body):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, labels_dir = portal
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.commit()
    db.close()
    if path != "/login":
        log_in(client)
    before = {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()}
    response = client.post(path, content=body, headers={"Content-Type": "application/x-www-form-urlencoded"},
                           follow_redirects=False)
    assert response.status_code in (200, 303) and "set-cookie" not in response.headers
    assert {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()} == before


def test_only_the_right_username_and_password_or_a_live_signed_session_log_in(portal, monkeypatch):
    client, _, _ = portal
    for username, password in (("me", "nope"), ("you", "correct horse")):
        wrong = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
        assert wrong.status_code == 200 and "set-cookie" not in wrong.headers
    # Forged, or not even ASCII: no session. Sent as raw bytes, as a browser can; the test client would make them UTF-8.
    async def get(cookie: bytes) -> httpx2.Response:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=client.app), base_url="http://testserver") as raw:
            return await raw.get("/", headers={"Cookie": b"hearsay_session=" + cookie})
    for forged in (b"9999999999.forged", "9999999999.é".encode(), "².forged".encode("latin-1")):
        assert asyncio.run(get(forged)).headers["location"] == "/login"

    client.cookies.clear()
    assert log_in(client).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200

    # A session past its 30 days, though signed, is refused. Sent by hand: the client's own jar would drop it.
    cookie = f"hearsay_session={client.cookies['hearsay_session']}"
    client.cookies.clear()
    later = time.time() + 31 * 24 * 3600
    monkeypatch.setattr(time, "time", lambda: later)
    assert client.get("/", headers={"Cookie": cookie}, follow_redirects=False).headers["location"] == "/login"


def test_the_app_logs_in_with_the_capture_token(portal, tmp_path):
    client, db_path, labels_dir = portal
    # The token alone, without its scheme, isn't the app.
    for wrong in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "capture-token"}):
        response = client.post("/login/app", headers=wrong, follow_redirects=False)
        assert response.status_code == 401 and "set-cookie" not in response.headers
        assert client.get("/places.json", headers=wrong).status_code == 401
    response = client.post("/login/app", headers={"Authorization": "Bearer capture-token"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200

    # With no token set up, an empty one doesn't match it.
    tokenless = TestClient(create_app(db_path, tmp_path / "audio", labels_dir, labels_dir / "stream", "me", "correct horse", "k" * 64))
    empty = {"Authorization": "Bearer "}
    response = tokenless.post("/login/app", headers=empty, follow_redirects=False)
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert tokenless.get("/places.json", headers=empty).status_code == 401


def test_turn_audio_supports_byte_ranges_for_ios(portal):
    client, _, _ = portal
    log_in(client)
    full = client.get("/audio/a2")  # turn [10, 14) s
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    with wave.open(io.BytesIO(full.content)) as w:
        assert w.getnframes() == 4 * 16000
        assert int.from_bytes(w.readframes(1), "little") == 10

    # A range starting at or past the end has nothing to serve.
    for start in (len(full.content), len(full.content) + 10):
        beyond = client.get("/audio/a2", headers={"Range": f"bytes={start}-"})
        assert beyond.status_code == 416 and beyond.headers["content-range"] == f"bytes */{len(full.content)}"


@pytest.mark.parametrize("asked, served", [
    ("bytes=0-1", slice(0, 2)),
    ("bytes=0-", slice(0, None)),  # open-ended: to the end
    ("bytes=-10", slice(-10, None)),  # suffix: the last 10
    ("bytes=-1", slice(-1, None)),
    ("bytes=-999999999", slice(0, None)),  # a suffix longer than the file is all of it
    ("bytes=100-999999999", slice(100, None)),  # an end past the file stops at it
])
def test_turn_audio_serves_the_byte_range_asked_for(portal, asked, served):
    client, _, _ = portal
    log_in(client)
    full = client.get("/audio/a2").content
    part = client.get("/audio/a2", headers={"Range": asked})
    start, stop, _ = served.indices(len(full))
    assert part.status_code == 206 and part.content == full[served]
    assert part.headers["content-range"] == f"bytes {start}-{stop - 1}/{len(full)}"


def test_a_spot_where_the_owner_spent_time_can_be_named(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.places import SCHEMA as PLACES_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + PLACES_SCHEMA)
    # Readings around the fixture's conversation (its WAV starts at 2026-01-01 00:00 UTC).
    db.executemany("INSERT INTO locations VALUES ('p', ?, -26.12, 80.14, 65)", [(1_767_225_590 + 60 * i,) for i in range(5)])
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225590, 1767225900, 32, 0)")
    db.commit()
    db.close()

    page = client.get("/places").text
    assert "About 5 min" in page and "maps.apple.com/?ll=-26.12000,80.14000" in page
    assert page.count("<audio") == 3  # clips heard there, to tell where it is
    # The app's map: the conversations there, each with its first words, to tell by what was said.
    [spot] = client.get("/places.json", headers={"Authorization": "Bearer capture-token"}).json()["unnamed"]
    assert [(c["start"], c["opening"]) for c in spot["conversations"]] == [(1_767_225_590, "synthetic synthetic")]

    client.post("/places", data={"action": "name", "name": "Back Room", "latitude": "-26.12", "longitude": "80.14"})
    page = client.get("/places").text
    assert "Nowhere new." in page and "<strong>Back Room</strong>" in page
    [record] = [json.loads(line) for line in (labels_dir / "places.jsonl").read_text().splitlines()]
    assert (record["type"], record["name"], record["latitude"]) == ("place", "Back Room", -26.12)

    # Anchored where it really is, from a pasted Maps link; readings near the old spot are offered again.
    client.post("/places", data={"action": "move", "from": "Back Room",
                                 "at": "https://maps.apple.com/?ll=-26.13000%2C80.15000&q=Pin"})
    assert read_places(labels_dir) == [("Back Room", -26.13, 80.15, 100.0)]
    assert "About 5 min" in client.get("/places").text

    # The app's map: the token alone, no session; dragging one of a place's two spots keeps both.
    client.cookies.clear()
    app = {"Authorization": "Bearer capture-token"}
    assert client.get("/places.json", headers={"Authorization": "Bearer wrong"}).status_code == 401
    client.post("/places", headers=app, data={"action": "name", "name": "Back Room", "latitude": "-26.12", "longitude": "80.14"})
    places = client.get("/places.json", headers=app).json()
    assert len(places["named"]) == 2 and places["unnamed"] == []
    client.post("/places", headers=app, data={"action": "move", "from": "Back Room", "at": ["-26.13,80.15", "-26.125,80.145"],
                                              "radius": ["100", "400"]})
    assert read_places(labels_dir) == [("Back Room", -26.13, 80.15, 100.0), ("Back Room", -26.125, 80.145, 400.0)]
    # A move pasted on the web page, with no radius, keeps the area set.
    client.post("/places", headers=app, data={"action": "move", "from": "Back Room", "at": "-26.12,80.14"})
    assert read_places(labels_dir) == [("Back Room", -26.12, 80.14, 400.0)]

    # Its conversations, each with the span of readings that put it there; dropping that span takes it off.
    [visit] = client.get("/places.json", headers=app).json()["visits"]["Back Room"]
    assert visit["readings"] == [1_767_225_590, 1_767_225_830]
    client.post("/places", headers=app, data={"action": "drop", "start": "1767225590", "end": "1767225830"})
    assert client.get("/places.json", headers=app).json()["visits"] == {}


@pytest.mark.parametrize("radius", ["nan", "inf", "-inf", "wide"])
def test_a_radius_that_isnt_a_number_of_metres_keeps_the_area(portal, radius):
    client, _, labels_dir = portal
    log_in(client)
    client.post("/places", data={"action": "name", "name": "Back Room", "latitude": "-26.12", "longitude": "80.14", "radius": radius})
    assert read_places(labels_dir) == [("Back Room", -26.12, 80.14, 100.0)]
    client.post("/places", data={"action": "move", "from": "Back Room", "at": ["-26.12,80.14", "-26.13,80.15"],
                                 "radius": ["400", radius]})
    assert read_places(labels_dir) == [("Back Room", -26.12, 80.14, 400.0), ("Back Room", -26.13, 80.15, 100.0)]


@pytest.mark.parametrize("action", ["name", "ignore", "unignore"])
@pytest.mark.parametrize("latitude, longitude", [
    ("north", "80.14"), ("-26.12", ""),  # not numbers
    ("nan", "80.14"), ("-26.12", "inf"),  # not finite
    ("91", "80.14"), ("-26.12", "-180.5"),  # off the earth
])
def test_a_spot_that_isnt_a_point_on_earth_writes_nothing(portal, action, latitude, longitude):
    client, _, labels_dir = portal
    log_in(client)
    before = {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()}
    response = client.post("/places", data={"action": action, "name": "Back Room", "latitude": latitude,
                                            "longitude": longitude}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/places"
    assert {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("action", ["drop", "at"])
@pytest.mark.parametrize("start, end", [("noon", "1767225830"), ("nan", "1767225830"), ("1767225590", "inf"),
                                        ("-inf", "1767225830")])
def test_a_span_of_readings_that_isnt_two_times_writes_nothing(portal, action, start, end):
    client, _, labels_dir = portal
    log_in(client)
    before = {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()}
    response = client.post("/places", data={"action": action, "name": "Back Room", "start": start, "end": end},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/places"
    assert {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()} == before


def test_each_clip_shows_its_words_and_when_and_where_it_was_said(portal):
    client, db_path, _ = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.execute("UPDATE turns SET text = 'Pass the <salt>' WHERE turn_id = 'a0'")
    # The conversation starts 2026-01-01 00:00 UTC; the owner moved on 20 s in.
    db.execute("INSERT INTO conversation_places VALUES ('c1', 'Blind Monk', 1767225600, 1767225620)")
    db.execute("INSERT INTO conversation_places VALUES ('c1', 'Home', 1767225620, 1767225640)")
    db.commit()
    db.close()

    page = client.get("/").text
    assert "Pass the &lt;salt&gt;" in page
    assert "4.0 s · <a class='inline' href='/conversations/c1#c1-0'>Jan 1 00:00</a> · Blind Monk" in page  # a0, at 0 s
    assert "#c1-4'>Jan 1 00:00</a> · Home" in page  # a4, at 20 s


def test_conversations_narrow_to_a_place_and_to_words_said_and_read_in_order(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, _ = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    # A day later, at no named place, and with no audio kept.
    db.execute("INSERT INTO conversations VALUES ('c2', 1767312000, 1767312010, 2, 1)")
    db.execute("INSERT INTO conversation_audio VALUES ('c2', '2026-01-02T00:00:00+00:00', 10, 1, NULL, NULL, 0)")
    db.execute("INSERT INTO turns VALUES ('c2:0', 'c2', 0, 1, 3, 'Pass the bread', NULL, NULL)")
    db.execute("INSERT INTO turn_speakers VALUES ('c2:0', 'c2', 1.0, NULL, 0.9, 'owner', 'voice', NULL, NULL)")
    db.execute("UPDATE turns SET text = 'Pass the <salt>' WHERE turn_id = 'a2'")
    db.execute("UPDATE turns SET diar_speaker = 'SPEAKER_07' WHERE turn_id IN ('b1', 'b5')")
    db.execute("INSERT INTO turn_media VALUES ('b3', 0.95)")
    db.execute("INSERT INTO conversation_places VALUES ('c1', 'Home', 1767225600, 1767225620)")
    db.execute("INSERT INTO conversation_places VALUES ('c1', 'Back Room', 1767225620, 1767225640)")
    db.commit()
    db.close()

    def listed(**params) -> list[str]:
        return [c for c in ("c1", "c2") if f"/conversations/{c}" in client.get("/conversations", params=params).text]

    page = client.get("/conversations").text
    assert page.index("/conversations/c2") < page.index("/conversations/c1")  # newest first
    assert "1 min · Home · Back Room · 8 turn(s)" in page and "still going" in page
    assert listed(place="Home") == ["c1"]
    assert listed(q="pass THE") == ["c1", "c2"]
    assert listed(q="salt", place="Home") == ["c1"] and listed(q="salt", place="Nowhere") == []
    found = client.get("/conversations", params={"q": "SALT"}).text
    assert "<a href='/conversations/c1?q=SALT#c1-2'>Pass the &lt;<mark>salt</mark>&gt;</a>" in found

    page = client.get("/conversations/c1", params={"q": "salt"}).text
    turns = [page.index(f"id='c1-{idx}'") for idx in range(8)]
    assert turns == sorted(turns)  # in the order said
    assert page.index("At Home") < turns[0] < turns[3] < page.index("At Back Room") < turns[4]
    assert "<a class='at' href='/audio/a0'>00:00:00</a> <span class='muted'>Not sure</span>" in page
    assert "Not me · A" in page and "Not me</span>" in page  # b1 and b5 one voice, b7 unknown
    assert "class='turn left' data-start='15.00'" in page and "Left out · media" in page  # b3 sounds like TV
    assert "Pass the &lt;<mark>salt</mark>&gt;" in page
    assert "No audio." in client.get("/conversations/c2").text
    assert client.get("/conversations/nope").status_code == 404

    whole = client.get("/conversations/c1/audio", headers={"Range": "bytes=0-43"})
    assert whole.status_code == 206 and whole.content[:4] == b"RIFF"
    assert client.get("/conversations/c2/audio").status_code == 404
    client.cookies.clear()
    assert client.get("/conversations/c1/audio").status_code == 401


def test_all_not_me_and_all_media_mark_the_turns_hearsay_is_sure_of_and_leave_the_rest_yellow(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.execute("INSERT INTO turn_media VALUES ('b3', 0.95)")  # media
    db.execute("INSERT INTO turn_media VALUES ('b5', 0.6)")  # might be
    # b1 and b7 aren't the owner only by their speaker: b1 too short to say by its own voice,
    # b7's own voice close to the owner's. The operator tags a6, which Hearsay couldn't decide.
    db.execute("UPDATE turn_speakers SET basis = 'diarization', owner_similarity = NULL WHERE turn_id = 'b1'")
    db.execute("UPDATE turn_speakers SET basis = 'diarization', owner_similarity = 0.3 WHERE turn_id = 'b7'")
    # a0 and a2 are the owner's, by voice and by channel, though they sound like media: never checked, never offered.
    db.execute("UPDATE turn_speakers SET label = 'owner', basis = 'voice' WHERE turn_id = 'a0'")
    db.execute("UPDATE turn_speakers SET label = 'owner', basis = 'channel' WHERE turn_id = 'a2'")
    db.executemany("INSERT INTO turn_media VALUES (?, 0.95)", [("a0",), ("a2",)])
    db.commit()
    db.close()
    client.post("/turn", data={"turn_id": "a6", "category": "_stranger"})
    record_operator_input(db_path, labels_dir)

    page = client.get("/conversations/c1").text
    picks = dict(re.findall(r"id='c1-(\d)' class='turn[^']*' data-start='[\d.]+' data-pick='(\w+)'", page))
    assert picks == {"1": "notme", "3": "media", "6": "notme"}
    unsure = dict(re.findall(r"id='c1-(\d)' class='turn[^']* unsure'.*?<span class='unsure-why'>([^<]+)", page))
    assert unsure == {"4": "Not sure who spoke", "5": "Might be media",
                      "7": "Not me by its speaker, but its voice is close to yours"}
    assert "<button type=\"button\" value=\"notme\">All not me</button>" in page
    front = client.get("/").text
    assert "/audio/b3" in front and "/audio/a0" not in front and "/audio/a2" not in front

    # Throwing out what All not me checked keeps the turns between them.
    client.post("/forget", data={"conversation_id": "c1", "action": "checked", "turn_id": ["b1", "a6"]})
    marks = [json.loads(line)["at"] for line in (labels_dir / "forget.jsonl").read_text().splitlines()]
    assert marks == [["2026-01-01T00:00:05+00:00", "2026-01-01T00:00:09+00:00"],
                     ["2026-01-01T00:00:30+00:00", "2026-01-01T00:00:34+00:00"]]
    page = client.get("/conversations/c1").text
    assert "data-pick='notme'" not in page.split("id='c1-2'")[0]  # b1, going, is checked by nothing again


def test_checked_turns_are_tagged_and_placed_from_their_conversation_s_page(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.places import find_places

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.commit()
    db.close()
    form = {"conversation_id": "c1", "back": "/conversations/c1"}

    # Hearsay had b1 and b3 as someone else; they were the owner. Each shows as it will be until a reprocess.
    tagged = client.post("/turns/tag", data={**form, "turn_id": ["b3", "b1"], "category": "_me"}, follow_redirects=False)
    assert tagged.headers["location"] == "/conversations/c1"
    client.post("/turns/tag", data={**form, "turn_id": ["b5"], "category": "Alice"})  # not a category: ignored
    [tag] = [json.loads(line) for line in (labels_dir / "tags.jsonl").read_text().splitlines()]
    assert (tag["type"], tag["name"], tag["turn_ids"], tag["at"]) == ("clip", "_me", ["b1", "b3"], [
        ["2026-01-01T00:00:05+00:00", "2026-01-01T00:00:09+00:00"],
        ["2026-01-01T00:00:15+00:00", "2026-01-01T00:00:19+00:00"]])
    assert client.get("/conversations/c1").text.count("Me · at the next reprocess") == 2
    record_operator_input(db_path, labels_dir)
    page = client.get("/conversations/c1").text
    assert "Me · at the next reprocess" not in page and page.count("<span class='muted'>Me</span>") == 2

    # At a place from the first checked turn to the end, though no reading says so.
    client.post("/conversations/place", data={**form, "turn_id": ["b5", "b3"], "place": "  Back   Room "})
    [placed] = [json.loads(line) for line in (labels_dir / "places.jsonl").read_text().splitlines()]
    assert (placed["type"], placed["name"], placed["start"], placed["end"]) == ("at", "Back Room", 1767225615, 1767225640)
    assert "At Back Room from 00:00:15 at the next reprocess" in client.get("/conversations/c1").text
    find_places(db_path, labels_dir)
    page = client.get("/conversations/c1").text
    assert "Back Room from" not in page
    assert page.index("id='c1-2'") < page.index("At Back Room") < page.index("id='c1-3'")

    # Half of it music: flagged in the list.
    client.post("/turns/tag", data={**form, "turn_id": ["a0", "a2", "a4", "a6"], "category": "_music"})
    record_operator_input(db_path, labels_dir)
    assert "<span class='flag'>mostly music</span>" in client.get("/conversations").text


def test_mostly_media_music_or_not_me_each_flag_at_their_own_share_of_speech_time():
    from hearsay.portal import mostly

    owner, other, unsure = (4.0, "owner", None, "voice"), (4.0, "not_owner", None, "voice"), (4.0, None, None, None)
    assert mostly([(4.0, None, "_media", None), owner]) == ["mostly media"]  # half
    assert mostly([(4.0, "owner", "_music", "voice"), owner, owner]) == []  # a third
    assert mostly([other] * 3 + [(4.0, None, "_mic", None), owner]) == ["mostly not me"]  # 80%, a mic included
    assert mostly([other] * 3 + [unsure]) == []  # 75%: Hearsay couldn't tell who said the rest
    assert mostly([]) == []


def test_turns_hearsay_couldnt_decide_are_offered_and_the_operator_s_tag_decides(portal):
    from hearsay.stream import write_stream
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.affect import SCHEMA as AFFECT_SCHEMA
    from hearsay.delivery import SCHEMA as DELIVERY_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    page = client.get("/").text
    assert "4 turn(s)" in page and "/audio/a6" in page and "/audio/b1" not in page  # only the a turns
    assert page.index("/audio/a6") < page.index("/audio/a0")  # latest first
    assert "href='/conversations/c1#c1-6'>Jan 1 00:00</a>" in page  # each to hear in its conversation

    client.post("/turn", data={"turn_id": "a0", "category": "_me"})
    client.post("/turn", data={"turn_id": "a2", "category": "_stranger"})
    client.post("/turn", data={"turn_id": "a4", "category": "Alice"})  # not a category: ignored
    page = client.get("/").text
    assert "2 turn(s)" in page and "/audio/a0" not in page  # gone as soon as it's tagged
    [me, stranger] = [json.loads(line) for line in (labels_dir / "tags.jsonl").read_text().splitlines()]
    assert (me["type"], me["name"], me["at"]) == ("clip", "_me", [["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:04+00:00"]])

    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA)
    db.execute("DELETE FROM operator_input")
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.commit()
    db.close()
    record_operator_input(db_path, labels_dir)
    write_stream(db_path, labels_dir / "stream")
    utterances = [json.loads(line) for line in (labels_dir / "stream/conversations/c1.jsonl").read_text().splitlines()]
    kinds = {u["utterance_id"]: u["speaker"]["kind"] for u in utterances}
    assert (kinds["c1:0000"], kinds["c1:0002"], kinds["c1:0004"], kinds["c1:0001"]) == ("owner", "stranger", "unknown", "anonymous")


def test_turns_that_sound_like_tagged_media_are_offered_unless_the_owner_s_by_voice(portal):
    client, db_path, _ = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.execute("UPDATE turn_speakers SET label = 'owner' WHERE turn_id = 'b5'")
    db.executemany("INSERT INTO turn_media VALUES (?,?)",
                   [("b1", 0.95), ("a2", 0.97), ("b3", 0.5), ("b5", 0.99)])
    db.commit()
    db.close()

    page = client.get("/").text
    tv, unsure = page.split("Not sure who spoke")
    assert "2 turn(s)" in tv and "/audio/b1" in tv and "/audio/a2" in tv
    assert "/audio/b3" not in page and "/audio/b5" not in page  # under the hint, and the owner's by voice
    assert "/audio/a2" not in unsure and "3 turn(s)" in unsure  # offered once

    client.post("/turn", data={"turn_id": "b1", "category": "_media"})
    assert "/audio/b1" not in client.get("/").text


def test_a_day_reads_its_conversations_through_in_order_by_the_host_s_clock(portal, monkeypatch):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, labels_dir = portal
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    # 7 pm, 9 pm and 1 am in New York: the first two on Dec 31, though all three are Jan 1 in UTC.
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.execute("INSERT INTO conversations VALUES ('c2', 1767232800, 1767232810, 2, 0)")
    db.execute("INSERT INTO conversations VALUES ('c3', 1767247200, 1767247210, 2, 0)")
    for c in ("c2", "c3"):
        db.execute("INSERT INTO conversation_audio VALUES (?, '2026-01-01T00:00:00+00:00', 10, 1, NULL, NULL, 0)", (c,))
    db.commit()
    db.close()

    page = client.get("/day/2025-12-31").text
    assert "<h1>Wednesday December 31</h1>" in page and '<section id="c3">' not in page
    assert page.index('<section id="c1">') < page.index("1 h 59 min quiet") < page.index('<section id="c2">')
    assert ">19:00:00</a>" in page and "<a href='/day/2026-01-01'>Thu Jan 1 →</a>" in page and "←" not in page.split("<h1>")[1]
    # Each conversation's page goes on to the one before and after it, whatever their day.
    middle = client.get("/conversations/c2").text
    assert "<a href='/conversations/c1'>← Wed Dec 31 19:00</a><a href='/conversations/c3'>Thu Jan 1 01:00 →</a>" in middle
    assert "<p class='nav ends'><span></span><a href='/conversations/c2'>" in client.get("/conversations/c1").text
    assert "<a href='/day/2025-12-31'>Wed Dec 31</a>" in client.get("/conversations").text
    for day in ("not-a-day", "0001-01-01", "9999-12-31"):
        assert client.get(f"/day/{day}").status_code == 404

    # Acting on a day's page comes back to it.
    thrown = client.post("/forget", data={"conversation_id": "c2", "action": "whole", "back": "/day/2025-12-31#c2"},
                         follow_redirects=False)
    assert thrown.headers["location"] == "/day/2025-12-31#c2" and (labels_dir / "forget.jsonl").exists()
    elsewhere = client.post("/release", data={"conversation_id": "c2", "back": "//evil.example"}, follow_redirects=False)
    assert elsewhere.headers["location"] == "/conversations/c2"


def test_a_stretch_thrown_out_leaves_the_stream_and_is_final_once_a_reprocess_applies_it(portal):
    from hearsay.affect import SCHEMA as AFFECT_SCHEMA
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.delivery import SCHEMA as DELIVERY_SCHEMA
    from hearsay.forget import forget
    from hearsay.stream import write_stream

    client, db_path, labels_dir = portal
    stream = labels_dir / "stream"
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.executemany("INSERT INTO conversation_taps VALUES ('c1', ?)", [(1767225601,), (1767225617,)])
    db.commit()
    db.close()

    def throw_out(action: str, *turn_ids: str) -> str:
        data = {"conversation_id": "c1", "action": action, "reason": " too  personal ", "turn_id": list(turn_ids)}
        return client.post("/forget", data=data).text

    def reprocess() -> dict:
        forget(db_path, labels_dir)
        write_stream(db_path, stream)
        return json.loads((stream / "index.json").read_text())

    def said() -> list[str]:
        return [json.loads(line)["utterance_id"] for line in (stream / "conversations/c1.jsonl").read_text().splitlines()]

    # Turns checked apart are thrown out each on its own, and the turn between them is kept.
    page = throw_out("checked", "a4", "a2")
    assert "To throw out 00:00:10–00:00:14 · too personal" in page and "To throw out 00:00:20–00:00:24" in page
    assert page.count("class='turn left") == 2 and "id='c1-3' class='turn' " in page
    for undo in re.findall(r"name='at' value='([^']+)'>Undo", page):
        page = client.post("/forget/undo", data={"conversation_id": "c1", "at": html.unescape(undo)}).text
    assert "To throw out 00:" not in page and "class='turn left" not in page
    # Checked one after another, a2 at 10 s through a4 ending at 24 s, they're one stretch.
    page = throw_out("checked", "a2", "b3", "a4")
    assert "To throw out 00:00:10–00:00:24 · too personal at the next reprocess" in page
    undo = re.search(r"name='at' value='([^']+)'>Undo", page)[1]

    index = reprocess()
    assert said() == ["c1:0000", "c1:0001", "c1:0005", "c1:0006", "c1:0007"]
    assert index["conversations"][0]["taps"] == ["2026-01-01T00:00:01.000Z"]  # not the one at 17 s
    [entry] = json.loads((stream / "forgotten.json").read_text())
    assert entry == {"forgotten_at": entry["forgotten_at"], "start": "2026-01-01T00:00:10.000Z",
                     "end": "2026-01-01T00:00:24.000Z", "conversation_id": "c1", "utterance_ids": ["a2", "b3", "a4"],
                     "reason": "too personal"}

    # Applied, it can't be taken back: the page offers no Undo, and one sent anyway isn't written.
    page = client.get("/conversations/c1").text
    assert "Thrown out 00:00:10–00:00:24 · too personal" in page and "Undo" not in page and "some thrown out" in client.get("/conversations").text
    marks = (labels_dir / "forget.jsonl").read_text()
    client.post("/forget/undo", data={"conversation_id": "c1", "at": html.unescape(undo)})
    assert (labels_dir / "forget.jsonl").read_text() == marks
    # Even if the database got its words back, forgotten.json keeps them out of the stream.
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO turns VALUES ('a2', 'c1', 2, 10.0, 14.0, 'synthetic', NULL, NULL)")
    db.execute("DELETE FROM forgotten")
    db.commit()
    db.close()
    write_stream(db_path, stream)
    assert "c1:0002" not in said() and len(json.loads((stream / "forgotten.json").read_text())) == 1

    throw_out("whole")
    index = reprocess()
    assert index["conversations"] == [] and not (stream / "conversations/c1.jsonl").exists()
    assert [f["start"] for f in json.loads((stream / "forgotten.json").read_text())] == [
        "2026-01-01T00:00:10.000Z", "2026-01-01T00:00:00.000Z"]


@pytest.mark.parametrize("at", ["yesterday|today", "2026-01-01T00:00:00+00:00|soon", "2026-01-01T00:00:00+00:00"])
def test_taking_back_a_mark_that_isnt_two_times_writes_nothing(portal, at):
    client, _, labels_dir = portal
    log_in(client)
    before = {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()}
    response = client.post("/forget/undo", data={"conversation_id": "c1", "at": at}, follow_redirects=False)
    assert response.status_code == 303
    assert {p: p.read_bytes() for p in labels_dir.rglob("*") if p.is_file()} == before


def test_a_tag_can_be_undone_and_the_page_s_script_gets_no_page_back(portal):
    client, _, labels_dir = portal
    script = {"X-Requested-With": "fetch"}
    assert client.post("/turn", data={"turn_id": "a0", "category": "_media"}, headers=script).status_code == 401
    log_in(client)

    assert client.post("/turn", data={"turn_id": "a0", "category": "_media"}, headers=script).status_code == 204
    assert client.post("/turn", data={"turn_id": "a0", "category": "Alice"}, headers=script).status_code == 404
    assert "/audio/a0" not in client.get("/").text
    assert client.post("/turn/undo", data={"turn_id": "a0"}, headers=script).status_code == 204
    assert "/audio/a0" in client.get("/").text  # offered again
    tagged, undone = [json.loads(line) for line in (labels_dir / "tags.jsonl").read_text().splitlines()]
    assert (tagged["name"], undone["name"], undone["at"]) == ("_media", "", tagged["at"])


def test_a_conversation_is_held_until_released_from_its_page_once_it_has_ended(portal, monkeypatch):
    from hearsay.affect import SCHEMA as AFFECT_SCHEMA
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.delivery import SCHEMA as DELIVERY_SCHEMA
    from hearsay.hold import DELAY, record_releases
    from hearsay.stream import write_stream

    client, db_path, labels_dir = portal
    log_in(client)
    now = time.time()
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + AFFECT_SCHEMA + DELIVERY_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 32, 0)", (now - 3600.4, now - 3000.7))
    db.execute("INSERT INTO conversations VALUES ('c2', ?, ?, 5, 1)", (now - 100.3, now - 10.2))  # still going
    db.execute("INSERT INTO conversation_audio VALUES ('c2', '2026-01-02T00:00:00+00:00', 10, 1, NULL, NULL, 0)")
    db.commit()
    db.close()

    page = client.get("/conversations/c1").text
    assert "Idea Machine: held until" in page and "Release to Idea Machine</button>" in page
    assert "Release to Idea Machine" not in client.get("/conversations/c2").text
    client.post("/release", data={"conversation_id": "c2"})
    assert not (labels_dir / "release.jsonl").exists()

    # The button asks on a page of its own, not in a dialog a web view can skip; asking writes nothing.
    assert "<form class='release' method='get' action='/release'>" in page
    asked = client.get("/release", params={"conversation_id": "c1", "back": "//evil.example"})
    assert "Release to Idea Machine?" in asked.text and "<form method=\"post\" action=\"/release\">" in asked.text
    assert "<a href=\"/conversations/c1\">Keep it held</a>" in asked.text and not (labels_dir / "release.jsonl").exists()
    assert client.get("/release", params={"conversation_id": "c2"}, follow_redirects=False).status_code == 303

    def published(at: float) -> str:
        record_releases(db_path, labels_dir, at)
        write_stream(db_path, labels_dir / "stream", at)
        return client.get("/conversations/c1").text

    # Released, it waits DELAY: a reprocess before then leaves it held, and it can be cancelled.
    page = client.post("/release", data={"conversation_id": "c1"}).text
    assert "unless you cancel" in page and "Cancel release" in page and "Release to" not in page
    assert client.get("/release", params={"conversation_id": "c1"}, follow_redirects=False).status_code == 303
    assert "Idea Machine: released, goes at" in published(now + DELAY - 5)
    page = client.post("/release/undo", data={"conversation_id": "c1"}).text
    assert "Idea Machine: held until" in page and "Release to Idea Machine</button>" in page
    assert "Idea Machine: held until" in published(now + DELAY + 5)

    # Released again, it's due DELAY after this press, and then it can't be cancelled.
    client.post("/release", data={"conversation_id": "c1"})
    monkeypatch.setattr(time, "time", lambda: now + 2 * DELAY)
    page = client.post("/release/undo", data={"conversation_id": "c1"}).text
    assert "Idea Machine: released: out at the next reprocess" in page and "Cancel release" not in page
    published(now + 2 * DELAY)
    listed = client.get("/conversations").text
    assert listed.count("in the stream") == 1 and listed.count("held until") == 1
    assert "Idea Machine: in the stream" in client.get("/conversations/c1").text


def test_delete_throws_a_turn_out_and_can_be_undone_until_a_reprocess_applies_it(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.forget import forget

    client, db_path, labels_dir = portal
    script = {"X-Requested-With": "fetch"}
    log_in(client)
    page = client.get("/").text
    assert "value='_mic'>Mic</button></div><div class='row'><button type='submit' name='category' value='_media'" in page
    assert "formaction='/turn/delete'>Delete</button></div>" in page

    assert client.post("/turn/delete", data={"turn_id": "a0"}, headers=script).status_code == 204
    assert "/audio/a0" not in client.get("/").text
    assert client.post("/turn/delete/undo", data={"turn_id": "a0"}, headers=script).status_code == 204
    assert "/audio/a0" in client.get("/").text
    marked, undone = [json.loads(line) for line in (labels_dir / "forget.jsonl").read_text().splitlines()]
    assert (marked["type"], undone["type"], marked["at"]) == (
        "forget", "undo", ["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:04+00:00"]) and undone["at"] == marked["at"]
    assert not (labels_dir / "tags.jsonl").exists()  # a delete isn't a tag

    client.post("/turn/delete", data={"turn_id": "a0"}, headers=script)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225600, 1767225640, 32, 0)")
    db.commit()
    db.close()
    assert forget(db_path, labels_dir) == {"stretches": 1, "turns_removed": 1}
    # Applied: the turn is gone, so there's nothing left to undo.
    assert client.post("/turn/delete/undo", data={"turn_id": "a0"}, headers=script).status_code == 404
    assert len((labels_dir / "forget.jsonl").read_text().splitlines()) == 3


def test_a_spot_that_isnt_a_place_stops_being_offered(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + PLACES_SCHEMA)
    # Readings that wander a little, so the spot's centre has more decimals than the page shows.
    db.executemany("INSERT INTO locations VALUES ('p', ?, ?, 80.14, 65)",
                   [(1_790_000_000 + 60 * i, -26.12 - 0.0000003 * i) for i in range(5)])
    db.commit()
    db.close()

    # The app says it isn't a place, with its centre as /places.json gave it.
    app = {"Authorization": "Bearer capture-token"}
    [spot] = client.get("/places.json", headers=app).json()["unnamed"]
    client.post("/places", headers=app, data={"action": "ignore", "latitude": str(spot["latitude"]),
                                              "longitude": str(spot["longitude"])})
    page = client.get("/places").text
    assert "Nowhere new." in page and "Not places (1)" in page
    # The page offers it again as it shows it.
    shown = dict(re.findall(r"name='(latitude|longitude)' value='([^']+)'", page))
    assert shown["latitude"] != str(spot["latitude"])
    client.post("/places", data={"action": "unignore", **shown})
    assert "About 5 min" in client.get("/places").text


def test_words_wait_in_a_draft_and_update_commits_them_with_lattice_s_all_at_once(portal, tmp_path):
    _, db_path, labels_dir = portal
    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({"format_version": 1, "terms": [
        {"term": "gnomon", "source": "repair", "heard_as": "no man"}, {"term": "Gutenberg", "source": "archive"}]}))
    client = TestClient(create_app(db_path, tmp_path / "audio", labels_dir, labels_dir / "stream", "me", "correct horse", "k" * 64,
                                   vocab_path=vocab))
    log_in(client)

    client.post("/words", data={"action": "save", "words": "  Xanthe\n\nisotherm\nXanthe\n"})
    page = client.get("/words").text
    assert "isotherm\nXanthe</textarea>" in page  # tidied, in order regardless of case
    assert not (labels_dir / "words.json").exists()  # a draft isn't committed

    # Lattice approves another word after the page was shown: that page's Update commits nothing.
    seen = page.split("name='seen' value='")[1].split("'")[0]
    vocab.write_text(json.dumps({"format_version": 1, "terms": [
        {"term": "gnomon", "source": "repair"}, {"term": "Gutenberg", "source": "archive"}, {"term": "Kelvin", "source": "archive"}]}))
    stale = client.post("/words", data={"action": "update", "seen": seen}, follow_redirects=False)
    assert "changed" in stale.headers["location"] and not (labels_dir / "words.json").exists()

    seen = client.get("/words").text.split("name='seen' value='")[1].split("'")[0]
    client.post("/words", data={"action": "update", "seen": seen})
    assert json.loads((labels_dir / "words.json").read_text()) == {
        "typed": ["isotherm", "Xanthe"], "lattice": ["gnomon", "Gutenberg", "Kelvin"]}
    assert not (labels_dir / "words.draft.txt").exists()
    assert "name='seen'" not in client.get("/words").text  # nothing left to update

    # Unreadable Lattice words never commit as "Lattice has none".
    vocab.write_text(json.dumps({"format_version": 2, "terms": []}))
    client.post("/words", data={"action": "save", "words": "Xanthe"})
    assert "name='seen'" not in client.get("/words").text


@pytest.fixture
def corral(portal, tmp_path, monkeypatch):
    """The portal set up with corral, whose calls are recorded instead of made: post answers job 7."""
    from hearsay import enqueue

    _, db_path, labels_dir = portal
    calls = []
    monkeypatch.setattr(enqueue, "post", lambda url, token, spec: calls.append(("post", spec)) or (7, True))
    monkeypatch.setattr(enqueue, "release", lambda url, token, job_id: calls.append(("release", job_id)))
    client = TestClient(create_app(db_path, tmp_path / "audio", labels_dir, labels_dir / "stream", "me", "correct horse",
                                   "k" * 64, corral_url="http://corral.test", corral_token="corral-token"))
    return client, labels_dir, calls


def test_each_edit_enqueues_one_reprocess_to_apply_it(corral):
    from hearsay.enqueue import APPLY

    client, labels_dir, calls = corral
    log_in(client)
    script = {"X-Requested-With": "fetch"}
    assert client.post("/turn", data={"turn_id": "a0", "category": "_media"}, headers=script).status_code == 204
    assert calls == [("post", APPLY)]
    # A move writes a record per spot, and is still one edit.
    client.post("/places", data={"action": "move", "from": "Back Room", "at": ["-26.13,80.15", "-26.125,80.145"]})
    assert len((labels_dir / "places.jsonl").read_text().splitlines()) == 3 and len(calls) == 2
    # Nothing written, nothing to apply.
    client.post("/places", data={"action": "name", "name": "Back Room"})
    client.post("/turn", data={"turn_id": "a0", "category": "Alice"})
    assert len(calls) == 2


def test_each_release_enqueues_a_reprocess_of_its_own_held_until_it_is_due(portal, corral):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.enqueue import APPLY
    from hearsay.hold import DELAY

    _, db_path, _ = portal
    client, labels_dir, calls = corral
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA)
    db.execute("INSERT INTO conversations VALUES ('c1', ?, ?, 32, 0)", (time.time() - 3600, time.time() - 3000))
    db.commit()
    db.close()
    log_in(client)
    client.post("/release", data={"conversation_id": "c1"})
    client.post("/release/undo", data={"conversation_id": "c1"})  # not an edit a reprocess needs
    client.post("/release", data={"conversation_id": "c1"})
    specs = [spec for _, spec in calls]
    # Under APPLY's key, or one press's, it would coalesce into a job that may run before it's due.
    assert len(specs) == 2 and len({spec["key"] for spec in specs} | {APPLY["key"]}) == 3
    assert all(spec["wait_seconds"] >= DELAY and spec["quiet_seconds"] >= DELAY for spec in specs)



def test_a_words_update_enqueues_the_whole_pipeline(portal, tmp_path, monkeypatch):
    from hearsay import enqueue

    _, db_path, labels_dir = portal
    calls = []
    monkeypatch.setattr(enqueue, "post", lambda url, token, spec: calls.append(spec) or (7, True))
    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({"format_version": 1, "terms": [{"term": "gnomon", "source": "repair"}]}))
    client = TestClient(create_app(db_path, tmp_path / "audio", labels_dir, labels_dir / "stream", "me", "correct horse",
                                   "k" * 64, vocab_path=vocab, corral_url="http://corral.test", corral_token="t"))
    log_in(client)
    client.post("/words", data={"action": "save", "words": "Xanthe"})
    assert calls == []  # a draft changes nothing yet
    seen = client.get("/words").text.split("name='seen' value='")[1].split("'")[0]
    client.post("/words", data={"action": "update", "seen": seen})
    # Words change transcription, which a reprocess alone (APPLY) doesn't redo.
    assert calls == [enqueue.CHAIN]


def test_an_edit_is_kept_when_corral_cant_be_reached(corral, monkeypatch, caplog):
    import urllib.error

    from hearsay import enqueue

    client, labels_dir, _ = corral
    log_in(client)

    def refused(url, token, spec):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(enqueue, "post", refused)
    response = client.post("/turn", data={"turn_id": "a0", "category": "_media"}, headers={"X-Requested-With": "fetch"})
    assert response.status_code == 204 and "_media" in (labels_dir / "tags.jsonl").read_text()
    assert "couldn't enqueue on corral" in caplog.text
    # Apply now says it didn't, and that the edits are kept.
    response = client.post("/apply", data={"back": "/"}, follow_redirects=False)
    assert response.status_code == 502 and "Your edits are saved" in response.text


def test_apply_now_releases_the_reprocess_it_enqueues(portal, corral):
    from hearsay.enqueue import APPLY

    client, _, calls = corral
    assert client.post("/apply", data={"back": "/"}, headers={"X-Requested-With": "fetch"}).status_code == 401
    assert client.post("/apply", data={"back": "/"}, follow_redirects=False).headers["location"] == "/login"
    assert calls == []
    log_in(client)

    assert "action='/apply'" in client.get("/").text and "name='back' value='/places'" in client.get("/places").text
    response = client.post("/apply", data={"back": "/places"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/places"
    assert calls == [("post", APPLY), ("release", 7)]
    assert client.post("/apply", data={"back": "/"}, headers={"X-Requested-With": "fetch"}).status_code == 204
    # Without corral (local dev) there's no Apply now to offer.
    plain, _, _ = portal
    log_in(plain)
    assert "action='/apply'" not in plain.get("/").text

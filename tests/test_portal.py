import io
import json
import sqlite3
import wave

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

from hearsay.assemble import SCHEMA as ASSEMBLE_SCHEMA  # noqa: E402
from hearsay.media import SCHEMA as MEDIA_SCHEMA  # noqa: E402
from hearsay.parse import SCHEMA as PARSE_SCHEMA  # noqa: E402
from hearsay.places import SCHEMA as PLACES_SCHEMA, read_places  # noqa: E402
from hearsay.portal import create_app  # noqa: E402
from hearsay.speakers import SCHEMA as SPEAKERS_SCHEMA  # noqa: E402
from hearsay.turns import SCHEMA as TURNS_SCHEMA, record_operator_input  # noqa: E402


@pytest.fixture
def portal(tmp_path):
    """Eight 4 s turns in one conversation WAV: Hearsay couldn't tell who said the a turns, the b turns aren't the owner."""
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
    db.executescript(PARSE_SCHEMA + ASSEMBLE_SCHEMA + TURNS_SCHEMA + SPEAKERS_SCHEMA + PLACES_SCHEMA + MEDIA_SCHEMA)
    db.execute("INSERT INTO conversation_audio VALUES ('c1', '2026-01-01T00:00:00+00:00', 40, 1, 'c1.wav', 'sha')")
    for idx in range(8):
        turn_id = f"{'ab'[idx % 2]}{idx}"
        db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                   (turn_id, "c1", idx, 5.0 * idx, 5.0 * idx + 4, "synthetic", None, None))
        label = (None, "not_owner")[idx % 2]
        db.execute("INSERT INTO turn_speakers VALUES (?,?,?,?,?,?,?)",
                   (turn_id, "c1", 1.0, None, 0.2 if label is None else 0.05, label, "voice" if label else None))
    db.commit()
    db.close()
    record_operator_input(db_path, labels_dir)

    app = create_app(db_path, audio_dir, labels_dir, "me", "correct horse", "k" * 64, app_token="capture-token")
    return TestClient(app), db_path, labels_dir


def log_in(client):
    return client.post("/login", data={"username": "me", "password": "correct horse"}, follow_redirects=False)


def test_nothing_is_reachable_without_logging_in(portal):
    client, _, _ = portal
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/audio/a0").status_code == 401

    wrong = client.post("/login", data={"username": "me", "password": "nope"}, follow_redirects=False)
    assert wrong.status_code == 200 and "set-cookie" not in wrong.headers
    client.cookies.set("hearsay_session", "9999999999.forged")
    assert client.get("/", follow_redirects=False).status_code == 303

    client.cookies.clear()
    assert log_in(client).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_the_app_logs_in_with_the_capture_token(portal):
    client, _, _ = portal
    for wrong in ({}, {"Authorization": "Bearer nope"}):
        response = client.post("/login/app", headers=wrong, follow_redirects=False)
        assert response.status_code == 401 and "set-cookie" not in response.headers
    response = client.post("/login/app", headers={"Authorization": "Bearer capture-token"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_turn_audio_supports_byte_ranges_for_ios(portal):
    client, _, _ = portal
    log_in(client)
    full = client.get("/audio/a2")  # turn [10, 14) s
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    with wave.open(io.BytesIO(full.content)) as w:
        assert w.getnframes() == 4 * 16000
        assert int.from_bytes(w.readframes(1), "little") == 10

    probe = client.get("/audio/a2", headers={"Range": "bytes=0-1"})
    assert probe.status_code == 206
    assert probe.content == full.content[:2]
    assert probe.headers["content-range"] == f"bytes 0-1/{len(full.content)}"


def test_a_spot_where_the_owner_spent_time_can_be_named(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA
    from hearsay.places import SCHEMA as PLACES_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + PLACES_SCHEMA)
    # Readings around the fixture's conversation (its WAV starts at 2026-01-01 00:00 UTC).
    db.executemany("INSERT INTO locations VALUES ('p', ?, 26.12, -80.14, 65)", [(1_767_225_590 + 60 * i,) for i in range(5)])
    db.execute("INSERT INTO conversations VALUES ('c1', 1767225590, 1767225900, 32, 0)")
    db.commit()
    db.close()

    page = client.get("/places").text
    assert "About 5 min" in page and "maps.apple.com/?ll=26.12000,-80.14000" in page
    assert page.count("<audio") == 3  # clips heard there, to tell where it is
    # The app's map: the conversations there, each with its first words, to tell by what was said.
    [spot] = client.get("/places.json", headers={"Authorization": "Bearer capture-token"}).json()["unnamed"]
    assert [(c["start"], c["opening"]) for c in spot["conversations"]] == [(1_767_225_590, "synthetic synthetic")]

    client.post("/places", data={"action": "name", "name": "Chill Room", "latitude": "26.12", "longitude": "-80.14"})
    page = client.get("/places").text
    assert "Nowhere new." in page and "<strong>Chill Room</strong>" in page
    [record] = [json.loads(line) for line in (labels_dir / "places.jsonl").read_text().splitlines()]
    assert (record["type"], record["name"], record["latitude"]) == ("place", "Chill Room", 26.12)

    # Anchored where it really is, from a pasted Maps link; readings near the old spot are offered again.
    client.post("/places", data={"action": "move", "from": "Chill Room",
                                 "at": "https://maps.apple.com/?ll=26.13000%2C-80.15000&q=Pin"})
    assert read_places(labels_dir) == [("Chill Room", 26.13, -80.15, 100.0)]
    assert "About 5 min" in client.get("/places").text

    # The app's map: the token alone, no session; dragging one of a place's two spots keeps both.
    client.cookies.clear()
    app = {"Authorization": "Bearer capture-token"}
    assert client.get("/places.json", headers={"Authorization": "Bearer wrong"}).status_code == 401
    client.post("/places", headers=app, data={"action": "name", "name": "Chill Room", "latitude": "26.12", "longitude": "-80.14"})
    places = client.get("/places.json", headers=app).json()
    assert len(places["named"]) == 2 and places["unnamed"] == []
    client.post("/places", headers=app, data={"action": "move", "from": "Chill Room", "at": ["26.13,-80.15", "26.125,-80.145"],
                                              "radius": ["100", "400"]})
    assert read_places(labels_dir) == [("Chill Room", 26.13, -80.15, 100.0), ("Chill Room", 26.125, -80.145, 400.0)]
    # A move pasted on the web page, with no radius, keeps the area set.
    client.post("/places", headers=app, data={"action": "move", "from": "Chill Room", "at": "26.12,-80.14"})
    assert read_places(labels_dir) == [("Chill Room", 26.12, -80.14, 400.0)]

    # Its conversations, each with the span of readings that put it there; dropping that span takes it off.
    [visit] = client.get("/places.json", headers=app).json()["visits"]["Chill Room"]
    assert visit["readings"] == [1_767_225_590, 1_767_225_830]
    client.post("/places", headers=app, data={"action": "drop", "start": "1767225590", "end": "1767225830"})
    assert client.get("/places.json", headers=app).json()["visits"] == {}


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
    assert "4.0 s · Jan 1 00:00 UTC · Blind Monk" in page  # a0, at 0 s
    assert "Jan 1 00:00 UTC · Home" in page  # a4, at 20 s


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


def test_a_spot_that_isnt_a_place_stops_being_offered(portal):
    from hearsay.conversations import SCHEMA as CONVERSATIONS_SCHEMA

    client, db_path, labels_dir = portal
    log_in(client)
    db = sqlite3.connect(db_path)
    db.executescript(CONVERSATIONS_SCHEMA + PLACES_SCHEMA)
    db.executemany("INSERT INTO locations VALUES ('p', ?, 26.12, -80.14, 65)", [(1_790_000_000 + 60 * i,) for i in range(5)])
    db.commit()
    db.close()

    client.post("/places", data={"action": "ignore", "latitude": "26.120000", "longitude": "-80.140000"})
    page = client.get("/places").text
    assert "Nowhere new." in page and "Not places (1)" in page
    client.post("/places", data={"action": "unignore", "latitude": "26.120000", "longitude": "-80.140000"})
    assert "About 5 min" in client.get("/places").text


def test_words_wait_in_a_draft_and_update_commits_them_with_lattice_s_all_at_once(portal, tmp_path):
    _, db_path, labels_dir = portal
    vocab = tmp_path / "vocab.json"
    vocab.write_text(json.dumps({"format_version": 1, "terms": [
        {"term": "goetia", "source": "repair", "heard_as": "go Asia"}, {"term": "Gurdjieff", "source": "archive"}]}))
    client = TestClient(create_app(db_path, tmp_path / "audio", labels_dir, "me", "correct horse", "k" * 64,
                                   vocab_path=vocab))
    log_in(client)

    client.post("/words", data={"action": "save", "words": "  Siobhan\n\nhyperstition\nSiobhan\n"})
    page = client.get("/words").text
    assert "hyperstition\nSiobhan</textarea>" in page  # tidied, in order regardless of case
    assert not (labels_dir / "words.json").exists()  # a draft isn't committed

    # Lattice approves another word after the page was shown: that page's Update commits nothing.
    seen = page.split("name='seen' value='")[1].split("'")[0]
    vocab.write_text(json.dumps({"format_version": 1, "terms": [
        {"term": "goetia", "source": "repair"}, {"term": "Gurdjieff", "source": "archive"}, {"term": "Kabbalah", "source": "archive"}]}))
    stale = client.post("/words", data={"action": "update", "seen": seen}, follow_redirects=False)
    assert "changed" in stale.headers["location"] and not (labels_dir / "words.json").exists()

    seen = client.get("/words").text.split("name='seen' value='")[1].split("'")[0]
    client.post("/words", data={"action": "update", "seen": seen})
    assert json.loads((labels_dir / "words.json").read_text()) == {
        "typed": ["hyperstition", "Siobhan"], "lattice": ["goetia", "Gurdjieff", "Kabbalah"]}
    assert not (labels_dir / "words.draft.txt").exists()
    assert "name='seen'" not in client.get("/words").text  # nothing left to update

    # Unreadable Lattice words never commit as "Lattice has none".
    vocab.write_text(json.dumps({"format_version": 2, "terms": []}))
    client.post("/words", data={"action": "save", "words": "Siobhan"})
    assert "name='seen'" not in client.get("/words").text

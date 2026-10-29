"""Web portal for correcting who spoke, from a phone or any browser.

Listens only on the LAN and tailnet addresses (install/compose.yaml), never
the public internet. Login is a plain HTML form, not an HTTP auth
dialog, so password managers can fill it; sessions are HMAC-signed cookies.
The iPhone app logs in instead with the capture token it already holds
(POST /login/app, bearer): the portal shows in its Tag tab, and whoever
holds that token can already send Hearsay audio. The app's own Places map
sends the token on each request instead: GET /places.json, and POST /places
with the same fields as the page's forms.

Hearsay says only whether the owner spoke (2026-10-28). The front page plays
the turns it couldn't decide, and the operator tags each: Me, Someone else,
or not speech (media, music, noise). A tag is written to labels/tags.jsonl,
the same durable input reprocess reads, with the turn's time span so it
survives re-transcription, and applies to that turn whatever its voice said
(hearsay.turns.categories).

The Words page edits the words transcription is nudged toward
(hearsay/words.py): a draft kept until the operator presses Update, which
commits it with Lattice's approved terms all at once.
"""

import asyncio
import bisect
import hashlib
import hmac
import html
import io
import json
import os
import re
import sqlite3
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from hearsay.places import (
    LOOKBACK,
    MAX_RADIUS,
    MIN_RADIUS,
    RADIUS,
    read_ignored,
    read_places,
    read_readings,
    unnamed_spots,
    visits,
)
from hearsay.turns import read_operator_files, resolve, wall
from hearsay.words import boosted, commit, read_committed, read_draft, read_lattice, save_draft, tidy

COOKIE = "hearsay_session"
SESSION_SECONDS = 30 * 24 * 3600
# Turns Hearsay couldn't decide, shown at a time, and the shortest worth a listen.
UNSURE = 20
MIN_UNSURE_SECONDS = 1.0
# Clips played per unnamed place, to tell by ear where it is.
PLACE_CLIPS = 3
# For the app's Places map: an unnamed spot's latest conversations, each with
# its first turns cut short, to tell by what was said where it is.
SPOT_CONVERSATIONS = 10
# And a named place's, to correct one that was put there wrongly.
PLACE_CONVERSATIONS = 20
OPENING_TURNS = 2
OPENING_CHARACTERS = 200
# A clip's words, shown with it so the operator knows which voice is meant.
CLIP_CHARACTERS = 280

UNSURE_SQL = """
SELECT t.turn_id, t.conversation_id, t.start, t.end, t.text, ca.zero_at
FROM turns t
JOIN turn_speakers ts ON ts.turn_id = t.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE ts.label IS NULL AND ca.wav_file IS NOT NULL AND t.end - t.start >= ?
ORDER BY ca.zero_at DESC, t.start DESC
"""

STYLE = """
:root { color-scheme: light dark; --fg: #111; --bg: #fff; --muted: #666; --line: #ddd; --accent: #2458d6; }
@media (prefers-color-scheme: dark) { :root { --fg: #eee; --bg: #121212; --muted: #999; --line: #333; --accent: #7fa4ff; } }
body { font: 18px/1.4 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0 auto; max-width: 40rem; padding: 1rem; }
a { color: var(--accent); }
.muted { color: var(--muted); font-size: 0.9em; }
ul { list-style: none; padding: 0; }
li { border-bottom: 1px solid var(--line); padding: 0.75rem 0; }
li a { display: block; text-decoration: none; }
audio { width: 100%; margin: 0.4rem 0; }
label { display: block; margin: 0.8rem 0 0.3rem; }
input { font: inherit; width: 100%; box-sizing: border-box; padding: 0.6rem; }
button { font: inherit; width: 100%; padding: 0.8rem; margin-top: 0.6rem; }
.error { color: #c33; }
.clip { margin-bottom: 1.2rem; }
.words { margin: 0.2rem 0; }
.clip input { padding: 0.4rem; }
.clip button { padding: 0.4rem; margin-top: 0.3rem; }
.row { display: flex; gap: 0.5rem; }
.row button { flex: 1; }
.clip .row button { font-size: 0.85em; }
.hinted { font-weight: 600; outline: 2px solid var(--accent); }
textarea { font: inherit; width: 100%; box-sizing: border-box; padding: 0.6rem; min-height: 12rem; }
.added { color: #2a8a3a; }
.removed { color: #c33; text-decoration: line-through; }
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def wav_slice(path: Path, start: float, end: float) -> bytes:
    with wave.open(str(path)) as src:
        rate = src.getframerate()
        src.setpos(max(0, min(src.getnframes(), int(start * rate))))
        frames = src.readframes(max(0, int((end - start) * rate)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue()


def ranged(data: bytes, range_header: str | None) -> Response:
    """Serve bytes, honoring a single byte range. iOS Safari won't play audio without it."""
    headers = {"Accept-Ranges": "bytes"}
    if not range_header or not range_header.startswith("bytes=") or "," in range_header:
        return Response(data, media_type="audio/wav", headers=headers)
    first, _, last = range_header[len("bytes="):].partition("-")
    try:
        if first:
            start, end = int(first), int(last) if last else len(data) - 1
        else:
            start, end = max(0, len(data) - int(last)), len(data) - 1
    except ValueError:
        return Response(data, media_type="audio/wav", headers=headers)
    end = min(end, len(data) - 1)
    if start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return Response(data[start : end + 1], status_code=206, media_type="audio/wav", headers=headers)


# One tap each. hearsay/stream.py keeps _me (the owner) and _stranger
# (someone else present), and leaves the rest out: not speech from anyone there.
CATEGORIES = {"_me": "Me", "_stranger": "Someone else", "_media": "Media", "_music": "Music", "_noise": "Noise"}


def clip_player(s: dict) -> str:
    """A turn's audio with its words, when and where it was said, and a button per category."""
    words = s["text"] if len(s["text"]) <= CLIP_CHARACTERS else s["text"][:CLIP_CHARACTERS].rsplit(" ", 1)[0] + " …"
    said = [f"{s['end'] - s['start']:.1f} s", f"{datetime.fromtimestamp(s['at'], timezone.utc):%b %-d %H:%M} UTC"]
    said += [s["place"]] if s["place"] else []
    buttons = "".join(f"<button type='submit' name='category' value='{value}'>{label}</button>"
                      for value, label in CATEGORIES.items())
    return (f"<audio controls preload='none' src='/audio/{quote(s['turn_id'])}'></audio>"
            f"<div class='words'>{html.escape(words)}</div>"
            f"<div class='muted'>{html.escape(' · '.join(said))}</div>"
            f"<form class='clip' method='post' action='/turn'>"
            f"<input type='hidden' name='turn_id' value='{html.escape(s['turn_id'])}'>"
            f"<div class='row'>{buttons}</div></form>")


def coordinates(text: str) -> tuple[float, float] | None:
    """Latitude and longitude from pasted text: "26.12, -80.14", or an Apple or
    Google Maps link that has them in it (ll=, q=, @). Not an address: looking
    one up would send it to a geocoding service."""
    found = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", unquote(text))
    if not found:
        return None
    lat, lon = float(found[1]), float(found[2])
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else None


def heard_at_spot(spot_times: list[float], times: list[float], turns: list[tuple]) -> list[tuple]:
    """The longest turns recorded while the phone was at a spot, one per
    conversation first. A turn is where the latest reading before it was, as
    in hearsay/places.py. turns are (turn_id, conversation_id, wall start,
    duration); times are every reading's, in order."""
    at_spot = set(spot_times)
    found = []
    for turn in turns:
        i = bisect.bisect_right(times, turn[2]) - 1
        if i >= 0 and times[i] in at_spot and turn[2] - times[i] <= LOOKBACK:
            found.append(turn)
    found.sort(key=lambda t: (-t[3], t[0]))
    picked, conversations = [], set()
    for turn in found:
        if turn[1] not in conversations:
            picked.append(turn)
            conversations.add(turn[1])
    return (picked + [t for t in found if t not in picked])[:PLACE_CLIPS]


def create_app(db_path: Path, audio_dir: Path, labels_dir: Path, user: str, password: str, session_key: str,
               app_token: str = "", vocab_path: Path | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    tags_path = labels_dir / "tags.jsonl"

    def sign(expires: int) -> str:
        return hmac.new(session_key.encode(), f"{user}:{expires}".encode(), hashlib.sha256).hexdigest()

    def logged_in(request: Request) -> bool:
        # The iPhone app's own screens (GET /places.json) send the capture token on each request.
        sent = request.headers.get("authorization", "")
        if app_token and sent.startswith("Bearer "):
            return hmac.compare_digest(sent.removeprefix("Bearer ").encode(), app_token.encode())
        expires, _, signature = request.cookies.get(COOKIE, "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(signature, sign(int(expires)))

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def tagged() -> set[str]:
        """Turns the operator has given a category, as of now rather than the last reprocess."""
        db = connect()
        try:
            found = resolve(db, *read_operator_files(labels_dir))
        finally:
            db.close()
        return {t for t, value in found.clips.items() if value.startswith("_")}

    def load_unsure() -> list[dict]:
        """Turns Hearsay couldn't decide and the operator hasn't tagged, latest first, with
        when each was said (unix seconds) and the named place it was said at."""
        db = connect()
        db.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in db.execute(UNSURE_SQL, (MIN_UNSURE_SECONDS,))]
            places = {}
            for conversation_id, place, start, end in db.execute(
                    "SELECT conversation_id, place, start, end FROM conversation_places ORDER BY start"):
                places.setdefault(conversation_id, []).append((place, start, end))
        finally:
            db.close()
        done = tagged()
        rows = [row for row in rows if row["turn_id"] not in done]
        for row in rows:
            row["at"] = datetime.fromisoformat(row["zero_at"]).timestamp() + row["start"]
            # A place lasts until the next one starts (hearsay/places.py).
            row["place"] = next((place for place, start, end in reversed(places.get(row["conversation_id"], []))
                                 if start <= row["at"] <= end), None)
        return rows

    def append_tag(record: dict, path: Path = tags_path) -> None:
        record["tagged_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def login_page(error: str = "") -> HTMLResponse:
        message = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        return page("Hearsay login", f"""
<h1>Hearsay</h1>{message}
<form method="post" action="/login">
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" autocapitalize="none" required>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Log in</button>
</form>""")

    @app.get("/login")
    async def login_form() -> HTMLResponse:
        return login_page()

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        given_user = form.get("username", [""])[0]
        given_password = form.get("password", [""])[0]
        user_ok = hmac.compare_digest(given_user.encode(), user.encode())
        password_ok = hmac.compare_digest(given_password.encode(), password.encode())
        if not (user_ok and password_ok):
            await asyncio.sleep(1)  # slows guessing
            return login_page("Wrong username or password.")
        return session()

    @app.post("/login/app")
    async def app_login(request: Request) -> Response:
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not app_token or not hmac.compare_digest(sent.encode(), app_token.encode()):
            await asyncio.sleep(1)
            return Response(status_code=401)
        return session()

    def session() -> Response:
        expires = int(time.time()) + SESSION_SECONDS
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, f"{expires}.{sign(expires)}", max_age=SESSION_SECONDS,
                            httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/")
    async def index(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        unsure = load_unsure()
        players = "".join(f"<li>{clip_player(s)}</li>" for s in unsure[:UNSURE])
        return page("Hearsay", f"""
<h1>Not sure who spoke</h1>
<p class="muted">{len(unsure)} turn(s) Hearsay couldn't tell were you or not, latest first.
Tag each; it applies at the next reprocess. · <a href="/places">Places</a> · <a href="/words">Words</a></p>
<ul>{players or "<li class='muted'>Nothing to check.</li>"}</ul>
<h2>Transcription</h2><ul><li><a href="/words">Words <span class="muted">· {len(boosted(read_committed(labels_dir)))} in use
{"· a draft is waiting" if read_draft(labels_dir) is not None else ""}</span></a></li></ul>
<form method="post" action="/logout"><button type="submit">Log out</button></form>""")

    @app.post("/turn")
    async def tag_turn(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode())
        turn_id = form.get("turn_id", [""])[0]
        category = form.get("category", [""])[0]
        db = connect()
        try:
            turn = db.execute(
                "SELECT ca.zero_at, t.start, t.end FROM turns t"
                " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id WHERE t.turn_id = ?",
                (turn_id,)).fetchone()
        finally:
            db.close()
        if turn and category in CATEGORIES:
            # Absolute times, so the tag survives new transcripts and boundaries;
            # the turn id only for reference.
            append_tag({"type": "clip", "at": [wall(*turn)], "turn_ids": [turn_id], "name": category})
        return RedirectResponse("/", status_code=303)

    @app.get("/places")
    async def show_places(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        spots = read_places(labels_dir)
        ignored = read_ignored(labels_dir)
        db = connect()
        try:
            readings = read_readings(db, labels_dir, spots)
            turns = [(turn_id, cid, datetime.fromisoformat(zero_at).timestamp() + start, end - start)
                     for turn_id, cid, zero_at, start, end in db.execute(
                         "SELECT t.turn_id, t.conversation_id, ca.zero_at, t.start, t.end FROM turns t"
                         " JOIN conversation_audio ca USING (conversation_id) WHERE ca.wav_file IS NOT NULL")]
            visited = dict(db.execute("SELECT place, count(DISTINCT conversation_id) FROM conversation_places GROUP BY place"))
        finally:
            db.close()

        def maps(lat: float, lon: float, label: str) -> str:
            return f"<a href='https://maps.apple.com/?ll={lat:.5f},{lon:.5f}&q={quote(label)}'>Open in Maps</a>"

        named = ""
        for name in sorted({sp[0] for sp in spots}):
            _, lat, lon, radius = next(sp for sp in spots if sp[0] == name)
            named += (f"<li><strong>{html.escape(name)}</strong> <span class='muted'>"
                      f"{visited.get(name, 0)} conversation(s) as of the last reprocess"
                      f"{f' · within {radius:.0f} m' if radius != RADIUS else ''} · {maps(lat, lon, name)}</span>"
                      f"<form class='clip' method='post' action='/places'><input type='hidden' name='from' value='{html.escape(name)}'>"
                      "<input name='name' placeholder='New name (an existing one merges them)' autocomplete='off'>"
                      "<button type='submit' name='action' value='rename'>Rename</button>"
                      "<button type='submit' name='action' value='forget' formnovalidate>Forget</button></form>"
                      f"<form class='clip' method='post' action='/places'><input type='hidden' name='from' value='{html.escape(name)}'>"
                      "<input name='at' placeholder='Coordinates or a Maps link' autocomplete='off' required>"
                      "<button type='submit' name='action' value='move'>Move here</button></form></li>")
        reading_times = [at for at, _, _, _ in readings]
        unnamed = ""
        for spot in unnamed_spots([r[:3] for r in readings if r[3] is None], spots, ignored):
            times = spot["times"]
            days = sorted({datetime.fromtimestamp(at, timezone.utc).strftime("%b %-d") for at in times})
            lat, lon = spot["latitude"], spot["longitude"]
            clips = "".join(
                f"<audio controls preload='none' src='/audio/{quote(turn_id)}'></audio><div class='muted'>"
                f"{datetime.fromtimestamp(at, timezone.utc):%b %-d %H:%M} UTC · {seconds:.0f} s</div>"
                for turn_id, _, at, seconds in heard_at_spot(times, reading_times, turns))
            unnamed += (f"<li>About {len(times)} min on {', '.join(days)} (UTC)"
                        f"<div class='muted'>{maps(lat, lon, 'Unnamed place')}</div>{clips}"
                        f"<form class='clip' method='post' action='/places'>"
                        f"<input type='hidden' name='latitude' value='{lat:.6f}'><input type='hidden' name='longitude' value='{lon:.6f}'>"
                        "<input name='name' placeholder='Name this place (an existing name adds to it)' autocomplete='off' required>"
                        "<button type='submit' name='action' value='name'>Save place</button>"
                        "<button type='submit' name='action' value='ignore' formnovalidate>Not a place</button></form></li>")
        not_places = "".join(
            f"<li>{maps(lat, lon, 'Not a place')}<form class='clip' method='post' action='/places'>"
            f"<input type='hidden' name='latitude' value='{lat:.6f}'><input type='hidden' name='longitude' value='{lon:.6f}'>"
            "<button type='submit' name='action' value='unignore'>Offer again</button></form></li>"
            for lat, lon in ignored)
        return page("Hearsay places", f"""
<p><a href="/">← Hearsay</a></p>
<h1>Places</h1>
<p class="muted">Where conversations happened, from the phone's location while it records. Only names you give
reach the stream, never coordinates; a reading counts as a place within 100 m of it, or the area set
in the app's Places map. To put a place
exactly where it is, find it in Maps, drop a pin, copy its coordinates, and paste them under Move here.</p>
<h2>To name</h2><ul>{unnamed or "<li class='muted'>Nowhere new.</li>"}</ul>
<h2>Named</h2><ul>{named or "<li class='muted'>None yet.</li>"}</ul>
{f"<h2>Not places ({len(ignored)})</h2><ul>{not_places}</ul>" if ignored else ""}""")

    @app.get("/places.json")
    async def places_json(request: Request) -> Response:
        """For the iPhone app's map: every named spot, and the spots to name."""
        if not logged_in(request):
            return Response(status_code=401)
        spots = read_places(labels_dir)
        db = connect()
        try:
            readings = read_readings(db, labels_dir, spots)
            conversations = db.execute("SELECT conversation_id, start, end FROM conversations ORDER BY start DESC").fetchall()
            opening = {}
            for conversation_id, text in db.execute(
                "SELECT conversation_id, text FROM turns WHERE idx < ? ORDER BY conversation_id, idx", (OPENING_TURNS,)
            ):
                opening[conversation_id] = f"{opening.get(conversation_id, '')} {text}".strip()
        finally:
            db.close()

        def entry(cid: str, start: float, end: float, at: list[float]) -> dict:
            # When and how it began, to tell from what was said where it was;
            # "readings" is the span of the readings that put it there, to correct.
            return {"start": start, "end": end, "opening": opening.get(cid, "")[:OPENING_CHARACTERS],
                    "readings": [min(at), max(at)]}

        def at_spot(times: list[float]) -> list[dict]:
            found = []
            for cid, start, end in conversations:
                at = [t for t in times if start <= t <= end]
                if at:
                    found.append(entry(cid, start, end, at))
            return found[:SPOT_CONVERSATIONS]

        # Each named place's conversations, latest first, as reprocess will
        # find them with the corrections so far.
        times, names = [r[0] for r in readings], [r[3] for r in readings]
        visited = {}
        for cid, start, end in conversations:
            for place in {place for place, _, _ in visits(times, names, start, end)}:
                at = [t for t, name in zip(times, names) if name == place and start - LOOKBACK <= t <= end]
                if at and len(visited.setdefault(place, [])) < PLACE_CONVERSATIONS:
                    visited[place].append(entry(cid, start, end, at))

        unnamed = unnamed_spots([r[:3] for r in readings if r[3] is None], spots, read_ignored(labels_dir))
        return JSONResponse({
            "named": [{"name": name, "latitude": lat, "longitude": lon, "radius": radius}
                      for name, lat, lon, radius in spots],
            "visits": visited,
            "unnamed": [{"latitude": s["latitude"], "longitude": s["longitude"], "minutes": len(s["times"]),
                         "conversations": at_spot(s["times"])} for s in unnamed]})

    @app.post("/places")
    async def tag_place(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode())

        def field(key: str) -> str:
            return " ".join(form.get(key, [""])[0].split())

        action, name = field("action"), field("name")
        places_path = labels_dir / "places.jsonl"
        def radius(text: str, default: float = RADIUS) -> float:
            try:
                return min(max(float(text), MIN_RADIUS), MAX_RADIUS)
            except ValueError:
                return default

        def place(name: str, lat: float, lon: float, r: float) -> dict:
            # Only a radius the operator set is written, so the default can still change.
            return {"type": "place", "name": name, "latitude": lat, "longitude": lon,
                    **({"radius": r} if r != RADIUS else {})}

        if action == "name" and name:
            append_tag(place(name, float(field("latitude")), float(field("longitude")), radius(field("radius"))),
                       places_path)
        elif action == "rename" and name and field("from") and name != field("from"):
            append_tag({"type": "rename", "from": field("from"), "to": name}, places_path)
        elif action in ("ignore", "unignore"):
            # The coordinates as the page showed them, so "Offer again" matches exactly.
            append_tag({"type": action, "latitude": float(field("latitude")),
                        "longitude": float(field("longitude"))}, places_path)
        elif action == "forget" and field("from"):
            append_tag({"type": "forget", "name": field("from")}, places_path)
        elif action in ("drop", "at") and field("start") and field("end") and (action == "drop" or name):
            # A span of readings that was wrong, or was really at a named place (hearsay/places.py).
            append_tag({"type": action, "start": float(field("start")), "end": float(field("end"))}
                       | ({"name": name} if action == "at" else {}), places_path)
        elif action == "move" and field("from"):
            # Its spots, where the phone's readings put them, give way to the points given:
            # one pasted here, or from the app's map each of the place's spots, one dragged
            # or resized. Each at may have a radius, in order; one without keeps the place's.
            points = [coordinates(at) for at in form.get("at", [])]
            current = max((sp[3] for sp in read_places(labels_dir) if sp[0] == field("from")), default=RADIUS)
            radii = form.get("radius", [])
            if points and None not in points:
                append_tag({"type": "forget", "name": field("from")}, places_path)
                for i, (lat, lon) in enumerate(points):
                    r = radius(radii[i], current) if i < len(radii) else current
                    append_tag(place(field("from"), lat, lon, r), places_path)
        return RedirectResponse("/places", status_code=303)

    def proposed_words() -> tuple[dict[str, list[str]], list[str], str | None]:
        """What Update would commit (the draft, or the typed words if there is
        none, with Lattice's terms as they are now), the draft's typed words,
        and why Lattice's terms couldn't be read, if they couldn't."""
        committed = read_committed(labels_dir)
        draft = read_draft(labels_dir)
        typed = committed["typed"] if draft is None else tidy(draft)
        lattice, error = read_lattice(vocab_path)
        return {"typed": typed, "lattice": lattice}, typed, error

    def words_seen(proposed: dict[str, list[str]]) -> str:
        return hashlib.sha256(json.dumps(proposed, sort_keys=True).encode()).hexdigest()

    @app.get("/words")
    async def show_words(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        committed = read_committed(labels_dir)
        proposed, typed, error = proposed_words()
        before, after = set(boosted(committed)), set(boosted(proposed))

        def source(word: str, lists: dict[str, list[str]]) -> str:
            return "Lattice" if word in lists["lattice"] and word not in lists["typed"] else "typed"

        changes = "".join(
            f"<li class='added'>+ {html.escape(w)} <span class='muted'>{source(w, proposed)}</span></li>" if w in after else
            f"<li><span class='removed'>− {html.escape(w)}</span> <span class='muted'>{source(w, committed)}</span></li>"
            for w in tidy(list(before ^ after)))
        spellings = {}
        for w in after:
            spellings.setdefault(w.casefold(), []).append(w)
        clashes = [sorted(ws) for ws in spellings.values() if len(ws) > 1]
        db = connect()
        try:
            [(conversations,)] = db.execute("SELECT count(*) FROM conversation_audio WHERE wav_file IS NOT NULL").fetchall()
        finally:
            db.close()

        notice = ("<p class='error'>Lattice's words changed since that page was shown. Check the changes again.</p>"
                  if request.query_params.get("changed") else "")
        if error:
            notice += f"<p class='error'>{html.escape(error)}. Update waits until Lattice's words can be read.</p>"
        if clashes:
            notice += ("<p class='error'>Spelled more than one way: "
                       f"{html.escape('; '.join(', '.join(ws) for ws in clashes))}. Parakeet leans toward each spelling.</p>")
        update = ("" if error or not changes else
                  f"<form method='post' action='/words'><input type='hidden' name='seen' value='{words_seen(proposed)}'>"
                  f"<button type='submit' name='action' value='update'>Update: re-transcribes all {conversations}"
                  " conversation(s)</button></form>")
        box = html.escape("\n".join(typed))
        discard = ("<form method='post' action='/words'><button type='submit' name='action' value='discard'>"
                   "Discard draft</button></form>" if read_draft(labels_dir) is not None else "")
        return page("Hearsay words", f"""
<p><a href="/">← Hearsay</a></p>
<h1>Words</h1>
<p class="muted">Names and terms transcription leans toward, one per line, spelled and capitalized as they
should be written. Lattice's approved words are added on Update; remove those in Lattice. Edits wait here
as a draft until you press Update.</p>
{notice}
<form method="post" action="/words">
  <textarea name="words" autocapitalize="none" spellcheck="false">{box}</textarea>
  <button type="submit" name="action" value="save">Save draft</button>
</form>
<h2>Changes</h2><ul>{changes or "<li class='muted'>None: transcription already uses these words.</li>"}</ul>
{update}{discard}
<p class="muted">{len(after)} word(s), {len(proposed["lattice"])} from Lattice.</p>""")

    @app.post("/words")
    async def edit_words(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        action = form.get("action", [""])[0]
        if action == "save":
            save_draft(labels_dir, tidy(form.get("words", [""])[0].splitlines()))
        elif action == "discard":
            (labels_dir / "words.draft.txt").unlink(missing_ok=True)
        elif action == "update":
            proposed, _, error = proposed_words()
            if error:
                return RedirectResponse("/words", status_code=303)
            # Commit what the page showed, or nothing: Lattice may have
            # changed its words, or another tab the draft, since.
            if form.get("seen", [""])[0] != words_seen(proposed):
                return RedirectResponse("/words?changed=1", status_code=303)
            commit(labels_dir, proposed["typed"], proposed["lattice"])
        return RedirectResponse("/words", status_code=303)

    @app.get("/audio/{turn_id}")
    async def audio(request: Request, turn_id: str) -> Response:
        if not logged_in(request):
            return Response(status_code=401)
        db = connect()
        try:
            row = db.execute(
                "SELECT ca.wav_file, t.start, t.end FROM turns t"
                " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
                " WHERE t.turn_id = ?",
                (turn_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None or row[0] is None:
            return Response(status_code=404)
        return ranged(wav_slice(audio_dir / row[0], row[1], row[2]), request.headers.get("range"))

    return app


def app_from_env() -> FastAPI:
    for key in ("HEARSAY_PORTAL_USER", "HEARSAY_PORTAL_PASSWORD", "HEARSAY_SESSION_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"{key} is empty")
    return create_app(
        Path(os.environ["HEARSAY_DB"]),
        Path(os.environ["HEARSAY_AUDIO_DIR"]),
        Path(os.environ["HEARSAY_LABELS_DIR"]),
        os.environ["HEARSAY_PORTAL_USER"],
        os.environ["HEARSAY_PORTAL_PASSWORD"],
        os.environ["HEARSAY_SESSION_KEY"],
        # From capture.env (install/compose.yaml); without it, app login is off.
        os.environ.get("HEARSAY_CAPTURE_TOKEN", ""),
        # Idea Machine's vocab.json, mounted read-only (install/compose.yaml).
        Path(os.environ["HEARSAY_VOCAB_FILE"]) if os.environ.get("HEARSAY_VOCAB_FILE") else None,
    )

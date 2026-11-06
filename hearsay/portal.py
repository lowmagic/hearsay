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
the turns it couldn't decide, and those that sound like the media the operator
has tagged (hearsay/media.py), and the operator tags each: Me, Other or Mic
(someone present), or not speech (Media, Music, Noise), or deletes it. Tagging
a turn collapses it to an Undo button rather than reloading the page; undoing
appends a record that takes the tag back. A tag is written to labels/tags.jsonl, the same durable
input reprocess reads, with the turn's time span so it
survives re-transcription, and applies to that turn whatever its voice said
(hearsay.turns.categories). Each turn's time links to it in its
conversation's transcript, to hear it in context. Delete throws the turn out, as a conversation's
page does (below): Noise keeps its words in Hearsay, Delete doesn't.

Times are the NAS's own time zone, the operator's.

The Conversations page lists every conversation, newest first, by day,
narrowed to
a named place or to those whose words contain a search, and opens each as
its transcript in order: every turn with when, where and who the stream
says said it (or why it's left out), and the conversation's audio, which
plays from any turn on. Each says whether it's in the stream yet, read from
the stream's index, or held, and until when (hearsay/hold.py); the operator
can release an ended one early (labels/release.jsonl). A day's page reads
its conversations through in order, with the quiet between them, each just
as on its own page. It reads what the last reprocess wrote; the
transcript is the first part of roadmap slice 20.

On a conversation's page the operator throws out the turns checked, each
run of them one mark, or the whole conversation, with an optional reason
that Idea Machine will see. All not me and All media check the turns Hearsay
is sure are either, and leave those it isn't marked yellow (pick); Check
between fills in a stretch. The mark is written to labels/forget.jsonl
(hearsay/forget.py) and can be taken back until a reprocess applies it.

The Words page edits the words transcription is nudged toward
(hearsay/words.py): a draft kept until the operator presses Update, which
commits it with Lattice's approved terms all at once.

Each edit a reprocess applies (a tag, a mark thrown out or taken back, a
release, a place) enqueues one on corral once the response is sent
(hearsay/enqueue.py APPLY): it runs once the edits have been quiet 10
minutes, so a session of tagging is one run, and Apply now starts it as soon
as the NAS is free. If corral can't be reached, the nightly pipeline applies
them instead. A Words Update enqueues the whole pipeline (hearsay/enqueue.py
CHAIN): it changes transcription, which a reprocess alone doesn't redo.
"""

import asyncio
import bisect
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
import urllib.error
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from hearsay import enqueue
from hearsay.forget import read_marks
from hearsay.hold import HOLD, held, read_releases
from hearsay.media import HINT
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
from hearsay.speakers import NOT_OWNER_THRESHOLD
from hearsay.stream import letters, published, speaker_kind
from hearsay.turns import categories, read_operator_files, resolve, wall
from hearsay.words import boosted, commit, read_committed, read_draft, read_lattice, save_draft, tidy

log = logging.getLogger("hearsay.portal")

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
# Turns shown under a conversation in the list when a search matches them.
MATCHES_SHOWN = 3
# A media score from here to the hint is too unsure for All media to check.
MEDIA_UNSURE = 0.5
# Why a stretch was thrown out: a few words, which reach Idea Machine.
REASON_CHARACTERS = 200
# How long edits wait for another before the reprocess that applies them starts.
APPLY_MINUTES = enqueue.APPLY["quiet_seconds"] // 60

UNSURE_SQL = """
SELECT t.turn_id, t.conversation_id, t.idx, t.start, t.end, t.text, ca.zero_at
FROM turns t
JOIN turn_speakers ts ON ts.turn_id = t.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE ts.label IS NULL AND ca.wav_file IS NOT NULL AND t.end - t.start >= ?
ORDER BY ca.zero_at DESC, t.start DESC
"""

# Turns that sound like tagged media, unless their own voice or channel said the owner's.
MEDIA_SQL = """
SELECT t.turn_id, t.conversation_id, t.idx, t.start, t.end, t.text, ca.zero_at
FROM turn_media tm
JOIN turns t ON t.turn_id = tm.turn_id
JOIN turn_speakers ts ON ts.turn_id = t.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
WHERE tm.score >= ? AND ca.wav_file IS NOT NULL AND NOT (ts.label IS 'owner' AND ts.basis IN ('voice', 'channel'))
ORDER BY ca.zero_at DESC, t.start DESC
"""

STYLE = """
:root { color-scheme: light dark; --fg: #111; --bg: #fff; --muted: #666; --line: #ddd; --accent: #2458d6; --unsure: #e0b000; --unsure-text: #8a6d00; }
@media (prefers-color-scheme: dark) { :root { --fg: #eee; --bg: #121212; --muted: #999; --line: #333; --accent: #7fa4ff; --unsure: #d4a800; --unsure-text: #e8c547; } }
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
.clip button.delete { color: #c33; }
.hinted { font-weight: 600; outline: 2px solid var(--accent); }
textarea { font: inherit; width: 100%; box-sizing: border-box; padding: 0.6rem; min-height: 12rem; }
.added { color: #2a8a3a; }
.removed { color: #c33; text-decoration: line-through; }
.tagged, .apply { display: flex; align-items: center; gap: 0.5rem; }
.tagged span, .apply span { flex: 1; }
.tagged button, .apply button { width: auto; margin: 0; padding: 0.4rem 1rem; }
.filter { display: flex; gap: 0.5rem; }
.filter select, .filter input { font: inherit; padding: 0.4rem; }
.filter input { flex: 1; min-width: 0; }
.filter button { width: auto; margin: 0; padding: 0.4rem 1rem; }
.player { position: sticky; top: 0; background: var(--bg); padding: 0.3rem 0; }
.turn { padding: 0.4rem 0; scroll-margin-top: 4rem; }
.turn a.at, a.inline { display: inline; }
.turn.left { color: var(--muted); }
.turn.now, .turn:target { background: color-mix(in srgb, var(--accent) 15%, transparent); }
.place { font-weight: 600; border-bottom: none; padding-top: 1rem; }
.turn input[type=checkbox] { width: auto; margin: 0 0.5rem 0 0; transform: scale(1.3); }
.place button { width: auto; margin: 0 0 0 0.5rem; padding: 0.2rem 0.8rem; font-size: 0.85em; }
.forget { position: sticky; bottom: 0; background: var(--bg); border-top: 1px solid var(--line); padding: 0.5rem 0; }
.forget input { padding: 0.4rem; }
.forget .row button { font-size: 0.85em; padding: 0.5rem; }
.forget .picks button { margin-top: 0; padding: 0.3rem; }
.picks[hidden] { display: none; }
.turn.unsure { border-left: 4px solid var(--unsure); padding-left: 0.4rem; }
.unsure-why { color: var(--unsure-text); font-size: 0.85em; }
mark { background: color-mix(in srgb, var(--accent) 35%, transparent); color: inherit; }
.release button { margin-top: 0; }
section { margin-bottom: 2rem; }
section h2 { margin-bottom: 0.2rem; }
.quiet { color: var(--muted); text-align: center; border-top: 1px dashed var(--line); padding-top: 0.5rem; }
.nav { margin: 0.5rem 0; }
"""

# Tags a clip without leaving the page, and collapses it to an Undo button that
# restores it. Without scripts the forms post as usual and the page reloads.
TAG_SCRIPT = """
const headers = {'X-Requested-With': 'fetch'};
async function post(path, fields) {
  const response = await fetch(path, {method: 'POST', headers, body: new URLSearchParams(fields)});
  if (!response.ok) location.reload();  // logged out, or the turn is gone
  return response.ok;
}
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.matches('form.clip') || !event.submitter) return;
  event.preventDefault();
  const item = form.closest('li'), turn_id = form.elements.turn_id.value, saved = [...item.childNodes];
  const path = event.submitter.getAttribute('formaction') || '/turn';
  if (!await post(path, {turn_id, category: event.submitter.value})) return;
  const row = document.createElement('div'), said = document.createElement('span'), undo = document.createElement('button');
  row.className = 'tagged';
  said.className = 'muted';
  said.textContent = path === '/turn' ? 'Tagged ' + event.submitter.textContent : 'Deleted at the next reprocess';
  undo.type = 'button';
  undo.textContent = 'Undo';
  undo.onclick = async () => { if (await post(path + '/undo', {turn_id})) item.replaceChildren(...saved); };
  row.append(said, undo);
  item.replaceChildren(row);
});
"""


# Plays the conversation from a turn's time, and marks the turn being heard.
# Without scripts a turn's time links to its own clip.
TRANSCRIPT_HELP = ("Tap a time to play from there. To throw out turns, check them: All not me and All media check"
                   " those Hearsay is sure of, never the ones marked yellow, and Check between fills in a stretch"
                   " from the first checked to the last.")

# Each conversation has its own player, and one playing stops the others.
TRANSCRIPT_SCRIPT = """
document.addEventListener('click', event => {
  const link = event.target.closest('a.at');
  const player = link?.closest('section').querySelector('audio');
  if (!player) return;
  event.preventDefault();
  const seconds = parseFloat(link.closest('li').dataset.start);
  if (player.readyState) player.currentTime = seconds;
  else player.addEventListener('loadedmetadata', () => { player.currentTime = seconds; }, {once: true});
  player.play();
});
// The quick picks check turns, adding to those already checked.
document.querySelectorAll('.picks').forEach(row => { row.hidden = false; });
document.addEventListener('click', event => {
  const button = event.target.closest('.picks button');
  if (!button) return;
  const boxes = [...button.closest('section').querySelectorAll('li.turn input[type=checkbox]')];
  if (button.value === 'none') boxes.forEach(box => { box.checked = false; });
  else if (button.value === 'between') {
    const checked = boxes.map(box => box.checked), first = checked.indexOf(true), last = checked.lastIndexOf(true);
    boxes.forEach((box, i) => { if (first >= 0 && i >= first && i <= last) box.checked = true; });
  } else boxes.forEach(box => { if (box.closest('li').dataset.pick === button.value) box.checked = true; });
});
document.addEventListener('submit', event => {
  const ask = {whole: 'Throw out this whole conversation?',
               release: 'Release this conversation? Idea Machine gets it at the next reprocess.'}[event.submitter?.value];
  if (ask && !confirm(ask)) event.preventDefault();
});
document.querySelectorAll('section audio').forEach(player => {
  const turns = [...player.closest('section').querySelectorAll('li[data-start]')];
  player.addEventListener('play', () => document.querySelectorAll('audio').forEach(a => { if (a !== player) a.pause(); }));
  player.addEventListener('timeupdate', () => {
    let now = null;
    for (const turn of turns) { if (parseFloat(turn.dataset.start) <= player.currentTime) now = turn; else break; }
    turns.forEach(turn => turn.classList.toggle('now', turn === now));
  });
});
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


async def read_form(request: Request, keep_blank_values: bool = False) -> dict[str, list[str]]:
    """A posted form's fields. Bytes that aren't UTF-8 are replaced, as parse_qs
    does for escaped ones: bad input, not an error."""
    return parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=keep_blank_values)


# One tap each. hearsay/stream.py keeps _me (the owner), _stranger (someone
# else present) and _mic (someone present, amplified), and leaves the rest out:
# not speech from anyone there. _mic is kept apart from _stranger because a PA
# sounds like a speaker, as TV does: hearsay/media.py learns from neither.
CATEGORIES = {"_me": "Me", "_stranger": "Other", "_mic": "Mic", "_media": "Media", "_music": "Music", "_noise": "Noise"}
# A row of people present, then one of what isn't speech, which ends with Delete.
CATEGORY_ROWS = (("_me", "_stranger", "_mic"), ("_media", "_music", "_noise"))


def clip_player(s: dict) -> str:
    """A turn's audio with its words, when and where it was said, and a button per category.
    When links to the turn in its conversation, to hear it in context."""
    words = s["text"] if len(s["text"]) <= CLIP_CHARACTERS else s["text"][:CLIP_CHARACTERS].rsplit(" ", 1)[0] + " …"
    conversation = quote(s["conversation_id"])
    when = (f"<a class='inline' href='/conversations/{conversation}#{conversation}-{s['idx']}'>"
            f"{local(s['at']):%b %-d %H:%M}</a>")
    said = [f"{s['end'] - s['start']:.1f} s", when] + ([html.escape(s["place"])] if s["place"] else [])
    people, other = ("".join(f"<button type='submit' name='category' value='{value}'>{CATEGORIES[value]}</button>"
                             for value in row) for row in CATEGORY_ROWS)
    other += "<button type='submit' class='delete' formaction='/turn/delete'>Delete</button>"
    return (f"<audio controls preload='none' src='/audio/{quote(s['turn_id'])}'></audio>"
            f"<div class='words'>{html.escape(words)}</div>"
            f"<div class='muted'>{' · '.join(said)}</div>"
            f"<form class='clip' method='post' action='/turn'>"
            f"<input type='hidden' name='turn_id' value='{html.escape(s['turn_id'])}'>"
            f"<div class='row'>{people}</div><div class='row'>{other}</div></form>")


def marked(text: str, query: str) -> str:
    """The text escaped, with each match of the search marked."""
    if not query:
        return html.escape(text)
    parts = re.split(f"({re.escape(query)})", text, flags=re.IGNORECASE)
    return "".join(f"<mark>{html.escape(p)}</mark>" if i % 2 else html.escape(p) for i, p in enumerate(parts))


def conversation_places(db: sqlite3.Connection) -> dict[str, list[tuple[str, float, float]]]:
    """Each conversation's named places, in order: (place, start, end), unix seconds."""
    places = {}
    for conversation_id, place, start, end in db.execute(
            "SELECT conversation_id, place, start, end FROM conversation_places ORDER BY start"):
        places.setdefault(conversation_id, []).append((place, start, end))
    return places


def local(at: float) -> datetime:
    """A moment in the NAS's own time zone, the operator's (install/compose.yaml)."""
    return datetime.fromtimestamp(at).astimezone()


def quiet(seconds: float) -> str:
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{f'{hours} h ' if hours else ''}{minutes} min quiet"


def whole_seconds(start: float, end: float) -> list[str]:
    """A conversation's span widened to whole seconds, so that a record of it,
    read back from text, still covers the conversation as stored."""
    return [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in (math.floor(start), math.ceil(end))]


def place_at(places: list[tuple[str, float, float]], at: float) -> str | None:
    """The named place a moment was at, from a conversation's places in order.
    A place lasts until the next one starts (hearsay/places.py)."""
    return next((place for place, start, end in reversed(places) if start <= at <= end), None)


def who(label: str | None, category: str | None, basis: str | None, voice: str | None) -> str:
    """Who the stream says said a turn, or why it's left out of it. Voices
    other than the owner's are lettered as diarization told them apart."""
    found = speaker_kind(label, category, basis)
    if found is None:
        return f"Left out · {category.removeprefix('_')}"
    if found[0] == "owner":
        return "Me"
    if found[0] == "unknown":
        return "Not sure"
    return f"Not me · {voice}" if voice else "Not me"


def pick(label: str | None, category: str | None, basis: str | None, similarity: float | None,
         media: float | None) -> tuple[str | None, str | None]:
    """Which of a conversation page's quick picks checks a turn (notme or
    media), or why neither does: a turn either could be wrong about is left
    unchecked and marked, since throwing out someone talking is worse than
    keeping some TV. The operator's tags are sure. Unsure: Hearsay couldn't
    tell who spoke; it's not the owner only by its diarized speaker and its
    own voice is no further from the owner's than an unsure one; or it
    sounds somewhat like tagged media, under the hint that takes it out."""
    if category == "_media":
        return "media", None
    if category is None:
        owner_by_ear = label == "owner" and basis in ("voice", "channel")
        if media is not None and MEDIA_UNSURE <= media < HINT and not owner_by_ear:
            return None, "Might be media"
        if label is None:
            return None, "Not sure who spoke"
        if label == "not_owner" and basis == "diarization" and similarity is not None and similarity > NOT_OWNER_THRESHOLD:
            return None, "Not me by its speaker, but its voice is close to yours"
    found = speaker_kind(label, category, basis)
    return ("notme" if found and found[0] not in ("owner", "unknown") else None), None


def coordinates(text: str) -> tuple[float, float] | None:
    """Latitude and longitude from pasted text: "-26.12, 80.14", or an Apple or
    Google Maps link that has them in it (ll=, q=, @). Not an address: looking
    one up would send it to a geocoding service."""
    found = re.search(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", unquote(text))
    if not found:
        return None
    lat, lon = float(found[1]), float(found[2])
    return (lat, lon) if on_earth(lat, lon) else None


def on_earth(lat: float, lon: float) -> bool:
    # False for NaN too, which compares false with everything.
    return abs(lat) <= 90 and abs(lon) <= 180


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


def create_app(db_path: Path, audio_dir: Path, labels_dir: Path, stream_dir: Path, user: str, password: str,
               session_key: str, app_token: str = "", vocab_path: Path | None = None, corral_url: str = "",
               corral_token: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    tags_path = labels_dir / "tags.jsonl"

    def sign(expires: int) -> str:
        return hmac.new(session_key.encode(), f"{user}:{expires}".encode(), hashlib.sha256).hexdigest()

    def logged_in(request: Request) -> bool:
        # The iPhone app's own screens (GET /places.json) send the capture token on each request.
        sent = request.headers.get("authorization", "")
        if app_token and sent.startswith("Bearer "):
            return hmac.compare_digest(sent.encode(), f"Bearer {app_token}".encode())
        expires, _, signature = request.cookies.get(COOKIE, "").partition(".")
        # Bytes: a cookie can hold any character, and one that isn't ASCII is just not a session.
        if not (expires.isascii() and expires.isdigit()) or int(expires) < time.time():
            return False
        return hmac.compare_digest(signature.encode(), sign(int(expires)).encode())

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

    def load_turns(sql: str, *params) -> list[dict]:
        """The query's turns the operator hasn't tagged, latest first, with when each was
        said (unix seconds) and the named place it was said at."""
        db = connect()
        db.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in db.execute(sql, params)]
            places = conversation_places(db)
        finally:
            db.close()
        done, marks = tagged(), read_marks(labels_dir)
        rows = [row for row in rows if row["turn_id"] not in done]
        for row in rows:
            row["at"] = datetime.fromisoformat(row["zero_at"]).timestamp() + row["start"]
            row["place"] = place_at(places.get(row["conversation_id"], []), row["at"])
        return [row for row in rows if not any(
            m["start"] < row["at"] + row["end"] - row["start"] and row["at"] < m["end"] for m in marks)]

    def hold_status(start: float, end: float, is_open: bool, sent: set[str], conversation_id: str) -> tuple[str, bool]:
        """Whether Idea Machine can see a conversation, in words, and whether it can be released now."""
        if conversation_id in sent:
            return "in the stream", False
        releases = read_releases(labels_dir)
        if not held(start, end, releases, time.time()):
            return ("released" if end + HOLD > time.time() else "two weeks up") + ": out at the next reprocess", False
        return f"held until {local(end + HOLD):%a %b %-d}", not is_open

    def append_tag(record: dict, path: Path = tags_path) -> None:
        record["tagged_at"] = datetime.now(timezone.utc).isoformat()
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def apply_later(background: BackgroundTasks) -> None:
        """Once the response is sent, enqueue the reprocess that applies the edit just written."""
        if corral_url:
            background.add_task(enqueue_on_corral, enqueue.APPLY)

    def enqueue_on_corral(spec: dict) -> None:
        try:
            enqueue.post(corral_url, corral_token, spec)
        except OSError as e:  # urllib's errors, refused connections and timeouts included
            log.warning("couldn't enqueue on corral (%s); the nightly pipeline will apply this edit", e)

    def apply_form(back: str) -> str:
        """Apply now, on the pages edits are made on. Without corral (local dev) there's nothing to ask."""
        if not corral_url:
            return ""
        return (f"<form class='apply' method='post' action='/apply'><input type='hidden' name='back' value='{html.escape(back)}'>"
                f"<span class='muted'>Edits apply in a reprocess {APPLY_MINUTES} min after the last one.</span>"
                "<button type='submit'>Apply now</button></form>")

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
        form = await read_form(request)
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
        # The whole header, so a token sent without its scheme isn't one.
        sent = request.headers.get("authorization", "")
        if not app_token or not hmac.compare_digest(sent.encode(), f"Bearer {app_token}".encode()):
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
        media = load_turns(MEDIA_SQL, HINT)
        offered = {s["turn_id"] for s in media}
        unsure = [s for s in load_turns(UNSURE_SQL, MIN_UNSURE_SECONDS) if s["turn_id"] not in offered]
        players = "".join(f"<li>{clip_player(s)}</li>" for s in unsure[:UNSURE])
        media_players = "".join(f"<li>{clip_player(s)}</li>" for s in media[:UNSURE])
        media_section = f"""
<h1>Sounds like TV</h1>
<p class="muted">{len(media)} turn(s) that sound like what you've tagged Media, latest first.</p>
<ul>{media_players}</ul>""" if media else ""
        return page("Hearsay", f"""
<p class="nav"><a href="/day">Today</a> · <a href="/conversations">Conversations</a> · <a href="/places">Places</a> ·
<a href="/words">Words</a></p>{apply_form("/")}{media_section}
<h1>Not sure who spoke</h1>
<p class="muted">{len(unsure)} turn(s) Hearsay couldn't tell were you or not, latest first.
Tag each; it applies at the next reprocess.</p>
<ul>{players or "<li class='muted'>Nothing to check.</li>"}</ul>
<h2>Transcription</h2><ul><li><a href="/words">Words <span class="muted">· {len(boosted(read_committed(labels_dir)))} in use
{"· a draft is waiting" if read_draft(labels_dir) is not None else ""}</span></a></li></ul>
<form method="post" action="/logout"><button type="submit">Log out</button></form>
<script>{TAG_SCRIPT}</script>""")

    def find_turn(turn_id: str) -> tuple | None:
        db = connect()
        try:
            return db.execute(
                "SELECT ca.zero_at, t.start, t.end FROM turns t"
                " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id WHERE t.turn_id = ?",
                (turn_id,)).fetchone()
        finally:
            db.close()

    def applied(at: list[str]) -> bool:
        """Whether a reprocess has applied the mark with this span: then it's final."""
        start, end = (datetime.fromisoformat(t).timestamp() for t in at)
        db = connect()
        try:
            return db.execute("SELECT 1 FROM forgotten WHERE start = ? AND end = ?", (start, end)).fetchone() is not None
        finally:
            db.close()

    async def tag(request: Request, background: BackgroundTasks, undo: bool, delete: bool = False) -> Response:
        """Tag a turn, or take its tag back: a clip record with no name (hearsay.turns.resolve).
        Or delete it: throw it out (hearsay/forget.py), which can be taken back
        until a reprocess applies it. The page's script gets 204 or 401 instead
        of a page to follow."""
        script = request.headers.get("x-requested-with") == "fetch"
        if not logged_in(request):
            return Response(status_code=401) if script else RedirectResponse("/login", status_code=303)
        form = await read_form(request)
        turn_id = form.get("turn_id", [""])[0]
        category = "" if undo else form.get("category", [""])[0]
        turn = find_turn(turn_id)
        if not turn or not (undo or delete or category in CATEGORIES):
            return Response(status_code=404) if script else RedirectResponse("/", status_code=303)
        # Absolute times, so the tag survives new transcripts and boundaries;
        # the turn id only for reference.
        if not delete:
            append_tag({"type": "clip", "at": [wall(*turn)], "turn_ids": [turn_id], "name": category})
            apply_later(background)
        elif not (undo and applied(wall(*turn))):
            append_tag({"type": "undo" if undo else "forget", "at": wall(*turn), "turn_ids": [turn_id]},
                       labels_dir / "forget.jsonl")
            apply_later(background)
        return Response(status_code=204) if script else RedirectResponse("/", status_code=303)

    @app.post("/turn")
    async def tag_turn(request: Request, background: BackgroundTasks) -> Response:
        return await tag(request, background, undo=False)

    @app.post("/turn/undo")
    async def untag_turn(request: Request, background: BackgroundTasks) -> Response:
        return await tag(request, background, undo=True)

    @app.post("/turn/delete")
    async def delete_turn(request: Request, background: BackgroundTasks) -> Response:
        return await tag(request, background, undo=False, delete=True)

    @app.post("/turn/delete/undo")
    async def undelete_turn(request: Request, background: BackgroundTasks) -> Response:
        return await tag(request, background, undo=True, delete=True)

    @app.get("/conversations")
    async def list_conversations(request: Request) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        place = request.query_params.get("place", "")
        query = " ".join(request.query_params.get("q", "").split())
        db = connect()
        try:
            conversations = db.execute(
                "SELECT conversation_id, start, end, open FROM conversations ORDER BY start DESC").fetchall()
            places = conversation_places(db)
            thrown_out = {c for (c,) in db.execute("SELECT conversation_id FROM forgotten")}
            turns = {}
            for conversation_id, idx, text in db.execute(
                    "SELECT conversation_id, idx, text FROM turns ORDER BY conversation_id, start, idx"):
                turns.setdefault(conversation_id, []).append((idx, text))
        finally:
            db.close()

        items, sent, last_day = [], published(stream_dir), None
        for conversation_id, start, end, is_open in conversations:
            here = places.get(conversation_id, [])
            if place and place not in {name for name, _, _ in here}:
                continue
            said = turns.get(conversation_id, [])
            matches = [(idx, text) for idx, text in said if query.casefold() in text.casefold()] if query else []
            if query and not matches:
                continue
            url = f"/conversations/{quote(conversation_id)}" + (f"?q={quote(query)}" if query else "")
            names = list(dict.fromkeys(name for name, _, _ in here))
            facts = [f"{(end - start) / 60:.0f} min", *names, f"{len(said)} turn(s)"] + (["still going"] if is_open else [])
            facts += ["some thrown out"] if conversation_id in thrown_out else []
            facts.append(hold_status(start, end, is_open, sent, conversation_id)[0])
            shown = "".join(f"<li><a href='{url}#{quote(conversation_id)}-{idx}'>{marked(text, query)}</a></li>"
                            for idx, text in matches[:MATCHES_SHOWN])
            more = f"<div class='muted'>and {len(matches) - MATCHES_SHOWN} more</div>" if len(matches) > MATCHES_SHOWN else ""
            if local(start).date() != last_day:
                last_day = local(start).date()
                items.append(f"<li class='place'><a href='/day/{last_day}'>{last_day:%a %b %-d}</a></li>")
            items.append(f"<li><a href='{url}'><strong>{local(start):%H:%M}"
                         f"</strong> <span class='muted'>{html.escape(' · '.join(facts))}</span></a>"
                         f"{f'<ul>{shown}</ul>{more}' if shown else ''}</li>")
        options = "".join(f"<option{' selected' if name == place else ''}>{html.escape(name)}</option>"
                          for name in sorted({n for here in places.values() for n, _, _ in here}))
        return page("Hearsay conversations", f"""
<p><a href="/">← Hearsay</a></p>
<h1>Conversations</h1>
<form class="filter" method="get" action="/conversations">
  <select name="place" aria-label="Place"><option value="">Anywhere</option>{options}</select>
  <input name="q" type="search" value="{html.escape(query)}" placeholder="Words said" aria-label="Words said">
  <button type="submit">Find</button>
</form>
<p class="muted">{sum(1 for i in items if not i.startswith("<li class='place'>"))} of {len(conversations)}
conversation(s), newest first, as of the last reprocess. Tap a day to read it through.</p>
<ul>{"".join(items) or "<li class='muted'>None.</li>"}</ul>""")

    def transcript(db: sqlite3.Connection, conversation_id: str, query: str, back: str, tagged_as: dict[str, str],
                   places: dict[str, list[tuple[str, float, float]]], marks: list[dict], sent: set[str]) -> str | None:
        """One conversation as a section: when, how long and where, whether Idea Machine
        can see it, its audio, and its turns in order, each to play from or throw out."""
        found = db.execute(
            "SELECT c.start, c.end, c.open, ca.zero_at, ca.wav_file FROM conversations c"
            " JOIN conversation_audio ca USING (conversation_id) WHERE conversation_id = ?",
            (conversation_id,)).fetchone()
        if found is None:
            return None
        start, end, is_open, zero_at, wav_file = found
        zero = datetime.fromisoformat(zero_at).timestamp()
        here = places.get(conversation_id, [])
        turns = db.execute(
            "SELECT t.turn_id, t.idx, t.start, t.end, t.text, t.diar_speaker, ts.label, ts.basis, ts.owner_similarity,"
            " tm.score FROM turns t JOIN turn_speakers ts USING (turn_id) LEFT JOIN turn_media tm USING (turn_id)"
            " WHERE t.conversation_id = ? ORDER BY t.start, t.idx",
            (conversation_id,)).fetchall()
        applied = db.execute("SELECT start, end, reason FROM forgotten WHERE conversation_id = ?",
                             (conversation_id,)).fetchall()

        def clock(at: float) -> str:
            return f"{local(at):%H:%M:%S}"

        # Stretches thrown out: those a reprocess has applied are final, their
        # turns already gone; the rest can still be taken back.
        done = {(s, e) for s, e, _ in applied}
        pending = [m for m in marks if m["start"] < end and m["end"] > start and (m["start"], m["end"]) not in done]
        notes = sorted(
            [(s, f"<li class='place muted'>Thrown out {clock(s)}–{clock(e)}"
                 f"{html.escape(f' · {reason}') if reason else ''}</li>") for s, e, reason in applied]
            + [(m["start"], f"<li class='place'>To throw out {clock(m['start'])}–{clock(m['end'])}"
                            f"{html.escape(' · ' + m['reason']) if m['reason'] else ''} at the next reprocess"
                            f"<button type='submit' formaction='/forget/undo' name='at'"
                            f" value='{html.escape('|'.join(m['at']))}'>Undo</button></li>") for m in pending])
        voices = {}  # diarized speaker -> A, B, ... in order of first turn
        lines, last_place = [], None
        for turn_id, idx, turn_start, turn_end, text, voice, label, basis, similarity, media in turns:
            at = zero + turn_start
            while notes and notes[0][0] <= at:
                lines.append(notes.pop(0)[1])
            place = place_at(here, at)
            if place and place != last_place:
                lines.append(f"<li class='place'>At {html.escape(place)}</li>")
                last_place = place
            if voice is not None and voice not in voices:
                voices[voice] = letters(len(voices))
            speaker = who(label, tagged_as.get(turn_id), basis, voices.get(voice))
            going = any(m["start"] < zero + turn_end and at < m["end"] for m in pending)
            picked, unsure = pick(label, tagged_as.get(turn_id), basis, similarity, media)
            kind = " left" if going or speaker.startswith("Left out") else ""
            kind += " unsure" if unsure else ""
            lines.append(
                f"<li id='{html.escape(conversation_id)}-{idx}' class='turn{kind}' data-start='{turn_start:.2f}'"
                + (f" data-pick='{picked}'" if picked and not going else "")
                + f"><input type='checkbox' name='turn_id' value='{html.escape(turn_id)}'"
                f" aria-label='Choose'><a class='at' href='/audio/{quote(turn_id)}'>{clock(at)}</a> "
                f"<span class='muted'>{html.escape(speaker)}</span>"
                + (f" <span class='unsure-why'>{html.escape(unsure)}</span>" if unsure else "")
                + f"<div>{marked(text, query)}</div></li>")
        lines += [note for _, note in notes]
        player = (f"<div class='player'><audio controls preload='none'"
                  f" src='/conversations/{quote(conversation_id)}/audio'></audio></div>" if wav_file else
                  "<p class='muted'>No audio.</p>")
        names = list(dict.fromkeys(name for name, _, _ in here))
        facts = [f"{(end - start) / 60:.0f} min", *names, f"{len(turns)} turn(s)"] + (["still going"] if is_open else [])
        status, releasable = hold_status(start, end, is_open, sent, conversation_id)
        hidden = (f"<input type='hidden' name='conversation_id' value='{html.escape(conversation_id)}'>"
                  f"<input type='hidden' name='back' value='{html.escape(back)}'>")
        release = (f"<form class='release' method='post' action='/release'>{hidden}"
                   "<button type='submit' name='action' value='release'>Release to Idea Machine now</button></form>"
                   if releasable else "")
        return f"""<section id="{html.escape(conversation_id)}">
<h2><a href="/conversations/{quote(conversation_id)}">{local(start):%a %b %-d %H:%M}</a></h2>
<p class="muted">{html.escape(' · '.join(facts))}. Idea Machine: {html.escape(status)}.</p>
{release}{player}
<form method="post" action="/forget">{hidden}
<ul>{"".join(lines) or "<li class='muted'>Not transcribed yet.</li>"}</ul>
<div class="forget">
  <input name="reason" maxlength="{REASON_CHARACTERS}" autocomplete="off"
         placeholder="Why (optional; Idea Machine sees it)" aria-label="Why">
  <div class="row picks" hidden><button type="button" value="notme">All not me</button>
  <button type="button" value="media">All media</button>
  <button type="button" value="between">Check between</button>
  <button type="button" value="none">None</button></div>
  <div class="row"><button type="submit" name="action" value="checked">Throw out checked</button>
  <button type="submit" name="action" value="whole">Throw out all</button></div>
</div>
</form>
</section>"""

    @app.get("/conversations/{conversation_id}")
    async def show_conversation(request: Request, conversation_id: str) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        query = " ".join(request.query_params.get("q", "").split())
        db = connect()
        try:
            section = transcript(db, conversation_id, query, f"/conversations/{quote(conversation_id)}", categories(db),
                                 conversation_places(db), read_marks(labels_dir), published(stream_dir))
            [found] = db.execute("SELECT start FROM conversations WHERE conversation_id = ?",
                                 (conversation_id,)).fetchall() or [(None,)]
        finally:
            db.close()
        if section is None:
            return Response(status_code=404)
        return page("Hearsay conversation", f"""
<p><a href="/conversations">← Conversations</a> · <a href="/day/{local(found[0]):%Y-%m-%d}#{quote(conversation_id)}">Its day</a></p>
<p class="muted">{TRANSCRIPT_HELP}</p>
{apply_form(f"/conversations/{quote(conversation_id)}")}
{section}
<script>{TRANSCRIPT_SCRIPT}</script>""")

    @app.get("/day")
    @app.get("/day/{day}")
    async def show_day(request: Request, day: str = "") -> Response:
        """A day's conversations in order, one after another, with the quiet between them."""
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        try:
            date = datetime.fromisoformat(day).date() if day else datetime.now().astimezone().date()
            # Near the first and last days datetime can hold, a day's edges fall outside it.
            first, last = (datetime.combine(d, datetime.min.time()).astimezone().timestamp()
                           for d in (date, date + timedelta(days=1)))
        except (ValueError, OverflowError):
            return Response(status_code=404)
        db = connect()
        try:
            conversations = db.execute(
                "SELECT conversation_id, start, end FROM conversations WHERE start >= ? AND start < ? ORDER BY start",
                (first, last)).fetchall()
            [(before, after)] = db.execute(
                "SELECT (SELECT max(start) FROM conversations WHERE start < ?),"
                " (SELECT min(start) FROM conversations WHERE start >= ?)", (first, last)).fetchall()
            context = (categories(db), conversation_places(db), read_marks(labels_dir), published(stream_dir))
            sections, previous_end = [], None
            for conversation_id, start, end in conversations:
                if previous_end is not None:
                    sections.append(f"<p class='quiet'>{quiet(start - previous_end)}</p>")
                sections.append(transcript(db, conversation_id, "", f"/day/{date}#{quote(conversation_id)}", *context))
                previous_end = end
        finally:
            db.close()
        # The nearest days with anything recorded.
        nav = " · ".join(
            ([f"<a href='/day/{local(before):%Y-%m-%d}'>← {local(before):%a %b %-d}</a>"] if before else [])
            + ["<a href='/conversations'>Conversations</a>"]
            + ([f"<a href='/day/{local(after):%Y-%m-%d}'>{local(after):%a %b %-d} →</a>"] if after else []))
        return page("Hearsay day", f"""
<p><a href="/">← Hearsay</a></p>
<h1>{date:%A %B %-d}</h1>
<p class="nav">{nav}</p>
{apply_form(f"/day/{date}")}
<p class="muted">{len(conversations)} conversation(s), in order. {TRANSCRIPT_HELP}</p>
{"".join(sections) or "<p class='muted'>Nothing recorded.</p>"}
<p class="nav">{nav}</p>
<script>{TRANSCRIPT_SCRIPT}</script>""")

    def go_back(form: dict[str, list[str]]) -> Response:
        """To the page the form was on: a conversation's, its day's, or another."""
        back = form.get("back", [""])[0]
        if not back.startswith("/") or back.startswith("//"):
            conversation_id = form.get("conversation_id", [""])[0]
            back = f"/conversations/{quote(conversation_id)}" if conversation_id else "/"
        return RedirectResponse(back, status_code=303)

    @app.post("/forget")
    async def throw_out(request: Request, background: BackgroundTasks) -> Response:
        """Throw out the turns checked, or the whole conversation: marks by
        absolute time, which a reprocess applies. Turns checked one after
        another make one mark, so a stretch checked through is one mark; the
        turns left unchecked between them are kept."""
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await read_form(request)
        conversation_id = form.get("conversation_id", [""])[0]
        action = form.get("action", [""])[0]
        reason = " ".join(form.get("reason", [""])[0].split())[:REASON_CHARACTERS]
        chosen = set(form.get("turn_id", []))
        db = connect()
        try:
            conversation = db.execute("SELECT start, end FROM conversations WHERE conversation_id = ?",
                                      (conversation_id,)).fetchone()
            turns = db.execute(
                "SELECT t.turn_id, ca.zero_at, t.start, t.end FROM turns t JOIN conversation_audio ca"
                " USING (conversation_id) WHERE t.conversation_id = ? ORDER BY t.start, t.idx",
                (conversation_id,)).fetchall()
        finally:
            db.close()
        if conversation is None:
            return RedirectResponse("/conversations", status_code=303)
        if action == "whole":
            append_tag({"type": "forget", "at": whole_seconds(*conversation), "reason": reason},
                       labels_dir / "forget.jsonl")
            apply_later(background)
        elif action == "checked":
            runs, run = [], []
            for turn_id, zero_at, start, end in turns + [(None, None, None, None)]:
                if turn_id in chosen:
                    zero = datetime.fromisoformat(zero_at).timestamp()
                    run.append((zero + start, zero + end))
                elif run:
                    runs.append(run)
                    run = []
            for run in runs:
                span = (min(s for s, _ in run), max(e for _, e in run))
                append_tag({"type": "forget", "at": [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in span],
                            "reason": reason}, labels_dir / "forget.jsonl")
            if runs:
                apply_later(background)
        return go_back(form)

    @app.post("/release")
    async def release(request: Request, background: BackgroundTasks) -> Response:
        """Let a conversation that has ended reach the stream before its two weeks are up."""
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await read_form(request)
        conversation_id = form.get("conversation_id", [""])[0]
        db = connect()
        try:
            conversation = db.execute("SELECT start, end, open FROM conversations WHERE conversation_id = ?",
                                      (conversation_id,)).fetchone()
        finally:
            db.close()
        if conversation and not conversation[2]:
            append_tag({"type": "release", "at": whole_seconds(*conversation[:2])}, labels_dir / "release.jsonl")
            apply_later(background)
        return go_back(form)

    @app.post("/forget/undo")
    async def take_back(request: Request, background: BackgroundTasks) -> Response:
        """Take a mark back, unless a reprocess has applied it: then it's final."""
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await read_form(request)
        at = form.get("at", [""])[0].split("|")
        try:
            final = applied(at)
        except ValueError:  # not two times, so no mark of the page's: write nothing
            return go_back(form)
        if not final:
            append_tag({"type": "undo", "at": at}, labels_dir / "forget.jsonl")
            apply_later(background)
        return go_back(form)

    def start_now() -> None:
        job_id, _ = enqueue.post(corral_url, corral_token, enqueue.APPLY)
        try:
            enqueue.release(corral_url, corral_token, job_id)
        except urllib.error.HTTPError as e:
            if e.code != 409:  # 409: a worker leased it in between, so it's running already
                raise

    @app.post("/apply")
    async def apply_now(request: Request) -> Response:
        """Start the reprocess that applies the edits so far as soon as the NAS is
        free, rather than once they've been quiet a while. The edits are saved
        either way, so a failure here only says so."""
        script = request.headers.get("x-requested-with") == "fetch"
        if not logged_in(request):
            return Response(status_code=401) if script else RedirectResponse("/login", status_code=303)
        form = await read_form(request)
        if not corral_url:
            problem, status = "corral isn't set up here (CORRAL_URL)", 503
        else:
            try:
                await asyncio.to_thread(start_now)
                return Response(status_code=204) if script else go_back(form)
            except OSError as e:  # urllib's errors, refused connections and timeouts included
                log.warning("couldn't apply now on corral (%s)", e)
                problem, status = f"couldn't reach corral ({e})", 502
        response = page("Hearsay", f"<p class='error'>Not applied: {html.escape(problem)}. Your edits are saved;"
                                   " they apply at the next reprocess.</p><p><a href='/'>← Hearsay</a></p>")
        response.status_code = status
        return response

    @app.get("/conversations/{conversation_id}/audio")
    async def conversation_audio(request: Request, conversation_id: str) -> Response:
        """The whole conversation WAV, in byte ranges as the player asks for them."""
        if not logged_in(request):
            return Response(status_code=401)
        db = connect()
        try:
            row = db.execute("SELECT wav_file FROM conversation_audio WHERE conversation_id = ?",
                             (conversation_id,)).fetchone()
        finally:
            db.close()
        if row is None or row[0] is None:
            return Response(status_code=404)
        return FileResponse(audio_dir / row[0], media_type="audio/wav")

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
            days = sorted({local(at).strftime("%b %-d") for at in times})
            lat, lon = spot["latitude"], spot["longitude"]
            clips = "".join(
                f"<audio controls preload='none' src='/audio/{quote(turn_id)}'></audio><div class='muted'>"
                f"{local(at):%b %-d %H:%M} · {seconds:.0f} s</div>"
                for turn_id, _, at, seconds in heard_at_spot(times, reading_times, turns))
            unnamed += (f"<li>About {len(times)} min on {', '.join(days)}"
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
{apply_form("/places")}
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
    async def tag_place(request: Request, background: BackgroundTasks) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await read_form(request)

        def field(key: str) -> str:
            return " ".join(form.get(key, [""])[0].split())

        action, name = field("action"), field("name")
        places_path = labels_dir / "places.jsonl"
        def radius(text: str, default: float = RADIUS) -> float:
            try:
                r = float(text)
            except ValueError:
                return default
            # NaN would pass the bounds, and infinity isn't an area anyone set.
            return min(max(r, MIN_RADIUS), MAX_RADIUS) if math.isfinite(r) else default

        def place(name: str, lat: float, lon: float, r: float) -> dict:
            # Only a radius the operator set is written, so the default can still change.
            return {"type": "place", "name": name, "latitude": lat, "longitude": lon,
                    **({"radius": r} if r != RADIUS else {})}

        def point() -> tuple[float, float] | None:
            """The form's latitude and longitude, or None if they aren't a point on earth."""
            try:
                lat, lon = float(field("latitude")), float(field("longitude"))
            except ValueError:
                return None
            return (lat, lon) if on_earth(lat, lon) else None

        def span() -> tuple[float, float] | None:
            """The form's start and end, or None if they aren't times."""
            try:
                start, end = float(field("start")), float(field("end"))
            except ValueError:
                return None
            return (start, end) if math.isfinite(start) and math.isfinite(end) else None

        here, times = point(), span()
        records = []
        if action == "name" and name and here:
            records = [place(name, *here, radius(field("radius")))]
        elif action == "rename" and name and field("from") and name != field("from"):
            records = [{"type": "rename", "from": field("from"), "to": name}]
        elif action in ("ignore", "unignore") and here:
            # As posted: the app sends a spot's centre in full, the page to 6 decimals (hearsay.places.read_ignored).
            records = [{"type": action, "latitude": here[0], "longitude": here[1]}]
        elif action == "forget" and field("from"):
            records = [{"type": "forget", "name": field("from")}]
        elif action in ("drop", "at") and times and (action == "drop" or name):
            # A span of readings that was wrong, or was really at a named place (hearsay/places.py).
            records = [{"type": action, "start": times[0], "end": times[1]} | ({"name": name} if action == "at" else {})]
        elif action == "move" and field("from"):
            # Its spots, where the phone's readings put them, give way to the points given:
            # one pasted here, or from the app's map each of the place's spots, one dragged
            # or resized. Each at may have a radius, in order; one without keeps the place's.
            points = [coordinates(at) for at in form.get("at", [])]
            current = max((sp[3] for sp in read_places(labels_dir) if sp[0] == field("from")), default=RADIUS)
            radii = form.get("radius", [])
            if points and None not in points:
                records = [{"type": "forget", "name": field("from")}] + [
                    place(field("from"), lat, lon, radius(radii[i], current) if i < len(radii) else current)
                    for i, (lat, lon) in enumerate(points)]
        for record in records:
            append_tag(record, places_path)
        if records:
            apply_later(background)
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
            [(conversations,)] = db.execute(
                "SELECT count(*) FROM conversation_audio WHERE wav_file IS NOT NULL AND NOT sealed").fetchall()
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
                  f"<button type='submit' name='action' value='update'>Update: re-transcribes the {conversations}"
                  " conversation(s) not yet sealed</button></form>")
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
    async def edit_words(request: Request, background: BackgroundTasks) -> Response:
        if not logged_in(request):
            return RedirectResponse("/login", status_code=303)
        form = await read_form(request, keep_blank_values=True)
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
            if corral_url:
                # Transcription is what words change, so the whole pipeline, not APPLY.
                background.add_task(enqueue_on_corral, enqueue.CHAIN)
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
    corral_url = os.environ.get("CORRAL_URL", "")
    if not corral_url:
        # A local dev portal (install/dev.sh) has no corral.
        log.warning("CORRAL_URL isn't set: edits are saved but no reprocess is enqueued to apply them")
    return create_app(
        Path(os.environ["HEARSAY_DB"]),
        Path(os.environ["HEARSAY_AUDIO_DIR"]),
        Path(os.environ["HEARSAY_LABELS_DIR"]),
        # The stream, read-only, to say what Idea Machine can see (install/compose.yaml).
        Path(os.environ["HEARSAY_STREAM_DIR"]),
        os.environ["HEARSAY_PORTAL_USER"],
        os.environ["HEARSAY_PORTAL_PASSWORD"],
        os.environ["HEARSAY_SESSION_KEY"],
        # From capture.env (install/compose.yaml); without it, app login is off.
        os.environ.get("HEARSAY_CAPTURE_TOKEN", ""),
        # Idea Machine's vocab.json, mounted read-only (install/compose.yaml).
        Path(os.environ["HEARSAY_VOCAB_FILE"]) if os.environ.get("HEARSAY_VOCAB_FILE") else None,
        # From corral.env (install/compose.yaml).
        corral_url,
        os.environ["CORRAL_TOKEN"] if corral_url else "",
    )

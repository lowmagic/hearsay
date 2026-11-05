"""The utterance stream: Hearsay's output, for downstream consumers.

Written after every reprocess from the database, one JSONL file per
conversation plus index.json. Consumers never receive audio.
The contract with consumers is docs/stream.md: change it with this file.

Everything here can change after the fact (the operator's correction of who
spoke applies to past turns, a growing conversation is re-transcribed), so the unit of
change is a conversation, not an utterance. index.json gives each
conversation a revision (a hash of its file); consumers re-read the
conversations whose revision changed and replace them wholesale. utterance_id
is stable only while a conversation's transcript is, which transcript_revision
tracks: it hashes the turns as transcribed (times, words, diarized voices),
never who they were attributed to, so corrections leave it alone.

Speakers are the owner or not: since 2026-10-28 Hearsay names nobody else,
so `person` is never written, though it stays a valid kind for consumers.

Files are written atomically and the index last, so a reader never sees a
half-written file, and a file is only rewritten when its content changes.

forgotten.json is the consumers' only deletion signal, so it is append-only:
nothing here ever rewrites or shrinks it. Each stretch the operator threw
out (hearsay/forget.py) is appended the first time a stream is written with
it, with the ids it removed. Whatever falls in a listed stretch stays out
of the stream for good, even if the mark is later taken back: its
utterances, its taps, and a conversation that lies wholly inside it.

A conversation enters the stream when its hold ends (hearsay/hold.py), and
once in, it stays: one that left would look to a consumer like speech that
vanished, so the index this replaces says what's already published.
"""

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from hearsay.hold import held
from hearsay.turns import categories

# Bump on any change to the index or utterance format that would break a consumer.
FORMAT_VERSION = 1

UTTERANCES_SQL = """
SELECT t.conversation_id, t.idx, t.start, t.end, t.text, t.diar_speaker, t.confidence,
       ts.label, ts.basis, ts.owner_similarity, ca.zero_at, ta.arousal,
       td.words_per_second, td.pauses, td.pause_seconds, t.turn_id
FROM turns t
JOIN turn_speakers ts ON ts.turn_id = t.turn_id
LEFT JOIN turn_affect ta ON ta.turn_id = t.turn_id
LEFT JOIN turn_delivery td ON td.turn_id = t.turn_id
JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id
ORDER BY t.conversation_id, t.start, t.idx
"""


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def speaker_kind(label: str | None, category: str | None, basis: str | None) -> tuple[str, str | None, str] | None:
    """(kind, name, basis), or None for turns left out of the stream.

    Categories (hearsay/turns.py) are the operator's: _stranger is someone
    present, and so is _mic, someone present on a microphone or PA, so both
    stay; _me is the owner, in turns their voice or channel
    said were someone else's; every other category (_noise, _media, _music)
    is not speech from anyone present and is left out.

    A turn labeled by its diarized speaker rather than its own voice
    (hearsay/speakers.py) says so: its basis is "diarization", whatever the
    kind. Likewise "channel" for one labeled by which channel of a Mac
    recording it was loud on, except a turn the operator says is theirs:
    that channel said it was the call's.
    """
    found = speaker_from_label(label, category)
    if found and (basis == "diarization" or basis == "channel" and category != "_me"):
        return found[0], found[1], basis
    return found


def speaker_from_label(label: str | None, category: str | None) -> tuple[str, str | None, str] | None:
    if category == "_me":
        return "owner", None, "named"
    if category in ("_stranger", "_mic"):
        return "stranger", None, "named"
    if category:
        return None
    if label == "owner":
        return "owner", None, "voice"
    if label == "not_owner":
        return "anonymous", None, "cluster"
    return "unknown", None, "none"


def letters(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def utterances(db: sqlite3.Connection) -> dict[str, list[dict]]:
    by_conversation = {}
    stutters = {}
    for turn_id, at, kind, before, word in db.execute(
        "SELECT turn_id, at, kind, words_per_second_before, word FROM turn_stutters ORDER BY turn_id, at"
    ):
        stutters.setdefault(turn_id, []).append((at, kind, before, word))
    tagged = categories(db)
    labels = {}  # (conversation, kind, voice) -> "anon A" / "stranger A"
    for (conversation_id, idx, start, end, text, diar_speaker, confidence,
         label, basis, similarity, zero_at, arousal,
         words_per_second, pauses, pause_seconds, turn_id) in db.execute(UTTERANCES_SQL):
        speaker = speaker_kind(label, tagged.get(turn_id), basis)
        if speaker is None:
            continue
        kind, name, basis = speaker
        speaker_label = None
        if kind in ("anonymous", "stranger"):
            # Diarization tells voices apart within a conversation; turns it
            # didn't assign share one label.
            key = (conversation_id, kind, diar_speaker)
            if key not in labels:
                count = sum(1 for c, k, _ in labels if c == conversation_id and k == kind)
                labels[key] = f"{'anon' if kind == 'anonymous' else 'stranger'} {letters(count)}"
            speaker_label = labels[key]
        zero = datetime.fromisoformat(zero_at).timestamp()
        utterance = {
            "conversation_id": conversation_id,
            "utterance_id": f"{conversation_id}:{idx:04d}",
            "start": iso(zero + start),
            "end": iso(zero + end),
            "speaker": {"kind": kind, "name": name, "label": speaker_label},
            "text": text,
            "text_confidence": round(confidence, 3) if confidence is not None else None,
            "speaker_confidence": {"basis": basis,
                                   "owner_similarity": round(similarity, 3) if similarity is not None else None},
        }
        # The owner's tone of voice, only where it was scored (hearsay/affect.py).
        if arousal is not None and kind == "owner":
            utterance["affect"] = {"arousal": round(arousal, 3)}
        # How the owner delivered it (hearsay/delivery.py), where measured.
        if words_per_second is not None and kind == "owner":
            utterance["delivery"] = {
                "words_per_second": words_per_second, "pauses": pauses, "pause_seconds": pause_seconds,
                "stutters": [{"kind": k, "at": iso(zero + at), "words_per_second_before": before, "word": word}
                             for at, k, before, word in stutters.get(turn_id, [])],
            }
        by_conversation.setdefault(conversation_id, []).append(utterance)
    return by_conversation


def transcript_revisions(db: sqlite3.Connection) -> dict[str, str]:
    """A hash per conversation of what its utterance ids name, and nothing else."""
    rows = {}
    for conversation_id, zero_at, *turn in db.execute(
        "SELECT t.conversation_id, ca.zero_at, t.idx, t.start, t.end, t.text, t.diar_speaker, t.confidence"
        " FROM turns t JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
        " ORDER BY t.conversation_id, t.idx"
    ):
        rows.setdefault(conversation_id, [zero_at]).append(turn)
    return {c: hashlib.sha256(json.dumps(r).encode()).hexdigest()[:16] for c, r in rows.items()}


def within(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def overlaps(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    return any(s < end and start < e for s, e in spans)


def write_if_changed(path: Path, data: bytes) -> None:
    if path.exists() and path.read_bytes() == data:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def published(stream_dir: Path) -> dict[str, tuple[float, float]]:
    """The conversations in the stream as last written, with their spans."""
    index = stream_dir / "index.json"
    entries = json.loads(index.read_text())["conversations"] if index.exists() else []
    return {e["conversation_id"]: (datetime.fromisoformat(e["start"]).timestamp(),
                                   datetime.fromisoformat(e["end"]).timestamp()) for e in entries}


def write_stream(db_path: Path, stream_dir: Path, now: float | None = None) -> dict:
    db = sqlite3.connect(db_path)
    try:
        found = utterances(db)
        transcripts = transcript_revisions(db)
        conversations = db.execute("SELECT conversation_id, start, end, open FROM conversations ORDER BY start").fetchall()
        taps = {}
        for conversation_id, at in db.execute("SELECT conversation_id, at FROM conversation_taps ORDER BY at"):
            taps.setdefault(conversation_id, []).append(at)
        places = {}
        for conversation_id, place, start, end in db.execute(
            "SELECT conversation_id, place, start, end FROM conversation_places ORDER BY start"
        ):
            places.setdefault(conversation_id, []).append({"name": place, "start": iso(start), "end": iso(end)})
        thrown_out = db.execute(
            "SELECT start, end, marked_at, reason, conversation_id, turn_ids FROM forgotten ORDER BY start").fetchall()
        releases = db.execute("SELECT start, end FROM releases").fetchall()
    finally:
        db.close()

    stream_dir.mkdir(parents=True, exist_ok=True)
    forgotten_path = stream_dir / "forgotten.json"
    forgotten = json.loads(forgotten_path.read_text()) if forgotten_path.exists() else []
    listed, known = {(f["start"], f["end"]) for f in forgotten}, len(forgotten)
    for start, end, marked_at, reason, conversation_id, turn_ids in thrown_out:
        if (iso(start), iso(end)) not in listed:
            forgotten.append({"forgotten_at": marked_at, "start": iso(start), "end": iso(end),
                              "conversation_id": conversation_id, "utterance_ids": json.loads(turn_ids),
                              "reason": reason})
            listed.add((iso(start), iso(end)))
    if len(forgotten) > known or not forgotten_path.exists():
        write_if_changed(forgotten_path, json.dumps(forgotten, indent=2, ensure_ascii=False).encode() + b"\n")
    gone = [(datetime.fromisoformat(s).timestamp(), datetime.fromisoformat(e).timestamp()) for s, e in listed]
    for conversation_id in found:
        found[conversation_id] = [u for u in found[conversation_id] if not overlaps(
            gone, datetime.fromisoformat(u["start"]).timestamp(), datetime.fromisoformat(u["end"]).timestamp())]
    # One with every utterance thrown out has none: not transcribed, and no file.
    found = {c: kept for c, kept in found.items() if kept}

    conversation_dir = stream_dir / "conversations"
    conversation_dir.mkdir(parents=True, exist_ok=True)
    # A conversation over speech already published isn't held, though late
    # audio may have given it a new id: the stream never withdraws speech.
    published_spans = list(published(stream_dir).values())
    now = time.time() if now is None else now
    index, holding = [], 0
    for conversation_id, start, end, is_open in conversations:
        if within(gone, start, end):
            continue
        if not overlaps(published_spans, start, end) and held(start, end, releases, now):
            holding += 1
            continue
        entry = {"conversation_id": conversation_id, "start": iso(start), "end": iso(end),
                 "open": bool(is_open), "transcribed": conversation_id in found,
                 "taps": [iso(at) for at in taps.get(conversation_id, []) if not within(gone, at, at)], "places": places.get(conversation_id, []),
                 "utterances": 0, "revision": None, "transcript_revision": None, "file": None}
        if conversation_id in found:
            data = "".join(json.dumps(u, ensure_ascii=False) + "\n" for u in found[conversation_id]).encode()
            name = f"conversations/{conversation_id}.jsonl"
            write_if_changed(stream_dir / name, data)
            entry.update(utterances=len(found[conversation_id]), revision=hashlib.sha256(data).hexdigest()[:16],
                         transcript_revision=transcripts[conversation_id], file=name)
        index.append(entry)
    write_if_changed(stream_dir / "index.json",
                     json.dumps({"format_version": FORMAT_VERSION, "conversations": index}, indent=2).encode())

    current = {e["file"] for e in index if e["file"]}
    for path in conversation_dir.glob("*.jsonl"):
        if f"conversations/{path.name}" not in current:
            path.unlink()
    return {"conversations": len(index), "transcribed": len(current),
            "utterances": sum(e["utterances"] for e in index), "held": holding}

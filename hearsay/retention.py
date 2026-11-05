"""What Hearsay deletes, and when (roadmap slice 12).

Both rules run at the end of every reprocess, after sealing
(hearsay/records.py), and touch only what is sealed: by then a
conversation's record holds everything its audio gave, and nothing that
re-runs reads that audio again.

Thrown out (hearsay/forget.py), once the stretch's conversation is sealed:
raw uploads and imports whose audio lies wholly inside the stretch are
deleted; the stretch is silenced in the conversation's WAV; and the words of
the turns it removes are blanked in the record and in the dev box's
transcript file, and those turns' vectors and scores leave the record.
Blanked, not removed: a word keeps its timing and speaker, which are all
split_turns cuts turns by, so turns are built from the record as before and
the mark removes the same ones. Not before sealing: a WAV changed while its
conversation is still built from raw would be transcribed again. An upload
that only reaches into a stretch stays until retention, so an unseal still
has the audio around it; its thrown-out audio never reaches the stream, as
the mark applies again. Where a run of audio went on across the uploads
deleted, what follows them can be placed up to a second or two differently
after an unseal, inside a conversation that is transcribed again anyway (its
audio changed); the conversations around it, minutes of silence away, are
rebuilt exactly.

Past RETAIN, everything that is audio, the owner's too: raw capture and Omi
audio uploads whose audio ends before now - RETAIN, imports that end before
it, and the WAVs and transcript files of conversations that ended before it.
Raw uploads and imports overlapping an enrollment window
(labels/enrollment.json) are kept for good, so the owner can be enrolled in
a new speaker model. Omi's transcript and memory payloads are text, not
audio, and are kept. Transcript files stay until then, cut as above:
`python -m hearsay.records check` and an unseal within the window read them.
Decoded audio needs nothing here: only audio not yet sealed is decoded, so
the decoded caches drop sealed audio on the next run (hearsay/capture.py,
hearsay/imports.py), and the model cache drops what no run has used for HOLD
(hearsay/cache.py).

Raw is deleted whole, never edited, its sidecar first, so a body left behind
reads as an incomplete upload. Each deletion is logged in the records dir's
deleted.jsonl before the file goes: a deleted upload's parsed rows go on
loading from its digest wherever the seal line moves
(records.sealed_payloads), and `records check` knows where raw is whole.

    python -m hearsay.retention report

prints what the next reprocess would delete and cut, changing nothing.
"""

import copy
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import wave
from collections import Counter
from pathlib import Path
from typing import NamedTuple

from hearsay import capture
from hearsay.assemble import BYTES_PER_SECOND, SAMPLE_RATE, place_omi, timestamp, write_wav
from hearsay.records import deleted, iso, lines, read_seal, seal, write_json
from hearsay.turns import split_turns

RETAIN = 60 * 86400  # seconds: audio is kept this long after its conversation ends

# The longest one of the pendant's frames lasts, in seconds.
FRAME = max(capture.FRAME_SAMPLES.values()) / SAMPLE_RATE


class Deletion(NamedTuple):
    kind: str                # upload, import, wav or transcript
    why: str                 # thrown out or retention
    name: str                # the upload's raw path, the import's file, or the file's name
    files: tuple[Path, ...]  # in the order deleted: a sidecar before its body
    size: int                # bytes
    end: float               # unix seconds its audio ends; a WAV's or transcript's conversation's end
    logged: bool             # in deleted.jsonl already: a run stopped before deleting it


class Cut(NamedTuple):
    """Stretches thrown out of a sealed conversation, to cut from what's kept of it."""
    conversation_id: str
    record_path: Path
    record: dict             # as it will be written
    wav: Path | None         # to silence, if it's kept
    silence: list[tuple[float, float]]  # seconds into the WAV
    seconds: float           # of the WAV, silenced
    transcript_path: Path | None  # the dev box's transcript, if it's kept and changes
    transcript: dict | None
    words: int               # cut from the record


class Plan(NamedTuple):
    cutoff: float            # retention line: unix seconds
    deletions: list[Deletion]
    cuts: list[Cut]
    kept: Counter            # raw uploads and imports that stay, by why


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and a[1] > b[0]


def within(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return b[0] <= a[0] and a[1] <= b[1]


def capture_span(body: bytes) -> tuple[float, float] | None:
    """The earliest and latest an upload's audio can be placed
    (hearsay/capture.py), or None if it holds none. Bounds, not where it
    lands, which also depends on the uploads around it: a live frame ends
    within REANCHOR of its arrival, a stored packet within STORED_REANCHOR
    of the pendant's time on it, and a Mac frame starts within REANCHOR of
    its stamp."""
    spans = []
    for at, kind, data in capture.read_records(body):
        if kind == capture.AUDIO:
            spans.append((at - capture.REANCHOR - FRAME, at + capture.REANCHOR))
        elif kind == capture.STORED and len(data) == capture.STORED_HEADER.size + capture.STORED_PACKET:
            packet = data[capture.STORED_HEADER.size:]
            stamp = int.from_bytes(packet[:4], "big") + 0.5
            seconds = len(capture.stored_frames(packet[4:])) * FRAME
            spans.append((stamp - seconds - capture.STORED_REANCHOR, stamp + capture.STORED_REANCHOR))
        elif kind in (capture.MIC, capture.SYSTEM) and len(data) > capture.MAC_FRAME.size:
            (samples,) = capture.MAC_FRAME.unpack_from(data)
            spans.append((at - capture.REANCHOR, at + samples / SAMPLE_RATE + capture.REANCHOR))
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def omi_spans(db: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """Where each of Omi's audio chunks lies, as place_bursts lays them out
    with every chunk ever received; a duplicate where the chunk it repeats does."""
    rows = db.execute("SELECT path, received_at, body_bytes FROM payloads WHERE type = 'audio'"
                      " AND duplicate_of IS NULL ORDER BY received_at, path").fetchall()
    size = {path: n for path, _, n in rows}
    spans = {}
    for start, _, paths in place_omi(rows):
        for path in paths:
            spans[path] = (start, start + size[path] / BYTES_PER_SECOND)
            start = spans[path][1]
    for path, original in db.execute(
            "SELECT path, duplicate_of FROM payloads WHERE type = 'audio' AND duplicate_of IS NOT NULL"):
        if original in spans:
            spans[path] = spans[original]
    return spans


def turn_words(segments: list[dict]) -> list[tuple[float, float, list[tuple[int, int | None]]]]:
    """(start, end, where its words are) per turn, as split_turns cuts them:
    (segment, word), or (segment, None) for a segment kept whole. split_turns
    itself cuts segments whose words are their own positions, so the two
    can't drift apart."""
    marked = [{**s, "text": json.dumps([i, None]),
               "words": [{**w, "word": json.dumps([i, j])} for j, w in enumerate(s.get("words") or [])]}
              for i, s in enumerate(segments)]
    return [(t["start"], t["end"], [tuple(json.loads(w)) for w in t["words"]]) for t in split_turns(marked)]


def cut_words(transcript: dict, zero: float, stretches: list[tuple[float, float]]
              ) -> tuple[dict, list[tuple[int, float, float]], int]:
    """The transcript with the words of every turn a stretch overlaps at all
    (as hearsay/forget.py removes them) blanked; those turns (index, start,
    end); and how many words were blanked."""
    cut = copy.deepcopy(transcript)
    segments = cut["segments"]
    hits, words, touched = [], 0, set()
    for idx, (start, end, positions) in enumerate(turn_words(segments)):
        if not any(overlaps((zero + start, zero + end), s) for s in stretches):
            continue
        hits.append((idx, start, end))
        for i, j in positions:
            touched.add(i)
            if j is None:
                words += len(segments[i]["text"].split())
                segments[i]["text"] = ""
            else:
                words += bool(segments[i]["words"][j]["word"])
                segments[i]["words"][j]["word"] = ""
    for i in touched:
        if segments[i].get("words"):
            # A sentence's text repeats its words: keep only those left.
            segments[i]["text"] = " ".join(w["word"] for w in segments[i]["words"] if w["word"])
    return cut, hits, words


def silenced(spans: list[tuple[float, float]], duration: float) -> float:
    """Seconds of [0, duration) the spans cover."""
    total, reach = 0.0, 0.0
    for start, end in sorted((max(0.0, s), min(duration, e)) for s, e in spans):
        total += max(0.0, end - max(start, reach))
        reach = max(reach, end)
    return total


def silence(path: Path, spans: list[tuple[float, float]]) -> str:
    """Zero the spans (seconds) in the WAV at path, rewriting it atomically if
    that changes it, and return its sha256."""
    with wave.open(str(path)) as w:
        pcm = bytearray(w.readframes(w.getnframes()))
    total = len(pcm) // 2
    for start, end in spans:
        lo, hi = max(0, math.floor(start * SAMPLE_RATE)), min(total, math.ceil(end * SAMPLE_RATE))
        if hi > lo:
            pcm[lo * 2:hi * 2] = bytes((hi - lo) * 2)
    sha256, _ = write_wav(path, bytes(pcm), path)
    return sha256


def enrollment_windows(labels_dir: Path) -> list[tuple[float, float]]:
    path = labels_dir / "enrollment.json"
    return [(timestamp(w["start"]), timestamp(w["end"]))
            for w in (json.loads(path.read_text()) if path.exists() else [])]


def plan(env: dict[str, Path], records_dir: Path, now: float) -> Plan:
    """What retention does now, given the records behind the line and the
    database the last run built (its throw-outs, and where Omi's audio lies)."""
    cutoff = now - RETAIN
    line = read_seal(records_dir)["line"]
    records = {}
    for path in sorted((records_dir / "conversations").glob("*.json")):
        record = json.loads(path.read_text())
        if record["conversation"]["end"] <= line:  # past it: written by a run that stopped before moving it
            records[path] = record
    db = sqlite3.connect(env["db"])
    try:
        stretches = sorted(set(db.execute("SELECT start, end FROM forgotten")))
        omi = omi_spans(db)
    finally:
        db.close()
    # Only stretches in a sealed conversation: elsewhere the audio is still read.
    stretches = [s for s in stretches if any(overlaps(s, (r["conversation"]["start"], r["conversation"]["end"]))
                                             for r in records.values())]
    windows = enrollment_windows(env["labels_dir"])
    logged = {(e["kind"], e["name"]): e for e in deleted(records_dir)}
    deletions, kept = {}, Counter()

    def delete(kind: str, why: str, name: str, files: list[Path], end: float) -> None:
        files = [f for f in files if f.exists()]
        if files and (kind, name) not in deletions:
            entry = logged.get((kind, name))
            deletions[(kind, name)] = Deletion(kind, entry["why"] if entry else why, name, tuple(files),
                                               sum(f.stat().st_size for f in files),
                                               entry["end"] if entry else end, entry is not None)

    def raw_audio(kind: str, name: str, files: list[Path], audio: tuple[float, float] | None, end: float) -> None:
        """Decide for a sealed upload or import, given where its audio lies."""
        if audio and any(within(audio, s) for s in stretches):
            delete(kind, "thrown out", name, files, audio[1])
        elif end < cutoff and not (audio and any(overlaps(audio, w) for w in windows)):
            delete(kind, "retention", name, files, end)
        elif end < cutoff:
            kept["covering an enrollment window"] += 1
        elif audio and any(overlaps(audio, s) for s in stretches):
            kept["reaching into a thrown-out stretch, until retention"] += 1

    for (kind, name), entry in logged.items():  # a run stopped before deleting these
        if kind == "upload":
            body = env["raw_dir"] / name
            delete(kind, entry["why"], name, [body.with_suffix(".json"), body], entry["end"])
        elif kind == "import":
            delete(kind, entry["why"], name, [env["imports_dir"] / f"{name}.json", env["imports_dir"] / name],
                   entry["end"])

    digests = {d.get("path") or d["import"]: d for d in lines(records_dir / "payloads.jsonl") if d["last_at"] < line}
    for name, digest in digests.items():
        if "import" in digest:
            sidecar = env["imports_dir"] / f"{name}.json"
            if sidecar.exists():
                start = timestamp(json.loads(sidecar.read_text())["start"])
                raw_audio("import", name, [sidecar, env["imports_dir"] / name], (start, digest["last_at"]),
                          digest["last_at"])
            continue
        body = env["raw_dir"] / name
        if not body.exists():
            continue
        _, kind, received_at, _, _, _, _, original, first_at, last_at, _ = digest["rows"]["payloads"][0]
        end = last_at if last_at is not None else timestamp(received_at)
        if kind == "audio":
            audio = omi.get(original or name)
        elif first_at is not None and (end < cutoff or any(within((first_at, last_at), s) for s in stretches)):
            # Read only when it may go: for how far its audio can be placed from when its records arrived.
            audio = capture_span(body.read_bytes())
        else:
            audio = (first_at, last_at) if first_at is not None else None
        raw_audio("upload", name, [body.with_suffix(".json"), body], audio, audio[1] if audio else end)

    cuts = []
    for path, record in records.items():
        c, cid = record["conversation"], record["conversation"]["conversation_id"]
        expired = c["end"] < cutoff
        done = {tuple(s) for s in record.get("discarded", [])}
        new = [s for s in stretches if overlaps(s, (c["start"], c["end"])) and s not in done]
        wav = env["audio_dir"] / f"{cid}.wav"
        transcripts = [env["transcripts_dir"] / f"{cid}.json", env["transcripts_dir"] / "failed" / f"{cid}.json"]
        if new:
            cuts.append(cut(path, record, new, None if expired or not wav.exists() else wav,
                            None if expired else transcripts[0]))
        if expired:
            delete("wav", "retention", wav.name, [wav], c["end"])
            for transcript in transcripts:
                delete("transcript", "retention", str(transcript.relative_to(env["transcripts_dir"])), [transcript],
                       c["end"])
    return Plan(cutoff, list(deletions.values()), cuts, kept)


def cut(path: Path, record: dict, stretches: list[tuple[float, float]], wav: Path | None,
        transcript_path: Path | None) -> Cut:
    """Take the stretches out of a sealed conversation's record, WAV and transcript file."""
    c, audio = record["conversation"], record["audio"]
    zero = timestamp(audio["zero_at"])
    new = copy.deepcopy(record)
    hits, words = [], 0
    if record["transcript"] is not None:
        new["transcript"], hits, words = cut_words(record["transcript"], zero, stretches)
    removed = {f"{c['conversation_id']}:{idx:04d}" for idx, _, _ in hits}
    new["turns"] = {turn_id: t for turn_id, t in record["turns"].items() if turn_id not in removed}
    new["discarded"] = record.get("discarded", []) + [list(s) for s in stretches]
    # The stretch, and all of every turn it removes, whose words are blanked.
    spans = [(s - zero, e - zero) for s, e in stretches] + [(start, end) for _, start, end in hits]
    transcript = None
    if transcript_path and transcript_path.exists():
        found = json.loads(transcript_path.read_text())
        transcript = cut_words(found, zero, stretches)[0] if found.get("segments") else found
        if transcript == found:
            transcript_path = transcript = None
    else:
        transcript_path = None
    return Cut(c["conversation_id"], path, new, wav, spans, silenced(spans, audio["duration"]) if wav else 0.0,
               transcript_path, transcript, words)


def apply(plan: Plan, env: dict[str, Path], records_dir: Path, now: float) -> None:
    logging = [d for d in plan.deletions if not d.logged]
    if logging:
        with open(records_dir / "deleted.jsonl", "a") as f:
            f.writelines(json.dumps({"kind": d.kind, "name": d.name, "why": d.why, "end": d.end, "deleted_at": now})
                         + "\n" for d in logging)
            f.flush()
            os.fsync(f.fileno())
    db = sqlite3.connect(env["db"])
    try:
        # The database in place is changed to match, so it never describes audio that's gone.
        for c in plan.cuts:
            if c.wav:
                c.record["audio"]["wav_sha256"] = silence(c.wav, c.silence)
                db.execute("UPDATE conversation_audio SET wav_sha256 = ? WHERE conversation_id = ?",
                           (c.record["audio"]["wav_sha256"], c.conversation_id))
                db.commit()
            if c.transcript_path:
                write_json(c.transcript_path, c.transcript)
            # Last: until its stretches are listed in it, a run redoes them.
            write_json(c.record_path, c.record)
        db.executemany("UPDATE conversation_audio SET wav_file = NULL WHERE wav_file = ?",
                       [(d.name,) for d in plan.deletions if d.kind == "wav"])
        db.commit()
    finally:
        db.close()
    for d in plan.deletions:
        for f in d.files:
            f.unlink(missing_ok=True)


def summary(plan: Plan) -> dict[str, str]:
    """Counts and bytes per kind, and when the audio affected ends, oldest and newest."""
    def described(found: list[Deletion]) -> str:
        if not found:
            return "none"
        return (f"{len(found)}, {sum(d.size for d in found) / 1e6:.1f} MB, ending {iso(min(d.end for d in found))}"
                f" to {iso(max(d.end for d in found))}")

    out = {"retention line": iso(plan.cutoff)}
    for why, kinds in (("thrown out", ("upload", "import")), ("retention", ("upload", "import", "wav", "transcript"))):
        for kind in kinds:
            label = {"upload": "raw uploads", "import": "imports", "wav": "conversation WAVs",
                     "transcript": "transcript files"}[kind]
            out[f"{label} deleted, {why}"] = described([d for d in plan.deletions if (d.kind, d.why) == (kind, why)])
    out["sealed conversations cut, thrown out"] = (
        f"{len(plan.cuts)}: {sum(c.words for c in plan.cuts)} word(s) from records,"
        f" {sum(c.seconds for c in plan.cuts) / 60:.1f} min silenced in {sum(1 for c in plan.cuts if c.wav)} WAV(s),"
        f" {sum(1 for c in plan.cuts if c.transcript_path)} transcript file(s) cut")
    out |= {f"raw uploads and imports kept, {why}": str(n) for why, n in sorted(plan.kept.items())}
    return out


def run(env: dict[str, Path], now: float | None = None) -> dict[str, str]:
    """Delete and cut what's due, after a run has sealed."""
    now = time.time() if now is None else now
    found = plan(env, env["records_dir"], now)
    apply(found, env, env["records_dir"], now)
    return summary(found)


def report(env: dict[str, Path], now: float | None = None) -> dict[str, str]:
    """What the next run would delete and cut, sealing what it would seal on a
    copy of the records; changes nothing."""
    now = time.time() if now is None else now
    with tempfile.TemporaryDirectory(prefix="hearsay-retention-") as scratch:
        records_dir = Path(scratch) / "records"
        shutil.copytree(env["records_dir"], records_dir)
        seal(env["db"], records_dir, env["transcripts_dir"], env["labels_dir"], now)
        return summary(plan(env, records_dir, now))


def main() -> None:
    from hearsay.reprocess import lock  # imported here: reprocess runs this module

    env = {name: Path(os.environ[f"HEARSAY_{name.upper()}"]) for name in
           ("raw_dir", "db", "audio_dir", "labels_dir", "transcripts_dir", "imports_dir", "records_dir")}
    if sys.argv[1:] != ["report"]:
        sys.exit("usage: python -m hearsay.retention report")
    if not env["db"].exists():
        sys.exit(f"No database at {env['db']} yet: nothing is sealed, so nothing would be deleted.")
    with lock(env["db"]):
        found = report(env)
    print("The next reprocess would delete and cut (counts, size, when the audio ends):")
    for name, value in found.items():
        print(f"  {name}: {value}")


if __name__ == "__main__":
    main()

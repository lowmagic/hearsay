# Hearsay

Hearsay turns captured audio into attributed utterances: who said what, when.
It ingests audio from the pendant (through the iPhone app) and the Mac,
assembles it, transcribes, diarizes, and
identifies speakers (me / anonymous cluster / tagged person).

Hearsay does NOT categorize, summarize, extract ideas, or interpret content.
That belongs to downstream consumers (Idea Machine). If a change requires
understanding what was said, it is out of scope.

## Output contract

The product of Hearsay is a stream of utterance records (`hearsay/stream.py`,
written to `/mnt/fast/hearsay/stream/` after every reprocess): one JSONL
file per conversation plus `index.json`. Each utterance has conversation_id,
utterance_id, start/end (UTC), speaker {kind, name, label}, text,
text_confidence (Parakeet's token probabilities, each word its least
likely token) and speaker_confidence {basis,
owner_similarity}; basis is voice, named, cluster, channel (in a Mac
recording, the turn was loud on the mic, so the owner's, or on the call, so
someone else's), diarization (the turn took its label from its diarized
speaker in the conversation, not its own voice) or none. Speaker kinds: owner, person (named), anonymous (labeled
per conversation, e.g. "anon A"), stranger, unknown. `_noise`, `_media` and `_music`
turns are left out (every category but `_stranger` and `_me`); `_` names are categories, not people.
`_me` turns are the owner's, basis named (or diarization), where the operator
heard the owner in turns their voice or channel gave to someone else. Owner
utterances may also carry `affect` {arousal}: how calm or animated the owner
sounded, about 0-1, from the audio alone (`hearsay/affect.py`); absent where
the turn wasn't scored. Other people's tone is never inferred. They may also
carry `delivery` {words_per_second, pauses, pause_seconds, stutters: [{kind
(repeat, restart, filler), at, words_per_second_before, word}]}: measured from the
word timings (`hearsay/delivery.py`), not judged; absent where not measured.
`word` is the stutter's position in `text` split on whitespace (a repeat's
first copy, a restart's fragment, the filler), so dropping those words gives
the text without them; null where it couldn't be placed.

Each index entry also lists `taps`: when the owner marked that conversation
with the pendant's button (a start mark, e.g. a self-note, kept even when
it's short). The owner can also end a conversation with the button, which
splits it there. Both are facts about the capture, not interpretations of it.
So is `places`: the places the operator has named that the conversation
happened at, in order, each {name, start, end}, from the phone's location
while it recorded. Never coordinates; a spot nobody named is left out, and
naming one later adds it retroactively.

Speakers and text change after the fact (naming is retroactive, growing
conversations are re-transcribed), so the unit of change is a conversation:
consumers re-read conversations whose index revision changed and replace them
wholesale. Downstream consumers read this. They never receive audio.

Guarantees consumers rely on (Idea Machine, `docs/ROADMAP.md` §3 there):

- `index.json` has `format_version` (an integer, now 1). Bump it on any change
  to the index or utterance format that would break a consumer.
- Files are written atomically (temp file, then rename), conversation files
  first and `index.json` last, and only when their content changes.
- `revision` is the first 16 hex characters of the sha256 of the
  conversation file's bytes, so a consumer can detect a read that raced a
  rewrite.
- `transcript_revision` changes when a conversation's transcript does
  (re-transcription, a growing conversation) and not when only speaker fields
  do (naming, merging, owner relabeling). Within one transcript_revision a
  given utterance_id always names the same stretch of speech; across them,
  ids are renumbered.
- `forgotten.json` is an append-only list, never rewritten or shrunk, one
  entry per thing the operator asked Hearsay to forget: {forgotten_at, start,
  end, conversation_id, utterance_ids, reason}, ids as they were. It is the
  only deletion signal: anything else that leaves the stream was
  restructured, not deleted. Forgetting is operator input applied on every
  reprocess, so re-running from raw never brings forgotten speech back into
  the stream. Raw payloads and audio of a forgotten span are kept for now:
  deleting them is slice 12's call, alongside retention. Until the operator
  can forget (slice 12), the list is empty.

## Invariants

- Raw webhook payloads are written to disk verbatim before any parsing.
  Never modify or delete raw payloads as part of processing.
- Audio never leaves Hearsay. Hearsay's boundary is the operator's home LAN
  and Tailscale tailnet: the NAS, the dev box (which has the GPU), the
  operator's Mac laptop, and the operator's phone. Audio, including non-owner
  audio, may move between them.
  It is never exposed on the public internet (nothing of Hearsay's is; the
  Omi webhook receiver and its Cloudflare tunnel were retired in 2026-10)
  and never goes to third-party services or downstream consumers.
- Non-owner audio is deleted after embedding and transcription, per retention
  policy. Owner audio may be kept.
- Speaker embeddings are retained indefinitely; tagging and merging must be
  retroactive.
- All processing must be re-runnable from raw payloads.
- A reprocess with nothing new writes almost nothing: the database is updated
  in place (only changed rows), and a WAV is written only when its bytes
  change. Hearsay's data is on the NAS's NVMe pool, snapshotted nightly, so
  every rewritten byte is kept many times.
- Data directories and secrets are never committed.

## Current slice

Slice 18: review that shows what was said, and gates new matches (see the
roadmap). Every clip in the portal shows its words, time and place. Next,
voiceprints built only from the operator's input, and new matches to a
named person gated in batches, where rejecting one keeps that speaker apart
from them. Then slice 19 (turns kept without their audio) and slice 12
(retention), which change the invariants on raw payloads and re-running
from raw.

Slice 17: how the owner holds a turn (see the roadmap). Owner utterances
carry `delivery`, from Parakeet's word timings (`hearsay/delivery.py`):
speaking rate, pauses, and stutters (repeats, restarts, fillers) each with the
speaking rate before it. Stutter detection was checked by ear (20 of 24
real). No assertiveness label: ending pitch, rate, pauses and trailing off
didn't match the owner's blind ratings of calm turns.

Slice 16: the owner's tone of voice (see the roadmap). Owner turns labeled by
voice or channel are scored on reprocess (`hearsay/affect.py`, audeering's
wav2vec2, CC-BY-NC-SA, baked into the worker image); only arousal reaches the
stream, the one dimension checked by ear. Valence waits for its own check
(tone without words, and a blind rating); dominance follows arousal for every
voice measured and stays out. Other people's tone is never inferred.

Slice 14: place as a hint for who is speaking (see the roadmap). When the
operator names a cluster in the portal, offer the people heard at the
conversation's places first. Voice stays the judge: a place never names
anyone on its own, and never overrides a voice label or the operator's names.

Slice 15 (media hint) is built and being measured alongside 14: a cluster
that sounds like the media the operator tagged offers `_media` for one tap
(`hearsay/media.py`), never tagged on its own.

Slice 13 (where a conversation happened) is done: the iPhone app samples the
owner's location while recording (precise fixes while moving, an anchor
while staying put, `ios/Hearsay/Movement.swift`; records in the capture
upload, `hearsay/capture.py`), and each conversation in the stream lists the
places the operator named that it happened at (`hearsay/places.py`,
labels/places.jsonl), never coordinates.

Slice 12 (retention) follows slices 18 and 19: then all audio past a
rolling window (90 days to start) is deleted, the owner's too except the
enrollment windows', and a forgotten span leaves the stream and is listed in
`forgotten.json`.

Slice 11 (Mac audio capture) is done: a menu-bar app on the operator's
personal Mac records the mic and the call as separate channels, only after
the operator says yes to a Zoom call's prompt (`mac/`, `install/mac.sh`).
Each turn is attributed by the channel it's loud on (`with_channel` in
`hearsay/speakers.py`), unless the voice clearly says otherwise; a work
machine is never used for capture or holds audio.

Slice 18 is done when the operator gates new matches in batches, rejected
speakers stay apart from the person, and voiceprints come only from the
operator's input. Slice 14 is done when naming a cluster offers the people
heard at that place first, and that measurably saves the operator time.

## Coding standards

Code, tests, comments and prose follow [docs/standards.md](docs/standards.md),
which builds on the org's [docs/org-standards.md](docs/org-standards.md).
`make check` runs everything CI runs.

## Installation

All installation and deployment lives in `install/`, one script per target
(e.g. `install/dev.sh`, `install/nas.sh`, `install/gpu.sh`). Shared helpers
go in `install/lib.sh`. No install scripts live outside `install/`.

`docs/install.md` is the operator's guide (`README.md` is the project
overview and links to it). Its Install section orients installation: which
script runs on which host, in what order, and anything that happens between
scripts (pre-flight checks, post-flight verification, operator actions that
span targets). It describes a fresh install only. The sections after it cover
running Hearsay: recording, reprocessing, the owner's voice, the stream,
imports and naming. It links to scripts; it does not duplicate what they do.

- Any step inside a single target belongs in that target's script. A
  within-target step that only exists in docs/install.md or a chat is a bug.
- Each script is idempotent: re-running it on an installed system is safe and
  changes nothing that's already correct.
- Steps that require the operator are still owned by the script: it prints
  exact instructions, waits for confirmation, then verifies the result where
  it can. A step is manual only when it requires the operator's account,
  device, or judgment; say why in the script.
- If one target depends on another, the script checks for it and says which
  script to run, rather than silently doing the other target's work.
- If a change requires a new setup step, update the relevant script in the
  same change. If it affects order, hosts, or between-script actions, update
  docs/install.md in the same change too.
- Secrets are read from an env file outside the repo, never written into it.
- A step that changes an existing install (a move, a retirement) says in its
  comment which hosts it's for, and is deleted once they've all run it.

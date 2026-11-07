# The utterance stream

Hearsay's product, and the only thing a consumer reads from it: who said what, when, as one JSONL file per
conversation plus an index. Idea Machine is the consumer today. This file is the contract between them; anything
here that disagrees with `hearsay/stream.py` is a bug in this file. Idea Machine's
[ROADMAP §3.3](https://github.com/junglestyle/ideamachine/blob/main/docs/ROADMAP.md#33-how-idea-machine-imports-it)
on is how it imports the stream, which is Idea Machine's business.

The stream holds no audio, ever. It is rewritten after every reprocess into `/mnt/fast/hearsay/stream/`, readable by
the `apps` group:

- `index.json`: every conversation, under a top-level `format_version`.
- `conversations/<conversation_id>.jsonl`: one utterance per line, in time order.
- `forgotten.json`: what the operator asked Hearsay to forget.

## Conversations

Hearsay finds conversations in the audio from speech alone (`hearsay/conversations.py`). A conversation ends after
180 s without speech, or where the owner taps the pendant's button to end it. One with under 30 s of speech is dropped,
unless the owner made a start tap within 30 s of it (a self-note). Nothing about what was said is consulted, so a
conversation can run for hours.

A conversation's id is `c` and the UTC time of its first speech, e.g. `c20000101T000014Z`. It stays the same as the
conversation grows. When its start moves (late audio joins two, or starts one earlier) or an end tap splits one, the
conversations involved get new ids and the old ones leave the index. That's a restructure, not a deletion.

`index.json` is `{"format_version": 1, "conversations": [...]}`, oldest first. Each entry:

- `conversation_id`
- `start`, `end`: UTC, 1 s before the first speech and after the last.
- `open`: true while the audio ends less than 180 s after the last speech, so the conversation may still grow.
- `transcribed`: whether it has utterances. Until the GPU host transcribes a conversation, and again whenever its audio
  changes, it has none: `transcribed` false, `file` and both revisions null, `utterances` 0.
- `taps`: the owner's start taps within 30 s of it, UTC, in order. End taps aren't listed; they split.
- `places`: the places the operator named that it happened at, in order, `[{name, start, end}]`, from the phone's
  location while it recorded (`hearsay/places.py`). Never coordinates; a spot nobody named is left out, and naming one
  later adds it to every past conversation there.
- `utterances`: how many lines the file has.
- `revision`, `transcript_revision`, `file`: see [Change](#change).

Taps and places are facts about the capture, not interpretations of it. Both are in the index, not the conversation
file, so they can change without `revision` changing.

## Utterances

One per turn: consecutive words from one diarized speaker, with no pause over 2 s (`hearsay/turns.py`).

- `conversation_id`
- `utterance_id`: `<conversation_id>:<index>`, the index four digits, e.g. `c20000101T000014Z:0007`.
- `start`, `end`: UTC, to the millisecond.
- `speaker {kind, name, label}`: see [Speakers](#speakers).
- `text`: from Parakeet TDT 0.6B v3, verbatim with fillers, in the language spoken (Spanish stays Spanish).
- `text_confidence`: each word's least likely token probability, averaged over the turn; about 0.93 on average. Null
  where no word has one.
- `speaker_confidence {basis, owner_similarity}`: how the speaker was decided, and the turn's cosine similarity to the
  owner's enrolled voice. `owner_similarity` is null where the turn wasn't embedded (too short, or too little received
  audio).
- `affect {arousal}`, owner turns only: how calm (low) or animated (high) the owner sounded, about 0–1, from the audio
  alone (`hearsay/affect.py`). Tone of voice, not sentiment. Absent where the turn wasn't scored: only the owner's turns
  by voice or channel, at least 2.5 s long, are. Other people's tone is never inferred.
- `delivery {words_per_second, pauses, pause_seconds, stutters}`, owner turns only: measured from the word timings, not
  judged (`hearsay/delivery.py`), on the same turns as affect; absent where not measured. `pause_seconds` is the mean
  pause, null with no pauses. Each stutter is `{kind, at, words_per_second_before, word}`: `kind` is `repeat`,
  `restart` or `filler`; `at` is UTC; `words_per_second_before` is null without enough words before it; `word` is its
  position in `text` split on whitespace (a repeat's first copy, a restart's fragment, the filler), so dropping those
  words gives the text without them, null where it couldn't be placed. Checked by ear, 20 of 24 stutters were real.

## Speakers

Since 2026-10-28 Hearsay says only whether the owner spoke. It names nobody else.

`kind` is one of:

- `owner`: the owner.
- `anonymous`: not the owner.
- `stranger`: not the owner, someone present, as the operator tagged it.
- `unknown`: Hearsay couldn't tell.
- `person`: a named person. Still valid in format 1, never written; a consumer should keep accepting it.

`name` is always null. `label` is null for the owner and unknown; for `anonymous` and `stranger` it tells voices apart
within one conversation by diarized speaker (`anon A`, `anon B`, `stranger A`), and means nothing across conversations.

`basis` says how the speaker was decided (`speaker_kind` and `speaker_from_label` in `hearsay/stream.py`):

- `voice`: the owner, by the turn's own voice (similarity to the enrolled voice clearly high). Only on `owner`.
- `cluster`: not the owner, by the turn's own voice (similarity clearly low). Only on `anonymous`. The name is from
  when such turns were grouped into people; it stays so the format doesn't change.
- `channel`: in a Mac recording, the turn was clearly louder on the mic (the owner) or on the call (someone else),
  which beats voice unless the voice clearly says otherwise.
- `diarization`: the turn was too short or too uncertain to decide itself, and took the label of its diarized speaker
  in the same conversation, whose turns agreed. On any kind, including turns the operator tagged.
- `named`: the operator tagged the turn `_me`, `_stranger` or `_mic` (below), and diarization or, for the last two, the channel
  didn't decide it. The name is from when the operator named people.
- `none`: on `unknown`, nothing decided it.

Checked by ear on 2026-10-28, on Hearsay's own turns: an owner turn by `voice` was right 49 of 49 times, one by
`diarization` 147 of 162 (the misses are short interjections).

The operator tags turns Hearsay couldn't decide, or got wrong, on the portal. A category tagged on one turn applies
whatever its voice said; tagged on a whole diarized speaker, only to that speaker's turns that weren't the owner's. The
categories (`hearsay/turns.py`):

- `_me`: the owner's (`owner`), whatever voice or channel said.
- `_stranger`: someone else present, `stranger`.
- `_mic`: someone else present, on a microphone or PA (a class, a talk, an MC), `stranger`. Tagged apart from
  `_stranger` so it can be treated differently later; in the stream the two are the same.
- `_noise`, `_media`, `_music`: not speech from anyone present. Left out of the stream, and back if the operator
  refiles them. That's Hearsay's call about who is speaking, not a content filter.

Hearsay also files a turn `_music` on its own when its sound says the words are a song's: much music, little speech,
and at least a second long (`hearsay/music.py`). The operator's category, or their answer of song or talk, wins.
Likewise `_media` for a turn that sounds like the media the operator has tagged, unless its own voice or channel said
the owner's (`hearsay/media.py`); the operator's category wins.

## Change

Everything can change after the fact: the operator's tags apply to past turns, a growing conversation is
re-transcribed, a new enrollment relabels the owner. So the unit of change is the conversation: a consumer re-reads a
conversation whose `revision` changed and replaces everything it had for it.

- `revision`: the first 16 hex characters of the sha256 of the conversation file's bytes, so a consumer can tell a read
  that raced a rewrite.
- `transcript_revision`: a hash of the conversation's turns as transcribed (times, index, text, diarized speaker,
  confidence), left-out turns included. Who a turn is attributed to never goes into it, so tagging or relabeling leaves
  it alone, as does filing a turn out of the stream or back. Within one `transcript_revision` a given `utterance_id`
  always names the same stretch of speech; across them, ids are renumbered.

Transcription is per conversation (`hearsay/transcribe.py`): a transcript is used only with the exact WAV and settings
it was made from. Any change to either transcribes the whole conversation again, with a new `transcript_revision`. The
WAV changes when the conversation grows or its audio or boundaries change; the settings when the model, the diarizer's
settings or the vocabulary change. Parakeet works through long conversations in pieces of at most 300 s, cut in
pauses; they're internal, don't show in the stream, and are never redone on their own.

## Vocabulary: Idea Machine → Hearsay

The one thing that flows the other way. Idea Machine writes the terms the operator approved in Lattice to
`/mnt/fast/ideamachine/vocab/vocab.json`, in its own folder, which Hearsay only reads (`hearsay/words.py`):

```json
{"format_version": 1, "terms": [{"term": "Corral", "source": "repair", "heard_as": "coral"}]}
```

Hearsay reads only `term`; `source` and the optional `heard_as` are Idea Machine's. A file with any other
`format_version`, or one that can't be read, gives no terms, and the portal's Words page says why. No file is no terms.

Nothing reaches transcription until the operator presses Update on the Words page, which commits these terms and the
words typed there, as they are then. Every Update changes the transcription settings, so every conversation not yet
sealed (those still held, the last two weeks) is transcribed again, with new `transcript_revision`s and renumbered ids;
older conversations change only when the operator unseals them.

## Forgetting

`forgotten.json` is a list, one entry per stretch the operator threw out:
`{forgotten_at, start, end, conversation_id, utterance_ids, reason}`. `forgotten_at` is when they threw it out;
`utterance_ids` are the utterances it removed from the transcript of the time, which a consumer may never have seen
(the conversation may not have been in the stream yet); `reason` is the operator's few words, or `null`. It is
append-only, never rewritten or shrunk. It is the only deletion signal: anything else that leaves the stream was
restructured, not deleted. Match by time, `start` to `end`: the ids are renumbered across transcript revisions.

Everything in a listed stretch stays out of the stream for good: every utterance that overlaps it at all, the taps
inside it, and a conversation that lies wholly inside it, whose index entry goes too. A conversation only partly
thrown out keeps its index entry, with its places; if none of its utterances are left, it has none, as if not yet
transcribed (`transcribed` false, no file). Delete what you hold from a stretch; don't wait for its
conversation's revision to change.

Throwing out is the operator's input, applied on every reprocess, so a re-run from raw never brings it back into the
stream. Hearsay deletes the stretch's audio and cuts its words from what it keeps once the conversation is sealed, two
weeks after it ends (raw audio only partly in the stretch goes 60 days after it ends); nothing of that changes the
stream.

## The hold

A conversation enters the stream two weeks after it ends, or sooner when the operator releases it (only one that has
ended can be; a release takes effect 5 minutes after it's made, and they can cancel it until then): until then it isn't
in the index at all, nor its file, taps or places. That's additive, so
`format_version` stays 1, but captures, self-notes too, arrive up to two weeks late. Once in, a conversation stays: the
stream never withdraws a conversation it has published, so whatever was in it before the hold began stays too, and a
conversation that takes in speech already published (late audio gives it a new id) isn't held either. Only
`forgotten.json` takes anything out.

## Guarantees

- `format_version` is an integer, now 1. Hearsay bumps it on any change to the index or utterance format that would
  break a consumer, and only then; a new field doesn't. A consumer refuses a version it
  doesn't know (Idea Machine does, in `read_index`).
- Files are written atomically (a temp file, then rename), conversation files first and `index.json` last, and only
  when their content changes.
- A conversation file exists only while its conversation is in the index and transcribed.

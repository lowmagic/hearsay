# Hearsay roadmap

Slices are done in order. Each one should be usable on its own before
the next one starts. Don't build ahead.

## 1. Webhook receiver

FastAPI endpoint behind Cloudflare Tunnel. Validates a shared secret and
writes every request body verbatim to disk with a timestamp and webhook
type. No parsing, no schema, no database.

Done when: real Omi webhooks (transcript, audio bytes, memory created)
are landing on the NAS, and we know which ones come from the phone vs.
Omi's cloud.

Retired 2026-10: Omi's last payload was 2026-09-30, after slice 9 replaced
its app. The receiver and the tunnel are gone; the raw payloads stay and are
still parsed. Nothing of Hearsay's is on the public internet now. In the
same change Hearsay moved to the NAS's NVMe pool, and reprocess stopped
rewriting unchanged audio and the whole database every hour. Idea
Machine moved too; the portal reads its vocab from `/mnt/fast/ideamachine`.

## 2. Parse from reality

Write parsers and a schema from the captured payloads, not from Omi's
docs. Captured payloads become test fixtures. Store parsed records in
SQLite. Reprocessing from raw payloads must produce the same result.

Done when: every captured payload type parses, and a full reprocess
from raw is one command.

## 3. Audio assembly

Stitch audio-byte chunks into per-conversation audio files. Handle
gaps, out-of-order chunks, and duplicate deliveries.

Done when: each conversation has one playable audio file that lines up
with its transcript timestamps.

## 4. Me vs. not-me

Enroll the owner's voice. Compute speaker embeddings per diarized
segment and label each as owner or not-owner with a tunable threshold.
Tune for precision; ambiguous segments stay unlabeled.

Done when: owner speech is labeled reliably on real conversations, and
misses are rare enough to ignore.

## 5. Anonymous speakers and tagging

Cluster non-owner embeddings into anonymous speakers. Support tagging a
cluster with a name and merging clusters. Tags and merges apply
retroactively to all past utterances.

Done when: tagging a speaker once relabels their past conversations.
Revised 2026-10-28, me / not me: Hearsay names nobody but the owner.
Naming people and the slices built on it (14, 15 and 18) are shelved and
their code is gone; names already given stay in labels/tags.jsonl, unread.
The portal plays the turns Hearsay couldn't decide, and the operator tags
each Me, Someone else, or not speech. Checked by ear the same day on
Hearsay's own turns: an owner turn by voice was right 49 of 49 times, one by
diarization 147 of 162 (the misses are short interjections). Idea Machine's
M2 validates me / not me with these.

Owner threshold lowered from 0.40 to 0.30 on 2026-11-06, labeling about 55
turns between 0.30 and 0.40 blind (`hearsay-label --between`): of 152 labeled
turns with scores, owner at 0.30 is 0.99 precise (one miss, a turn mostly
someone else's that diarization merged with the owner's "um") and finds 0.95
of the owner's turns, against 0.47 at 0.40. Not-owner stays at 0.14: after
about 40 more turns were labeled between 0.14 and 0.30, the owner was already
3 of 14 turns from 0.14 to 0.18 (4 of 13 to 0.22, 8 of 12 to 0.26, 12 of 14
to 0.30), so voice can't decide there; diarization and the portal do.


## 6. Local transcription (WhisperX)

Transcribe and diarize each conversation's assembled audio with WhisperX
on the dev box GPU (inside the boundary), replacing Omi's text and speaker
turns. Word timestamps come from our own audio, so alignment is exact by
construction. Speaker labeling and clustering run on WhisperX's turns.
Carry existing ear labels and names across (e.g. by time overlap with the
Omi segments they were made on), so no operator input is lost.

Done when: every conversation's utterances come from our own transcription,
and past labels and names still apply.

Follow-up 2026-10-06: Parakeet TDT 0.6B v3 (NeMo) replaced Whisper large-v3
for the words, with pyannote's diarization run directly. On a real stretch
Whisper had dropped 30 s of speech and repeated one line four times over the
gap. In a blind listening check of 24 turns Parakeet Ultra (the same model
post-trained) was closer where it and Whisper disagreed most (7 to 3), and
wrote Spanish where Whisper, told English, translated it; it skipped some
short interjections Whisper caught. Parakeet detects the language itself and
can't be told to keep to English and Spanish; its rare guesses at other
languages (12 of 5,300 turns) were over noise both models got wrong.
`text_confidence` in the stream changed scale with it (words average about
0.93).
Long conversations are transcribed in pieces of at most 5 minutes, cut in
pauses.

## 7. Own conversation boundaries

Split the continuous audio stream into conversations ourselves (speech
and silence), instead of waiting for Omi's memory payloads. After this,
Omi is only a pipe for audio: its transcript and memory webhooks are no
longer needed.

Done when: conversations are found without any Omi transcript or memory
payload, including ones Omi never sent.

## 8. Utterance stream

Expose the utterance stream (conversation_id, timestamps, speaker, text,
confidence) for downstream consumers. Idea Machine is the first one.
Comes after slices 6 and 7 because both change where every field comes
from, and conversation ids change with slice 7.

Done when: a downstream consumer reads utterances without touching audio
or Hearsay's internals.

Amended for Idea Machine (after slice 10's first commits): `format_version`
in the index, a per-conversation `transcript_revision` that only a new
transcript changes, an append-only `forgotten.json` (empty until slice 12),
and atomic writes and `revision` stated as guarantees.

## 9. iPhone pendant app

A pared-down replacement for Omi's app: pendant over BLE, straight to
Hearsay's capture receiver over the tailnet. No Omi cloud, no upsells.
Replaces the webhook path. Native Swift (`ios/`), starting from a Mac
pendant recorder that proved the protocol, the spool and the upload format;
it was removed once the phone took over (the pendant belongs to the phone).
Protocol details come from Omi's open-source firmware (MIT).

- Nothing lost offline: the phone spools everything verbatim with arrival
  times and uploads whenever the NAS is reachable; audio the pendant stored
  while the phone was out of range is downloaded from it afterwards.
- Pause/resume monitoring. Paused audio never reaches the NAS. A switch
  decides whether it is dropped at once or kept on the phone only, marked
  paused, for a retention period (30 days to start) during which a paused
  window can be kept (uploaded like normal audio) before it is deleted.
  A window can be played on the phone and cropped to one selection before
  upload; the rest of it is deleted.
- Mute: the pendant's mic gain set to 0, which mutes it in hardware and
  holds out of range and across app crashes until unmuted (which restores
  the previous level). While muted the app drops the pendant's audio, live
  and stored, rather than upload silence; stored audio counts as muted if
  the app was muted at any time while the pendant was away.
- Optional conversation markers: start (keep what follows even if short,
  e.g. self-notes) and end (force a split).
- Configurable actions for the pendant's single and double tap (the
  consumer firmware reports no other gesture: holds go unreported, and 3 s
  powers it off), each action with its own haptic pattern. The firmware's
  haptic characteristic plays 100/300/500 ms buzzes; patterns are sequences
  of them.
- Server URL and token are settings, not hardwired, so the app isn't tied to
  one Hearsay install.
- Signing: a free Apple ID while building (installs expire after 7 days),
  the paid developer program once it's in daily use. Personal installs need
  no App Review; publishing is a separate decision for later.

Done when: a day of capture reaches Hearsay from the phone with Omi's cloud
out of the loop, including a stretch out of range and a paused stretch.

Follow-up 2026-10-25, stored audio: of 277 minutes the pendant stored while
away, only 139 were placed. Its packet count restarted from 0 on 10-07 and
10-11, and placed in count order a later away stretch made every earlier one
seem to step back, so audio placed before 10-11 left the timeline; and an
app iOS ended while the pendant was away dated the stretch from its relaunch,
hours late. Packets now go in download order, an away stretch starts when the
pendant was last heard live, and the app saves when that was. 55 minutes
from 10-07, where the pendant's clock was decades off, stay unplaced.

Revised 2026-11-09, pause and mute: unpaused and muted had no use, and
paused while muted kept nothing, so the app is on, paused or off (the mic
off in hardware, nothing kept). Paused audio is always kept on the phone,
deleted a set number of days after the pause ends (1 to 60, 30 to start).
Switching from paused to on mid-conversation sends the conversation from
where it began: the phone runs Apple's on-device speech classifier
(SoundAnalysis, 3 s windows) back through the paused window, 10 minutes at a
time and at most 2 hours, to a 3-minute gap without speech, and sends from
10 s before the speech after it; the rest stays to review. On a synthetic
window on the Mac it placed a conversation's start within 3 s, analysing 40
minutes in 4 s. A NAS-side search with Silero would match the NAS's own
boundaries, but paused audio would reach the NAS, and raw payloads can't be
cut down.

## 10. Every turn attributed

Turns too short to embed (under 2.5 s), or scoring between the owner and
not-owner thresholds, have no speaker: 18% of speech time on 2026-10-01.
Within a conversation the diarizer already groups each turn with a speaker,
and that speaker's voice-labeled turns agree almost always (51 of 53 at 90%
or more). An unlabeled turn takes its diarized speaker's label (owner or not,
and through the speaker's cluster, the person's name) when that speaker's
labeled turns agree, unless the turn sounds more like the owner than like
that speaker (the diarizer's usual slip is filing a short "yeah" under the
other person); the stream says the basis was diarization. Checked by ear on
a random sample of inherited turns (`hearsay-label check`). The first 155
checks were 86% right, 91% with the veto; the misses are nearly all
half-second interjections, which rarely carry anything.

Done when: under 5% of speech time has no speaker, and inherited labels hold
up by ear at 90% precision or better on a sample checked after the veto.

Done 2026-10-01: 3.2% of speech time has no speaker (voice 82%,
diarization 15%), and 278 inherited turns checked by ear after the veto are
96.4% right (96.2% by speech time).

Follow-up 2026-10-01, clusters: two clusters mixed named people (two women
bridged by untagged speakers; a man joined to a speaker the diarizer had
lumped several people into); the diarizer gave two men one label in two
conversations, and four people at a noisy dinner one label; and a `_noise`
cluster seeded by two tagged turns grew to 794, taking a real person's 32
minutes out of the stream. Names are now constraints (different names never
share a cluster), a category takes only turns tagged with it, and a diarized
speaker whose turns form several voices (1+ minute each) is split into them,
marked or not ("More than one person" forces at least two). No speaker of 3+
minutes that was one person split (26, the owner's and every named
person's); 3 of 4 known lumps did. A split-off voice not yet named joins no
named person, so it comes back to be named: on phone recordings in noisy
rooms different people score 0.4-0.6, and split voices otherwise joined the
wrong people. Replayed on the live data with no new marks: no conflicts, the
dinner came out as three voices, and the second man as his own cluster.

Follow-up 2026-10-05, diarization: the lumps start in the diarizer, so it now
splits more readily (VBx `Fb` 0.6, from the model's 0.8; `SETTINGS` in
`hearsay/transcribe.py`). Re-diarized 18 conversations at 13 settings and
scored against the operator's names and labels: tagged speech under the
wrong name fell from 49 s to 9 s, owner speech sharing a speaker with others
from 260 s to 126 s, and speakers where voices() still hears several from 4
to 1, for 63 to 78 speakers and no clean conversation split further. The
dinner's two speakers became the owner plus three voices, and Grace came
apart from Heidi and the strangers. Still lumped: Ivan and Judy in
c20000101T000004Z, and c20000101T000005Z (four voices under one label). Every
conversation is re-transcribed with the new setting.

## 11. Mac audio capture

A menu-bar app on the operator's personal Mac (no pendant) that captures the
mic (including AirPods) and system audio as separate channels: run/stop from
the menu bar, and a prompt to record when a Zoom call starts. Uploaded like pendant
audio. Speech on the mic channel is the owner's, by channel; during a call
the call recording wins over the pendant's. Recording calls can require
everyone's consent depending on jurisdiction, so the app asks rather than
starting on its own: a call is recorded only once the operator says yes.

Done when: a Zoom call's utterances reach the stream with the owner
attributed by channel, recorded after the app's prompt.

Done 2026-10-06: test calls on built-in speakers and AirPods, each recorded
after the prompt, reached the stream (c20000101T000008Z) with the owner by
channel and the other person never attributed to the owner. Measured per
speech stretch, the owner alone is 46-67 dB louder on the mic than on the
call, and the other side on AirPods 21-41 dB quieter, so the 12 dB
`CHANNEL_RATIO` holds. On built-in speakers the mic hears the other side
through them, 3-26 dB louder than on the call, which would pass for the
owner; a turn the voice clearly places is now labeled by voice instead
(`with_channel`). A short turn on speakers still follows the channel. With
AirPods the tap first recorded the AirPods' own input instead of Zoom (fixed
in `mac/`); calls under 30 s of speech are dropped like any conversation.

## 12. Retention

Held 2026-10-28 until Idea Machine's M2 validates me / not me: raw audio is
the rollback for its re-link of segments. Lifted 2026-11-01: M2 validated,
and nothing in Idea Machine needs the audio.

Delete non-owner audio after embedding and transcription. Starts only
once the models are tuned (diarization, voice thresholds, clustering): until
then all audio is kept, because deleted audio can't be re-embedded or
re-transcribed. Decided 2026-10-06: then delete audio older than a rolling
window, 90 days to start, revised down later. First resolve how this fits
"raw payloads are never deleted" and "everything re-runs from raw".

Forgetting comes first, in slice 20: the operator throws out a stretch,
which leaves the stream on every reprocess and is appended to
`forgotten.json`. Its raw payloads and audio are deleted here, with
retention, and not held for the window.

Done when: non-owner audio is actually gone from disk, a thrown-out
stretch's audio is gone too, and nothing that depends on either breaks.

Resolved 2026-10-25: retention waits for slices 20 and 19, not for the
models. Slice 19 keeps what audio produced, so only the audio-dependent
models (transcription, diarization, speaker embeddings) need tuning before
it goes; who is speaking re-runs from stored vectors for good. Then all
audio past the window is deleted, the owner's too, except the enrollment
windows' (a few minutes, so the owner can be enrolled in a new speaker
model). The invariants on raw payloads and re-running from raw change with it.

Decided 2026-11-05, with the operator:

- One boundary for sealing (slice 19), the hold (slice 20) and discarding
  thrown-out audio: 14 days after a conversation ends to start, 7 once
  reviewing is routine. Sealing moved from 7 days to the hold, so a Words
  update made while reviewing reaches the conversation being read, and what
  Idea Machine gets normally has its final transcript (extracted once).
  Sealing comes first in a run, so the record holds every turn before any
  audio goes. Late uploads don't hold the boundary back: of 3,830 phone
  uploads none arrived more than 24 h after its audio began (173 after
  6 h), and late audio unseals by itself.
- Thrown-out stretches, at the boundary: silenced in the conversation's WAV
  then, not before (a WAV changed before sealing would be transcribed
  again), raw uploads and imports lying wholly inside a stretch are
  deleted, and its words and vectors leave the conversation's record and
  the dev box's transcript file. Uploads that only reach into a stretch stay
  until retention, so an unseal within the window still has the audio
  around it; their thrown-out audio (up to about a minute at each edge)
  never reaches the stream. Raw is deleted whole, never edited. Deleting
  every upload a stretch touches would lose neighbouring speech on unseal;
  keeping sealed conversations with discarded audio out of unseal needs
  per-conversation sealing, a later step if edge audio surviving to
  retention is too much.
- Retention: audio 60 days on (raw uploads and imports 60 days after their
  own audio ends, the rest after its conversation does): raw uploads,
  imports, conversation WAVs and decoded audio, the owner's too, except raw
  covering an enrollment window, kept so the owner can be enrolled in a new
  speaker model. Records keep every turn's words, vectors and scores
  whatever it's tagged, so `_media`, `_music` and `_noise` tags keep
  training the hints. Unsealing reaches back 60 days at most; an import
  older than that is refused rather than unsealing to it.
- The dev box's transcript files stay until retention, cut like the record
  where something is thrown out, and go 60 days after their conversation
  ends: `records check` compares a build from raw with them, and an unseal
  within the window reads them again (revised 2026-11-05 from going once
  sealed). One whose conversation no longer exists (boundaries moved during
  the hold, or Omi's UUID-named ones) goes on any run.
- Backups: raw, audio, imports, decoded audio (moved out of `db` to its own
  dataset) and transcripts get their own daily snapshots kept 7 days,
  replicated at 7 days; input audio isn't precious. Records, the database,
  labels and the stream keep the NAS-wide 2 weeks, replicated for 2 months.
- No tuning has to come first: thresholds and the media and music rules run
  on stored vectors and can be retuned at any time. Only a new
  transcription, diarization or speaker model is limited to the window.
  Owner threshold lowered from 0.40 to 0.30 on 2026-11-06 (slice 5).
- Words cut from a record for a stretch thrown out after sealing stay in
  the records' and database's snapshots up to 2 months: accepted by the
  operator, since the audio is what mustn't outlive its window.

Discarding and retention, built 2026-11-05 (`hearsay/retention.py`), run
at the end of every reprocess, after sealing. A word cut from a record is
blanked, keeping its timing and speaker, which are all turns are cut by,
so the turns around it are built as before. Each deleted file is logged in
`records/deleted.jsonl` first, and a deleted upload's parsed rows (taps,
locations) load from its digest wherever the seal line moves. Raw uploads
and imports go 60 days after their own audio ends; a conversation's WAV and
transcripts 60 days after it ends. Where a run
of the pendant's audio went on across uploads deleted from a stretch, what
follows can be placed up to a second or two differently after an unseal,
inside the conversation that is transcribed again then anyway. `records
check` takes records in both builds up to the last conversation audio was
deleted or cut from, as raw can't rebuild those. The model cache drops
entries no run has used for two weeks. `python -m hearsay.retention report`
prints what the next run would delete, changing nothing.

Built in order, each usable on its own: the boundary; discarding thrown-out
audio and cutting it from records; retention with its guards; the datasets
and snapshots (`install/nas.sh`).

The datasets and snapshots, built 2026-11-05: decoded audio moved from `db`
to its own dataset, `pcm`. `raw`, `audio`, `imports`, `pcm` and
`transcripts` have their own snapshot tasks (daily, 7 days, schema
`hearsay-%Y-%m-%d_%H-%M`) and a replication to
`storage/hearsay-audio-replica` kept as long, and are excluded from the
NAS-wide tasks. A task's retention only prunes datasets it covers, so the
NAS-wide tasks would have kept what they already had of them forever:
`nas.sh` fixes the local snapshots' removal dates when it adds the excludes,
then lists those snapshots and the old replica datasets and destroys them
once the operator confirms. `db`'s snapshots still hold the decoded audio
cached there before; they age out (2 weeks local, 2 months replicated).

## 13. Where a conversation happened

The phone app records where the owner is while it captures, and each
conversation in the stream says where it happened, for downstream context.
Like taps, a fact about the capture, not an interpretation of it. The phone
is the source: it is with the owner wherever the pendant is, and the Mac's
location says little. Uploaded with the capture to the same tailnet-only
receiver, kept with the raw payloads, and re-run from raw like everything
else.

Location is as personal as the audio, but unlike audio it reaches
consumers. Decided 2026-10-06:

- The phone samples while it records, that is while neither paused nor
  muted: once when recording starts, then every 2 minutes, at about 100 m
  accuracy (Wi-Fi and cell, cheap on battery). Each reading (latitude,
  longitude, the accuracy iOS reports) is a record in the capture upload.
- The stream carries only places the operator has named ("Back Room"),
  never coordinates. A reading is at the nearest named place within 100 m.
- A conversation lists the named places it happened at, in order, each with
  when it started and ended there, so one that moves has several. Unnamed
  spots are left out.
- Places are named in the portal, on a page of spots where the owner spent
  time and no named place is near, each with how long, on which days, who
  was heard there, and a map link. Names are the operator's durable input
  (labels/places.jsonl), so naming is retroactive, like people's.

Done when: a day of conversations reaches the stream each with where it
happened, and the phone's battery use for it is acceptable.

Done 2026-10-07: a night and a morning of conversations reached the stream
at the operator's two named places (Blind Monk, Home), 1,085 of 1,096
readings matched one, and no file in the stream holds a coordinate. Readings
averaged 8 m accuracy (worst 195 m), sharper than the 100 m asked for. Battery
use was acceptable at one reading a minute; sampling then went to every 2
minutes, which loses nothing a conversation's places need.

Revised 2026-10-13, to spend battery only when the owner moves: a precise
fix when recording starts or resumes and about every 5 minutes on the move;
staying put (two fixes at least 2 minutes apart agreeing within their
accuracy, no confident motion between) makes an anchor from the sharpest
fix, with its reported accuracy, and fixes stop. The geofence radius stays
separate. Core Motion's activity (still, walking, cycling, driving) asks for
fixes sooner or later; a geofence, sustained motion, the car connecting or a
fix every 30 minutes suspects leaving. Departure needs two fixes at least
a minute apart, each accurate to 20 m or better and outside the anchor by
more than its own and the anchor fix's combined uncertainty. Motion and the
geofence never bypass confirmation. Sustained-motion checks double their
interval from 5 minutes to at most 30 minutes after unsuccessful checks.
After driving, settling takes 10 minutes unless the owner walks
or the car's Bluetooth disconnects, so traffic doesn't settle. The car is
whichever Bluetooth audio output the owner picks in the app, read from the
audio route without touching it; it suggests the car is near, never that
it moves. Settled, the app says every 5 minutes it's still at its anchor,
including the fix accuracy separately from the geofence radius, and the
NAS reads that as a reading. Activity, route changes and every
state, with its confidence and reason, are records in the upload, so trips
can be reconstructed from raw later. AirTags were left out: Find My has no
public API. The state machine is ios/Hearsay/Movement.swift, tested on its
own (`swift test` in ios/). Paused, it
keeps tracking into the paused window, so its records reach the NAS only
with the stretches the owner sends. Walking only delays settling: precise fixes that
keep agreeing for 10 minutes settle anyway (at lower confidence), so a
party or a yoga class is a stay; not after driving, so a jam isn't.

Next, in the app: a Places map (Apple's MapKit, so places go to no one new)
where the owner drags a pin to where a place really is. The portal serves it
with the capture token as a bearer on each request: GET /places.json lists
every named spot ({name, latitude, longitude}; a name can have several) and
the spots to name ({latitude, longitude, minutes}). POST /places, form
fields as the portal's page: action=move, from=name and one at="lat,lon" per
spot the place should have (all of its spots, the dragged one moved);
action=name with name, latitude, longitude; action=ignore with the spot's
latitude and longitude as listed. The web page's clips stay the way to hear
a spot. Built 2026-10-13 as the app's Places tab (ios/Hearsay/PlacesView.swift).

A radius per named spot, for places bigger or smaller than 100 m (a
festival ground, a shop next door to another): operator input in
labels/places.jsonl like the name ("radius", metres, 25 to 2000, absent for
the default), used by hearsay/places.py in place of the fixed RADIUS. The
NAS side is built: /places.json gives each named spot's radius, and POST
/places takes one radius per at on a move (in the same order; one left out
keeps the place's) and a radius on a name. In the app (2026-10-13), each
named spot's area is a faint orange circle (MapKit's MapCircle); selecting
its pin shows a slider (logarithmic, 25 to 2000 m, in 5 m steps), and Save
sends the place's move with every spot's at and radius. Tapping a spot to
name lists the conversations recorded there (up to 10, latest first: when,
the named people heard, and the first two turns' words, from /places.json),
to tell from what was said where it is; still no audio in the app.

Wrong readings, corrected by time (2026-10-13), since a wrong fix can sit
inside a real place's circle now or once it grows: labels/places.jsonl
takes {"type": "drop", start, end} (those readings count for no place) and
{"type": "at", start, end, name} (they were at that place wherever they
say), applied on every reprocess, raw untouched. Fixes iOS reports as
worse than 300 m are left out too. New stay records carry the anchor fix's
accuracy; older ones carry only the geofence radius, with no way to recover
the original accuracy. The app offers both corrections on each conversation it lists
for a place or a spot.

## 14. Place as a hint for who is speaking

Shelved 2026-10-28 with naming people (slice 5), its code removed. What was
built and measured is in [the roadmap as it was](https://github.com/junglestyle/hearsay/blob/068cf870af04516a53421b14e5133dbbdadddb38/docs/roadmap.md#14-place-as-a-hint-for-who-is-speaking).

## 15. Media hint

Shelved 2026-10-28 with naming people (slice 5), its code removed. What was
measured (sound-event models and speaker embeddings on media) is in
[the roadmap as it was](https://github.com/junglestyle/hearsay/blob/068cf870af04516a53421b14e5133dbbdadddb38/docs/roadmap.md#15-media-hint).

## 16. The owner's tone of voice

How the owner sounded, per turn: arousal, valence and dominance from a
dimensional speech emotion model run locally (audeering's wav2vec2 tuned on
MSP-Podcast, natural speech rather than acted). Like `owner_similarity`, a
property of the audio, not of what was said, and since audio never leaves
Hearsay, no consumer could compute it. Text sentiment stays downstream.

The idea comes from Manfred Clynes' sentics: an emotion has a
characteristic shape in time. His sentograph measurements don't carry over
to voice, so the shape is a later question (does the owner's arousal
contour across a conversation say more than each turn's number?), not the
method.

- Owner turns only, at least `MIN_TURN` long with `MIN_COVERAGE` of real
  audio, cut the way speaker embeddings are. Other people's emotional state
  is not inferred: they didn't agree to it, and one known voice gives a
  real baseline.
- Run after speaker labels in reprocess, cached by audio content. On the
  NAS CPU first; on the dev box GPU only if the hourly run gets slow.
- The stream gains an optional `affect` {arousal, valence, dominance} on
  owner utterances. Additive, so `format_version` stays 1; tell Idea
  Machine.
- Arousal from voice is usually reliable; valence much less so. Checked
  blind by ear like inherited labels: the operator rates a random sample of
  turns calm, neutral or heated without seeing the scores.

Done when: owner turns in the stream carry affect, and arousal agrees with
the operator's blind ratings well enough to be worth showing downstream. If
it doesn't, the slice stops there.

Checked 2026-10-07, before building: the operator rated 30 of their own
turns calm, neutral or heated without seeing scores, and arousal ranked them
alike (Spearman 0.74; in 62 of 63 calm-heated pairs the heated turn scored
higher). Dominance follows arousal for every voice measured (r 0.91-0.97 for
the owner and four others, so the model's, not the owner's), and was left
out of the stream: calm assertiveness, which it can't see, would need direct
prosody measures (falling versus rising endings, rate, fillers) instead.
Valence is computed but waits for two checks: whether it holds when the
words are muffled away (low-pass filtered audio), and a blind rating of
pleasant to unpleasant, ideally by someone other than the owner, since the
point is how others hear them.

Valence, 2026-10-08: scored on the owner's 928 turns as is and low-pass
filtered at 400 Hz (pitch, rhythm and loudness kept, words unintelligible),
its ranking held 0.24, against arousal's 0.47 (the baseline: muffling is
far from the clear speech the model learned on), and its part beyond
arousal held 0.17. Most of what this model calls valence comes from the
words or from energy, not from tone, so it stays out of the stream.

## 17. How the owner holds a turn

The owner wants to learn calm assertiveness: sounding settled and sure
without raising energy. Slice 16's dominance can't see it (it follows
arousal for every voice measured), so this measures the delivery directly,
in terms the owner can act on. Like affect, properties of the owner's audio
and timing, owner turns only, never other people's.

Candidate measures, per owner turn:

- Ending: whether the pitch falls or rises over the turn's last half second
  (a statement that rises sounds like a question). A pitch tracker on the
  turn's audio; no words needed.
- Rate: words per second of speaking time, from Parakeet's word timings,
  pauses left out.
- Pauses: silent gaps over 250 ms between words, how many and how long, and
  whether the turn trails off (its last word much quieter than the rest)
  rather than ending firmly.
- Disfluency: stutter events with their times, each with the speaking rate
  in the seconds before it, since the owner's stutters come from speaking
  quickly. Repeated words and restarts from the word timings (the same word
  twice a fraction of a second apart, a word cut off and begun again),
  fillers ("uh", "um") from the verbatim text, and perhaps sound
  repetitions, prolongations and blocks heard in the audio by a model
  trained on stuttering (SEP-28k), since Parakeet tidies many of those
  away. It stays in Hearsay because it needs the timings and the audio,
  which consumers never get, and it reads the form of the owner's speech,
  not what they meant.

Measured first, like affect. For ending, rate and pauses, the operator rates
a blind sample of their calm turns (low arousal) as assertive, neutral or
tentative, and each measure is checked against those ratings. For
disfluency, the operator listens to detected events and marks each a real
stutter or not, so each detector's precision is known. Only what holds up
reaches the stream, as an optional `delivery` on owner utterances; additive,
so `format_version` stays 1. What to do with it (trends, feedback) is
downstream.

Done when: the measures and detectors that hold up against the operator's
ears are in the stream, at least one of them. If none does, the slice stops
there.

Measured 2026-10-09 to 10-12 on the owner's ~1,100 turns. Ending pitch,
rate, pauses and trailing off are mostly independent of arousal (rate
r 0.41, the rest about 0), but none matched the owner's blind
assertive/neutral/tentative ratings of 29 calm turns (Spearman -0.23 to
+0.06, all within chance), so there is no assertiveness label; that would
need ratings by someone else to settle. Stutters held up: of 24 detections
the owner judged by ear, 20 were real (repeats 11/13, restarts 5/6, fillers
4/5); doubled answer words ("yeah, yeah") are left out. Per word, above
about 3.3 words/s the owner repeats and restarts 30-50% more than below it
and uses fewer fillers; the speed-up in the 3 s right before a stutter is
barely above chance against random words (53%), so it is pace across a
turn, not a sudden rush. In the stream: `delivery` on owner utterances, with
rate, pauses and stutters, each stutter with the rate before it; ending pitch
left out (the tracker is noisy at word ends).

## 18. Review that shows what was said, and gates new matches

Shelved 2026-10-28 with naming people (slice 5), its code removed. Clips
with their words, time and place (2026-10-25) carried over to the portal's
turns Hearsay couldn't decide. The rest is in [the roadmap as it was](https://github.com/junglestyle/hearsay/blob/068cf870af04516a53421b14e5133dbbdadddb38/docs/roadmap.md#18-review-that-shows-what-was-said-and-gates-new-matches).

## 19. Turns kept without their audio

Everything is rebuilt from raw audio every hour, so no audio can go (slice
12). Instead keep, per turn, what its audio produced: times, words and their
timings, diarized speaker, speaker embedding (with the model that made it),
owner similarity, affect, delivery and music scores. About 11 MB for
14,000 turns, against 9 GB of audio and decoded audio. Reprocess reads audio
only for conversations that are new or still growing, and rebuilds the rest from
these records and the operator's labels. Who is speaking (thresholds,
clustering, voiceprints, names) still re-runs on every turn ever recorded; what came out of
the audio is fixed once its audio is gone. A new speaker model applies only
to audio still kept, so changing models is a deliberate migration.

Done when: a rebuild from the records equals a rebuild from raw, and a
reprocess with nothing new reads no audio.

Decided 2026-11-02 (`hearsay/records.py`):

- A conversation seals a week after it ends, once transcribed and every one
  before it sealed, so a seal line runs through the timeline: behind it
  everything comes from records, past it from raw. A week, not the two-week
  hold, so a reprocess reads only the last week's audio; sealing doesn't
  touch the hold or throwing out, which act on turns after they're loaded.
- A record keeps what the audio gave, not what was decided from it: the
  conversation's boundaries and audio facts, the transcript as the dev box
  wrote it, and per turn its coverage, embedding, the short embedding for
  the veto (now made whether or not the owner is enrolled), the Mac channel
  that was louder, and the music and tone scores. An upload behind the line
  keeps the rows parsing made from it except its audio (marks, locations),
  and the owner's enrollment pieces are kept embedded, so neither is read
  again.
- Re-running sealed conversations from audio is deliberate: a Words update
  or a new model reaches only unsealed ones, and `python -m
  hearsay.records unseal <date>` moves the line back while the audio is
  kept. An import or late pendant audio behind the line (or less than GAP
  past it, where its speech joins the last sealed conversation), or an
  enrollment window added behind it, unseals back to it by itself.
- Tone stays the owner's only (hearsay/affect.py: others' emotional state
  isn't inferred), so a sealed turn that becomes the owner's later, by a new
  enrollment or threshold, has no tone score.
- Thrown-out words stay in a record's transcript until slice 12 cuts them,
  once the conversation is sealed (hearsay/retention.py); on every reprocess
  they're removed after loading, as from raw.
- Sealing also lets reprocess drop the decoded audio behind the line from
  its cache (`db/capture-pcm`, `db/imports-pcm`), which is most of the 6 GB
  there.

Done 2026-11-04: `python -m hearsay.records check` on the NAS said a build
from records is the same as one from raw. The line was at 2026-10-28 05:39,
with 72 of 85 conversations (15,038 of 16,511 turns) sealed; the build from
records read 768 uploads (9 hours) against 3,462 (42 hours) and no imports,
and wrote no WAVs. "Reads no audio" holds behind the line: the last week's
audio is still read on every reprocess, as decided above.

## 20. Throw out what was said, and hold the stream two weeks

The operator can't take anything back once it's said near the pendant: it
reaches the stream within the hour, and Idea Machine sends it to Claude.
Instead, nothing reaches consumers for two weeks, and in that time the
operator can read and hear a whole conversation and throw out parts of it.
Decided 2026-10-26:

- A conversation page in the portal: the transcript, each turn with who
  said it, when, and at which named place, and audio that plays from any
  turn on through the conversation. A list of conversations, newest first,
  says which are still held and for how long. It narrows to a named place,
  and to the conversations whose words contain a search, each with the
  turns that matched, marked on its page too.
- The operator marks a stretch, from one turn to another (whole turns to
  start; word timings would allow trimming inside a turn later), or a
  whole conversation, and throws it out with an optional reason. A mark is
  the operator's durable input, by absolute time like names
  (labels/forget.jsonl), so it survives re-transcription and applies on
  every reprocess: its turns leave the stream, their words leave the
  database, and they count for nothing (clusters, hints, affect,
  delivery). The stretch is appended to `forgotten.json`. A mark can be
  taken back until the next reprocess applies it; after that it is final,
  since `forgotten.json` never shrinks.
- The hold: a conversation enters the stream (its index entry, file, taps
  and places) two weeks after it ends, or sooner when the operator releases
  it from its page. Until then consumers don't see it at all. Additive, so
  `format_version` stays 1; Idea Machine's captures, self-notes too, arrive
  two weeks late unless released. Two weeks to start, for ample time to
  review; likely shortened once reviewing is routine. A month with early
  release was the alternative: releasing early is the same either way, and
  a longer hold only delays what the operator doesn't get to.
- The audio, raw payloads and transcription output of a thrown-out stretch
  are kept until slice 12 deletes them, once its conversation is sealed.
  Gone means every copy: raw payloads, conversation WAVs, the decoded audio
  and embedding cache, the transcription output, and the NAS's nightly
  snapshots once they expire.

Comes before 19 and 12: it needs neither, and it is the only way to keep
something from Claude until then.

The conversation page and its list come first, read-only (`/conversations`
in the portal, and a day's conversations read through in order at `/day`): reading and hearing new capture on them is also how slice
22's done-when is checked. Throwing out came next (`hearsay/forget.py`):
a stretch from the first turn checked to the last, or the whole
conversation, leaves on the next reprocess; every turn it overlaps at all
goes, since keeping words thrown out is worse than losing a neighbour's.
The hold came last (`hearsay/hold.py`): the stream never withdraws a
conversation it has published, since Idea Machine retires speech that
leaves the index, so what it had before the hold stayed. Only a
conversation that has ended can be released, or what's said next would go
unread. The portal reads the stream's index to say whether Idea Machine
can see each conversation.

Done when: the operator reviews two weeks of conversations on the page,
thrown-out stretches never reach Idea Machine, and the rest arrives on
release or after the hold.

Changed 2026-11-04, from the operator's review: Throw out checked takes
the turns checked, each run of them one mark, not everything from the first
to the last, so All not me and All media can check the turns Hearsay is
sure of and leave the owner's between them. Turns it isn't sure of are
marked yellow and left unchecked, since throwing out someone talking is
worse than keeping some TV: those it couldn't tell who said (2,763 turns,
60 minutes), media scores 0.5 to the hint (211, 35 minutes), and turns not
the owner's only by their speaker whose own voice is closer to the owner's
than the not-owner threshold (158 of 3,750). Check between fills in a
stretch.

Changed 2026-11-07, from the operator's use: a release takes effect 5
minutes after it's pressed, and until then its button is Cancel release.
One pressed by mistake reached Idea Machine 22 seconds later, because a
reprocess already running read it; now a reprocess counts only releases
already due, and each release enqueues one held until it is. Release asks
on a page of its own: the browser's confirm dialog it used didn't come up.

## 21. TV and other media, per turn

TV and video dialogue is most of what reaches the stream that isn't anyone
present: 100 minutes tagged `_media` by 2026-10-29. Until it's tagged it
reads as other people's speech, and the portal only offered turns Hearsay
couldn't decide, so TV it had filed as not the owner couldn't be tagged at
all. Slice 15's media hint, shelved with naming, offered `_media` on
clusters of people; this one scores single turns (`hearsay/media.py`).

- On every reprocess a logistic regression on speaker embeddings learns
  from the operator's input: `_media` against someone present (a name the
  operator gave, `_stranger`, `_me`, the owner by ear).
- The portal lists turns at the hint or above, unless their own voice or
  channel said the owner's, above the turns Hearsay couldn't decide, with
  the same tags. Nothing leaves the stream until the operator tags it.

Done when: new TV is tagged from the list as it comes, and what it offers
is rarely tagged anything but Media. Then decide whether turns at the hint
leave the stream untagged.

Measured 2026-10-29, leave one conversation out, per turn: AUC 0.97, and at
the hint (0.9) 91% of media time against 0.7 of 72 minutes of people
present. Without the names as people present, 315 untagged turns were
offered instead of 24, people the operator had named. AudioSet's classes
(AST) added to the embeddings made it worse. Most media was already tagged:
24 untagged turns (4 minutes) were offered on the live data.

Done 2026-10-30: of the turns at the hint the operator had tagged, 240 were
Media, 4 Music, and none someone present; 18 (4.3 minutes) waited untagged,
and once tagged the hint's clip tags were still only Media and Music. Turns
at the hint now leave the stream as `_media` without a tag, and the portal
keeps offering them so a wrong one can be put back. `_mic` trains neither
side: someone on a microphone is between present and media.

Music, checked the same day, needs nothing: AST's Singing class reached no
more than 0.13 on any of 9,837 turns, so songs don't become turns. Music is
only under speech (34 of 1,072 minutes above 0.5, bars and a yoga class),
and a music filter at that level would have dropped 91 of the owner's turns.
inaSpeechSegmenter, built for speech / music detection, agreed: of 63
minutes of music in all the recordings, 48 were outside any turn and never
reach the stream, and the turns it called mostly music were people talking
over it, 27 of them the owner's by voice.
Revised the same day: some songs do become turns. The operator had tagged
five lines of rap at a party `_music` (c20000101T000012Z, 26 s, all not the
owner by voice), and AudioSet files rapping as its own class under Singing,
which the check read alone. Slice 22.

## 22. Songs that become turns

Near a speaker playing music, the transcriber sometimes writes down the
song's words, and they reach the stream as someone's turns. Music under
people talking is real speech and stays; only turns whose words are the
song's go, as `_music`, like the operator's own tags. Pure music without talk
never forms a conversation (Silero hears no speech in it), and that stays.

- Score: every turn on reprocess, by AST's AudioSet classes for speech,
  singing (Rapping among them) and music, cached by audio like affect
  (`hearsay/music.py`). In the database only, not the stream.
- Label: `hearsay-label music` plays turns the model hears as little speech
  (under 0.3), spread across the music score, and asks whether the words are
  the song or someone talking; the answers are in labels/music.jsonl.
  `hearsay-label music report` gives, per music cutoff, what it would drop
  (turns, minutes, the owner's turns) and how the answers above it split.
  Changed 2026-10-30 from drawing on the music nights and `_music` tags: the
  operator's listening found low speech with high music is lyrics, and low
  speech with low music is quiet real talk, while singing didn't rank them.
  388 turns (15 minutes) score under 0.3 speech; none score 0.5 singing.
- Apply: once about 100 are labeled, turns above a threshold chosen for
  precision on song (dropping real speech is worse than keeping some
  lyrics) are `_music` without a tag. The operator's tags always win.

Applied 2026-10-30, on 114 answers (25 song, 89 talk): music alone was clean
only at 0.55, which caught 8 songs. Music at least 0.3 with speech under
0.22 and at least 1 s caught 18 songs and 2 talk; the talk left was 0.6-0.8 s
turns, whose padding keeps their speech score low. On all turns the rule
drops 30 (1.8 minutes), 3 the owner's by voice. A song or talk answer wins
over the rule, so the 2 talk turns stay.

Done when: songs' words rarely reach the stream, and the labeled turns say
the threshold drops almost no one talking.

Done 2026-11-05: listening through new capture on the conversation pages
(slice 20), songs' words no longer reached the stream, a session of songs
one night was clean, and so was the audio for a dance class.

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
dinner's two speakers became the owner plus three voices, and Veronica came
apart from Bella and the strangers. Still lumped: Robert and Christian in
c20260929T025421Z, and c20260930T232900Z (four voices under one label). Every
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
after the prompt, reached the stream (c20261006T182003Z) with the owner by
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
the rollback for its re-link of segments.

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
- The stream carries only places the operator has named ("Chill Room"),
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
accuracy, no confident motion between) makes an anchor, and fixes stop.
Core Motion's activity (still, walking, cycling, driving) asks for fixes
sooner or later; a geofence, sustained motion, the car connecting or a fix
every 30 minutes suspects leaving, and a precise fix outside the anchor by
more than its accuracy confirms it, alone only with motion or the geofence
agreeing. After driving, settling takes 10 minutes unless the owner walks
or the car's Bluetooth disconnects, so traffic doesn't settle. The car is
whichever Bluetooth audio output the owner picks in the app, read from the
audio route without touching it; it suggests the car is near, never that
it moves. Settled, the app says every 5 minutes it's still at its anchor,
and the NAS reads that as a reading. Activity, route changes and every
state, with its confidence and reason, are records in the upload, so trips
can be reconstructed from raw later. AirTags were left out: Find My has no
public API. The state machine is ios/Hearsay/Movement.swift, tested on its
own (`swift test` in ios/). Paused with paused audio kept on the phone, it
keeps tracking into the paused window, so its records reach the NAS only
with the stretches the owner uploads. Walking only delays settling: precise fixes that
keep agreeing for 10 minutes settle anyway (at lower confidence), so a
party or a dance class is a stay; not after driving, so a jam isn't.

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
worse than 300 m are left out too (a stay's readings carry its anchor's
radius, at most 300). The app offers both on each conversation it lists
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
owner similarity, affect and delivery. About 11 MB for 14,000 turns, against
9 GB of audio and decoded audio. Reprocess reads audio only for
conversations that are new or still growing, and rebuilds the rest from
these records and the operator's labels. Who is speaking (thresholds,
clustering, voiceprints, names) still re-runs on every turn ever recorded; what came out of
the audio is fixed once its audio is gone. A new speaker model applies only
to audio still kept, so changing models is a deliberate migration.

Done when: a rebuild from the records equals a rebuild from raw, and a
reprocess with nothing new reads no audio.

## 20. Throw out what was said, and hold the stream a week

The operator can't take anything back once it's said near the pendant: it
reaches the stream within the hour, and Idea Machine sends it to Claude.
Instead, nothing reaches consumers for a week, and in that week the
operator can read and hear a whole conversation and throw out parts of it.
Decided 2026-10-26:

- A conversation page in the portal: the transcript, each turn with who
  said it, when, and at which named place, and audio that plays from any
  turn on through the conversation. A list of conversations, newest first,
  says which are still held and for how long.
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
  and places) a week after it ends, or sooner when the operator releases
  it from its page. Until then consumers don't see it at all. Additive, so
  `format_version` stays 1; Idea Machine's captures, self-notes too, arrive
  a week late unless released.
- The audio, raw payloads and transcription output of a thrown-out stretch
  are kept until slice 12 deletes them. Gone means every copy: raw
  payloads, conversation WAVs, the decoded audio and embedding cache, the
  transcription output, and the NAS's nightly snapshots once they expire.

Comes before 19 and 12: it needs neither, and it is the only way to keep
something from Claude until then.

Done when: the operator reviews a week of conversations on the page,
thrown-out stretches never reach Idea Machine, and the rest arrives on
release or after the week.

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

Music, checked the same day, needs nothing: AST's Singing class reached no
more than 0.13 on any of 9,837 turns, so songs don't become turns. Music is
only under speech (34 of 1,072 minutes above 0.5, bars and a dance class),
and a music filter at that level would have dropped 91 of the owner's turns.

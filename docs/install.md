# Running Hearsay

The operator's guide. Install says which script runs on which host, in what
order, and what to do between them; the sections after it cover running
Hearsay: recording, reprocessing, your voice, reading the stream, importing
missed audio and correcting who spoke. For what Hearsay is, see the
[README](../README.md).

## Install

Each script is idempotent and walks you through any step that needs you.
Secrets are kept in env files outside the repo.

The pipeline runs on [corral](https://github.com/lowmagic/corral), set up
first from the infra repo: its server on the NAS, a NAS worker offering `nas`
from `/mnt/fast/corral-worker`, and a worker offering `gpu` on the dev box,
which runs as its own user, `corral`. Corral must be a version with held jobs (`wait_seconds`): update it before Hearsay.
`nas.sh` and `gpu.sh` add Hearsay's kinds to those workers and check they're
running, and `nas.sh` that the server has held jobs.

1. **Dev box (optional):** [`install/dev.sh`](../install/dev.sh) sets up a venv, a dev secret in
   `~/.config/hearsay/dev.env`, and runs the tests.
2. **NAS (TrueNAS SCALE 25.04+):** clone the repo as `admin` into the
   `fast/repos` dataset and run [`install/nas.sh`](../install/nas.sh) with sudo:
   ```sh
   git clone https://github.com/lowmagic/hearsay.git /mnt/fast/repos/hearsay
   sudo /mnt/fast/repos/hearsay/install/nas.sh
   ```
   This creates the datasets under `fast/hearsay` (the NVMe pool) and their
   backups, the secrets, builds the images, starts the portal on the LAN
   and tailnet and the capture receiver on the tailnet, checks that every
   captured payload reprocesses, reprocesses once, and waits for a real
   upload from the phone or Mac (on a new install it stops there; re-run it
   after step 4). The NAS must already be on your tailnet (the Tailscale
   app in the TrueNAS UI). Before you run it, the Apps pool must be set in
   the TrueNAS UI, and the NAS-wide backups must exist there (Data
   Protection): a recursive periodic snapshot task and a replication task
   for `fast`. The script checks both and tells you if they aren't.
3. **Dev box with the GPU, as the worker's user, `corral`:**
   [`install/gpu.sh`](../install/gpu.sh) sets up transcription (Parakeet,
   pyannote) as a kind on the dev box's corral worker, which pulls new WAVs
   from the NAS over `ssh nas` and writes transcripts back. `corral` has two
   keys of its own, without a passphrase, each named for what it opens:
   `~/.ssh/nas`, added to the NAS user's authorized keys, and
   `~/.ssh/hearsay_deploy`, added to this repo under Settings > Deploy keys
   with write access left off, since the repo is private. Keeping them apart
   means either can be replaced without breaking the other. As `corral`:
   ```sh
   sudo -iu corral
   ssh-keygen -t ed25519 -N "" -C corral@gpu-host -f ~/.ssh/nas
   ssh-keygen -t ed25519 -N "" -C corral@gpu-host -f ~/.ssh/hearsay_deploy
   cat >> ~/.ssh/config <<'EOF'
   Host nas
       HostName <the NAS's address>
       User admin
       IdentityFile ~/.ssh/nas
       IdentitiesOnly yes
   EOF
   chmod 600 ~/.ssh/config
   ```
   Add `~/.ssh/nas.pub` on the NAS and `~/.ssh/hearsay_deploy.pub` on GitHub,
   then clone with the deploy key, which the clone keeps for later pulls, and
   run the install from it:
   ```sh
   git clone -c core.sshCommand="ssh -i ~/.ssh/hearsay_deploy -o IdentitiesOnly=yes" \
       git@github.com:lowmagic/hearsay.git ~/hearsay
   ~/hearsay/install/gpu.sh
   ```
   It asks for a Hugging Face token for the gated diarization model, and
   transcribes everything pending once.

4. **iPhone, from the Mac, as your user (replaces Omi's app and cloud as
   the audio path):** [`install/ios.sh`](../install/ios.sh) builds the pendant
   app with Xcode and installs it on the phone. It asks for the capture URL
   that `nas.sh` prints; the token is entered in the app. The phone needs
   Tailscale. The pendant takes one connection at a time, so disconnect it
   from Omi's app if it was ever paired there.
5. **The operator's personal Mac, as your user (optional; never a work
   machine):** [`install/mac.sh`](../install/mac.sh) builds the menu-bar
   recorder and starts it at login. It records the mic and system audio as
   two channels, from the menu or, after asking you, when a Zoom call starts,
   and uploads to the same capture URL and token as the phone. Recording a
   call can need everyone's consent where you are. Needs macOS 14.4+.

To update, run `git pull` on every host, then re-run `install/nas.sh` on the
NAS, `install/gpu.sh` on the dev box, and `install/ios.sh` and
`install/mac.sh` on the Mac, in that order: the NAS must read a new kind of
upload record before the phone or Mac sends it. The dev box runs its own
copy of hearsay, in `corral`'s home, so a `git pull` there takes effect when
`install/gpu.sh` re-runs. With a
free Apple ID the phone's install expires after 7 days; re-running
`install/ios.sh` renews it.

## Data

Uploads land in `/mnt/fast/hearsay/raw/capture/<date>/`; Omi's webhook
payloads (until 2026-09-30) stay in `raw/{transcript,audio,memory}/` and are
still parsed:

- `.body` holds the request body, byte for byte.
- `.json` is the sidecar: timestamp, query params, headers without the
  token, size, and sha256.

Each directory under `/mnt/fast/hearsay/` is a dataset of its own, backed up
by what it holds ([`install/nas.sh`](../install/nas.sh) sets this up):

- Audio, and what's made from it alone: `raw`, `imports`, `audio`
  (conversation WAVs), `pcm` (decoded audio, a cache) and `transcripts`
  (the dev box's, which records hold once sealed). Daily snapshots at 04:00
  kept 7 days, replicated to `storage/hearsay-audio-replica` and kept there
  as long. Input audio isn't precious: what it gave is kept in records, and
  the audio itself goes at retention, so its backups don't keep it long
  after.
- What's kept: `records`, `db`, `labels`, `stream` and `config`. The
  NAS-wide backups: `fast` snapshotted daily and kept 2 weeks, replicated to
  `storage/fast-replica` and kept 2 months. These aren't Hearsay's; `nas.sh`
  only adds the audio datasets to their excludes.

The TrueNAS UI shows both under Data Protection.

## Recording from the pendant (iPhone)

The app ([`ios/`](../ios/)) connects to the Omi pendant over Bluetooth, keeps
everything it sends on the phone (in the app's own storage, excluded from
backups), and uploads it every minute to the capture receiver on the NAS over
the tailnet. It keeps recording in the background, and iOS relaunches it when
the pendant comes back in range. If the NAS is unreachable, uploads wait on
the phone. The app shows the pendant's state, battery and charging, the last
minute's packet counts, and how many uploads are waiting, and sets the
pendant's light (saved on the pendant).

While out of range the pendant records to its own storage; on reconnect the
app downloads that and uploads it like the rest, and the NAS places it by the
pendant's clock.

The app is on, paused or off. On, everything reaches the NAS. Paused, nothing
does: it's kept on the phone, where a paused stretch can be played, cropped to
one selection and uploaded, shared as a WAV (AirDrop or Files; the copy is
yours, and Hearsay never deletes it), or deleted, and it's deleted on its own a number
of days after the pause ends (30 to start; Settings, Paused audio). A paused
stretch with 5 seconds of audio or less is deleted as soon as the pause
ends, and nothing of it is sent. Switching
from paused to on mid-conversation sends that conversation from where it
began: the phone looks back up to 2 hours for a gap of 3 minutes without
speech, as the NAS does between conversations, with Apple's speech detector
rather than the NAS's, and the paused audio before it stays to review. Off
turns the pendant's mic off in hardware and keeps nothing. If the app was
paused at any time while the pendant was away, everything it stored then
counts as paused; if it was off, none of it is kept.

The pendant's single and double tap are set in the app (Pendant button): mark
to keep the conversation around the tap, from 30 s before it to 30 s after
you stop, even when it's short; mark to end the conversation there; pause, or
off (either one, tapped while paused or off, switches on); or nothing. Each confirms with
its own buzz. Single tap defaults to keep, double tap to pause. Holding the
button 3 s powers the pendant down. The NAS must be updated before the
app whenever the upload format changes, as the update order above does.

While recording, location fixes back off once the phone settles at a stay.
Its five-minute heartbeats carry the anchor fix's reported accuracy separately
from the geofence radius; older captures have only the radius, so reprocessing
cannot recover their original fix accuracy. Leaving needs two fixes at least a
minute apart, each accurate to 20 m or better and farther from the anchor than
the two fixes' combined uncertainty. Motion or a geofence exit only asks for a
check; poor indoor fixes don't end the stay. Sustained-motion checks back off
from five minutes to thirty minutes; this rule doesn't add continuous GPS.

## Recording on the Mac

The menu-bar recorder (`mac/`, installed by `install/mac.sh`) records the
mic and system audio as separate channels: Record and Stop in its menu tap
all system audio; when Zoom opens the mic for a call it asks first, then taps
Zoom only and stops when the call ends. Where a Mac recording and the pendant
overlap, the Mac's wins. A turn loud on the mic and quiet on the call is
yours, the reverse someone else's (basis `channel` in the stream). Log:
`~/Library/Logs/hearsay-mac.log`. The NAS must be updated before the Mac app
whenever the upload format changes.

## Reprocessing

Everything derived from raw is rebuilt by reprocess, which corral's NAS
worker runs (log: `journalctl -t hearsay-reprocess`; the portal and capture
receiver log to the journal too, e.g.
`journalctl CONTAINER_NAME=hearsay-capture`).
Conversations are found in the audio stream itself (voice detection; a
conversation ends after 3 minutes of silence, or at an end mark you make with
the pendant's button), not taken from Omi. One counts as ended once the
recording has run on 3 minutes past its last speech, or, if you stop
recording sooner, 8 minutes after it (the hourly check finds it). After each upload the capture receiver
enqueues a check ([`hearsay/check.py`](../hearsay/check.py), log:
`journalctl -t hearsay-check`): it finds conversations the way reprocess
does, with voice detection cached, and only when one has ended that the
database doesn't have as ended yet, enqueues the pipeline
([`hearsay/enqueue.py`](../hearsay/enqueue.py)): reprocess to assemble the new
audio into WAVs, transcription on the dev box, reprocess again to turn the
transcripts into turns, then Idea Machine. So a conversation is transcribed
once, after it ends, not while it goes on. Transcription takes a backlog in
chunks of about corral's window for `hearsay.transcribe`, and the second
reprocess waits until it's done, but never more than 30 minutes, so a long
backlog reaches the stream at least every half hour. A TrueNAS cron job
enqueues the check hourly at :30, for uploads that couldn't reach corral and
recordings stopped soon after their last speech, and another enqueues the
whole pipeline nightly at 03:00, for what's due by time alone (a hold ending,
sealing, retention) and anything a check missed; log: `journalctl -t hearsay-enqueue`.
Edits in the portal start a reprocess of their own (Correcting who spoke,
below). It all runs as each machine is free, with nothing to run by hand;
`corral pause` on the dev box's worker holds transcription while you use its
GPU, and takes effect between chunks. A conversation that fails to transcribe is left a marker in
`transcripts/failed/` and skipped until its WAV or the settings change; delete
the marker to retry it sooner. Each run builds
a complete new database from raw in memory, then updates the real one in a
single transaction that writes only the rows that changed, so readers never
see a half-built database and a run with nothing new writes almost nothing
(`fast` is snapshotted nightly). Slow steps are cached by audio content in
`db/cache.sqlite`.

A conversation is sealed when the week Idea Machine waits for it ends
([`hearsay/hold.py`](../hearsay/hold.py)), once it's transcribed and every one
before it is sealed ([`hearsay/records.py`](../hearsay/records.py)): what its
audio gave (boundaries, transcript, voice embeddings, music and tone scores)
is kept in `/mnt/fast/hearsay/records/`, and from then on reprocess takes it
from there and never reads that audio again. So a run with nothing new reads
only the last week's audio. Who spoke, your tags and throw-outs,
places and the stream still re-run over everything. A sealed conversation
keeps the transcript and models it was sealed with: a Words update or a new
model reaches it only if you unseal it, while its audio is still kept: never
more than 60 days back, nor so far that a conversation that began before
then would be redone. This moves the seal line back to before a date, and
the next reprocess redoes everything after it from raw:

```sh
sudo docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm reprocess python -m hearsay.records unseal 2026-10-01
```

An import, or pendant audio downloaded late, that lands behind the line, or
less than three minutes past it where its speech joins the last sealed
conversation, unseals back to it by itself, and so does an enrollment window
registered once the line has passed its start (so enroll from the last two
weeks' audio, or everything since the window is redone). Anything more than
60 days back is left out instead, and the run lists it. To check that a
build from the records equals a build from raw (slow: it builds the
database twice, once reading all the audio); up to the last conversation
audio was deleted or cut from, both builds take the records, and it says so:

```sh
sudo flock /run/hearsay-reprocess.lock docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm reprocess python -m hearsay.records check
```

At the end of every reprocess, once it has sealed, audio is deleted from
sealed conversations ([`hearsay/retention.py`](../hearsay/retention.py)):

- A stretch you threw out, as soon as its conversation is sealed: it's
  silenced in the conversation's WAV, its words are blanked in the record
  and in the dev box's transcript (timings kept, so the turns around it
  stay as they were), and raw uploads and imports wholly inside it are
  deleted. Those that only reach into it stay until the 60 days.
- Everything that's audio, 60 days on, yours too: each raw upload and
  import 60 days after its own audio ends, and a conversation's WAV (its
  page then says No audio) and transcript files 60 days after the
  conversation ends. Raw audio and imports covering an enrollment window
  stay, so you can be enrolled in a new speaker model. Omi's old transcript
  and memory payloads are text and stay. The records keep every other
  turn's words, voice vectors and scores.
- A transcript file or failure marker whose conversation no longer exists
  (its boundaries moved while it was held, or it's from Omi's days), on
  any run.
- Decoded audio goes on the run after the audio is sealed, and cached
  model results once no run has used them for a week.

Each deleted file is listed in `/mnt/fast/hearsay/records/deleted.jsonl`
before it goes; the NAS's snapshots keep it until they expire. To see what
the next reprocess would delete and cut, changing nothing (how many, how
big, and when their audio ends):

```sh
sudo flock /run/hearsay-reprocess.lock docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm reprocess python -m hearsay.retention report
```

To run the whole pipeline now, enqueue it on the NAS (`check` instead runs it
only if a conversation has ended):

```sh
sudo docker exec hearsay-capture python -m hearsay.enqueue chain
```

To run only a reprocess, by hand, on the NAS. The lock is the worker's too,
so this waits for a run in progress, and a queued one waits for this.

```sh
sudo flock /run/hearsay-reprocess.lock docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm reprocess
```

- `/mnt/fast/hearsay/db/hearsay.sqlite`: parsed payloads. Updated only if
  every payload parses; otherwise the failures are listed and the old database
  and audio are left in place. Deleting it is safe: the next run builds it
  again from raw and the records.
- `/mnt/fast/hearsay/audio/<conversation_id>.wav`: each conversation's audio,
  aligned so that segment times are offsets into the file. Missing audio is
  silence; the `conversation_audio` table records how each file was aligned
  and what fraction of it is real audio. A WAV is rewritten only when its
  audio changes, and the new one is moved in (from `audio/.staged/`) only
  once the database describing it is in place, so a failed run leaves the
  old audio too.
- `/mnt/fast/hearsay/pcm/`: uploads and imports decoded to PCM, cached so
  each is decoded once, and only while its conversation isn't sealed.
  Deleting it is safe: the next run decodes them again.

- Speaker embeddings and owner / not-owner labels per segment, in the
  `segment_speakers` table.

Members of the `apps` group can read the outputs.

## Owner voice (dev box, then NAS)

Speaker labels need your voice enrolled, and thresholds tuned against segments
you've labeled by ear. Your input lives in `/mnt/fast/hearsay/labels/`,
which reprocessing reads but never writes. The labeling tool runs on any
machine of yours with `ssh nas` and an audio player (`paplay` or macOS
`afplay`): from a checkout as `.venv/bin/python -m hearsay.label`, or installed
as a command with
`uv tool install git+ssh://git@github.com/lowmagic/hearsay.git`, then
`hearsay-label` in place of `.venv/bin/python -m hearsay.label` below.

1. Record yourself reading aloud for about 3 minutes, alone, with the pendant
   streaming. Then register the window (local time):
   `.venv/bin/python -m hearsay.label enroll 2026-09-27T10:05 2026-09-27T10:08`
2. Reprocess on the NAS (above).
3. Label segments by ear: `.venv/bin/python -m hearsay.label` (plays audio
   locally, over `ssh nas`). Turns are drawn across the whole similarity
   range; to fill in the band a threshold falls in, add
   `--between 0.30 0.40` and only turns scored in it play.
4. Re-hear the labels the model disagrees with, answering fresh:
   `.venv/bin/python -m hearsay.label review`. Answer `u` whenever you can't
   tell; a guess does more harm than no label.
5. See precision per threshold: `.venv/bin/python -m hearsay.label report`,
   then set `OWNER_THRESHOLD` / `NOT_OWNER_THRESHOLD` in
   [`hearsay/speakers.py`](../hearsay/speakers.py), deploy, and reprocess.
6. Turns too short to judge by voice take the label of their diarized speaker
   in the same conversation. Check those by ear, blind, in random order:
   `.venv/bin/python -m hearsay.label check`. After a reprocess, `report`
   shows their precision and how much speech is still unlabeled.

## Reading the utterance stream (consumers)

`/mnt/fast/hearsay/stream/` (readable by the `apps` group) holds Hearsay's
output, rewritten after every reprocess: `index.json`, one file per
conversation under `conversations/`, and `forgotten.json`. No audio is ever in
it. What every field means, when it changes and what a consumer can rely on
is in [stream.md](stream.md).

## Importing audio the live stream missed

If something was recorded that never reached Hearsay (another recorder, or
Omi's app before it was retired), export the audio and import it with the
recording's start time, in your local time:

```sh
hearsay-label import ~/Downloads/omi_export.mp3 "2026-09-28 19:42"
```

It is copied unchanged to `/mnt/fast/hearsay/imports/` over `ssh nas` and
placed on the timeline at that time on the next reprocess; where it overlaps
live audio, the live audio wins. Importing the same file twice does nothing.
A recording that began more than 60 days ago is refused: Hearsay keeps no
audio that old.

## Correcting who spoke (phone or any browser)

Hearsay says only whether you spoke: it names nobody else (since 2026-10-28;
names given before then are kept in the labels and only teach the TV hint
what people sound like). The web portal that `nas.sh` starts, at the
addresses it prints, plays the turns that sound like the TV you've tagged
Media, then the turns it couldn't decide, latest first. It listens on the LAN and tailnet only, so
from your phone use the tailnet address, or the Hearsay app's Tag tab,
which shows the same portal and logs in with the app's capture token. Tag
each turn Me, Other, Mic, Media, Music or Noise: Me reaches the stream as
yours, Other and Mic (someone on a microphone or PA) as a stranger, and the
rest (TV or video talk, songs, noise) is left out. Left out isn't gone: its
words stay in Hearsay, and a tag puts it back. Delete throws the turn out, as
on a conversation's page below: undo it until the next reprocess, then it's
gone for good. A tag applies to that turn whatever its voice said. The more TV you tag
Media, the better the TV list gets. Turns on the TV list leave the stream as Media even untagged; tag one
Other, Me or Mic to put it back.

The portal's Conversations page lists every conversation, newest first, by
day; narrow it to a named place, or to the ones whose words contain a search.
A conversation that's mostly media or music (half its speech time or more),
or mostly people other than you (80% or more), is flagged there.
Tap a day (or Today on the front page) to read it through: its conversations
in order, with how long it was quiet between them. Times are the NAS's time
zone, which `nas.sh` hands the portal.
Idea Machine sees a conversation only a week after it ends, and each
says whether it's in the stream yet or held, and until when. To send one
sooner, once it has ended, press Release to Idea Machine on its page and
then Release on the page that asks. It goes 5 minutes later, and until then
the button is Cancel release; once it's gone it can't be taken back, only
thrown out.
Each opens as its transcript in order, with who the stream says said each
turn (or why it's left out) and where, and links to the conversations before
and after it; tap a turn's time to play the
conversation from there. To throw out turns, check them, open Select
multiple or discard at the bottom and press Throw out checked, or Throw out
all for the whole conversation. There, All not me and All
media check the turns Hearsay is sure of; those marked yellow (it couldn't
tell who spoke, or it might be media, or the voice is close to yours) are
left for you. Check between fills in a stretch from the first turn checked
to the last. Under Tag or place, Tag checked tags the checked turns as the
front page tags one, and Place from checked puts the conversation at a place
from the first turn checked to its end (from its start if none is), whatever
the phone's readings said; both show as they will be until the next
reprocess applies them. Throwing out takes a few words of why if you like (Idea Machine sees
them). Each edit starts a reprocess once you've stopped making edits for 10
minutes (at most 30 minutes after the first), so you can undo it until then.
The portal's Apply now starts it as soon as the NAS is free. Words changes
start the whole pipeline instead (below). Once it runs, its words leave the
stream and Hearsay's database for good, and Idea
Machine is told to delete what it took from them. Once the conversation is
sealed (a week after it ends, or on the next reprocess if it already
is), its audio is silenced, its words are cut from the records and
transcripts, and the raw audio wholly inside it is deleted; raw audio that
only reaches into it goes at 60 days with the rest (see Reprocessing).

The portal's Places page lists spots where the phone recorded and no named
place is near (how long, which days, a map link); name them there. Only these
names reach the stream, never coordinates.

The portal's Words page lists words transcription leans toward (names,
jargon). Type them one per line and save a draft; it shows what would change
and waits as long as you like. Each Update enqueues the
pipeline, which re-transcribes the conversations not yet sealed (the last two
weeks), so batch your edits; to redo older ones too, unseal them
(Reprocessing).

Tags are stored by the time of the turn you heard, in
`/mnt/fast/hearsay/labels/`, so they survive re-transcription and apply to
every past conversation. The portal takes a tagged turn off its list at once;
the stream picks the tag up on the next reprocess. To measure how well your
voice is recognized, label turns blind with `hearsay-label` and read
`hearsay-label report`.
To pick where songs' words leave the stream, answer song or talk with
`hearsay-label music` and read `hearsay-label music report`. `hearsay-label music --dropped`
plays only the turns the rule is leaving out, to check it by ear.

To run the test suite against the real captures (slow: it repeats a whole
reprocess without the cache):

```sh
sudo docker compose -f /mnt/fast/repos/hearsay/install/compose.yaml run --rm check
```

After a `git pull`, re-run `install/nas.sh` first so both commands use the new code.

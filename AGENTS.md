# Hearsay

Hearsay turns captured audio into attributed utterances: who said what, when.
It ingests audio from the pendant (through the iPhone app) and the Mac,
assembles it, transcribes, diarizes, and
identifies speakers: the owner or not. It names nobody else (2026-10-28).

Hearsay does NOT categorize, summarize, extract ideas, or interpret content.
That belongs to downstream consumers (Idea Machine). If a change requires
understanding what was said, it is out of scope.

## Output contract

The product of Hearsay is the utterance stream (`hearsay/stream.py`): who said
what, when, one JSONL file per conversation plus `index.json`, never audio.
[docs/stream.md](docs/stream.md) is its contract with Idea Machine, and the
only place it's written down: fields, speakers, how conversations and their
ids change, the vocabulary Idea Machine sends back, and the guarantees. A
change to the stream's format or meaning updates it in the same change.

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
  A work machine is never used for capture and never holds audio.
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

Me / not me is done (2026-10-28, roadmap slice 5): Hearsay names nobody but
the owner, and the portal plays the turns it couldn't decide for the
operator to tag. Now slice 21: the portal also offers the turns that sound
like the media the operator has tagged (`hearsay/media.py`), so TV is tagged
as it comes. Next, slice 20 (throw out what was said, and hold the
stream a week), then slice 19 (turns kept without their audio) and slice 12
(retention), which change the invariants on raw payloads and re-running from
raw. Slice 12 also waits for Idea Machine's M2, which validates me / not me.
Every slice, done or shelved, and what was measured for it is in the
[roadmap](docs/roadmap.md).

## Coding standards

Code, tests, comments and prose follow [docs/standards.md](docs/standards.md),
which builds on the org's [principles](https://github.com/junglestyle/.github/blob/main/PRINCIPLES.md), [docs/org-standards.md](docs/org-standards.md) and
the copy of each topic listed in `.org-standards`, such as
[docs/org-standards-python.md](docs/org-standards-python.md).
Run `make check` before committing; it runs everything CI runs except the org standards check.

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

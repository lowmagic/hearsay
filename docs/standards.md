# Coding standards

Hearsay follows the [org standards](org-standards.md), an unchanged copy of
[junglestyle's](https://github.com/junglestyle/.github/blob/main/STANDARDS.md). This file adds Hearsay's own rules and
examples. Where it differs from an org rule, it says so, and it wins.

## YAGNI

- The current milestone is the current slice in [AGENTS.md](../AGENTS.md#current-slice). A deferred requirement gets a
  slice in the [roadmap](roadmap.md).
- When several solutions satisfy the requirements, prefer, in order: fewer concepts, fewer moving parts, fewer
  dependencies, less hidden behavior, easier failure recovery.

## Zen

- One obvious way: only the reprocess builds the database and writes the stream, from raw payloads, imports and labels.
  The portal and `hearsay-label` write labels and read the database; they never change it.
- Keep functions cohesive and reasonably small, but don't split code just to meet a line count.

## Pragmatic Programmer

- Orthogonality: keep the existing boundaries (capture receiver, raw payloads, reprocess, transcription on the GPU host,
  the stream) unless the task requires changing them.
- Overrides the org rule's ADRs in `docs/stack.md`: Hearsay records a decision in the docstring of the module it shapes
  (e.g. `hearsay/transcribe.py`, `hearsay/affect.py`) and in the [roadmap](roadmap.md).

## Testing

- Also worth a test here: upload parsing, deduplication and retry (the same upload or import twice), crash consistency
  (a failed reprocess, atomic stream writes), and artifact identity (stream revisions).
- Prefer an integration test when the property crosses a process, persistence, protocol or filesystem boundary.
- Don't test every enum value or branch on its own, or what types, compilation or the framework already guarantee.
- Where compiling, a manual run or a check by ear gives more confidence than a unit test, use that instead.
- Skip a test for behavior that a slice's end-to-end acceptance check already covers better.
- Fixtures are synthetic and committed. Real captures never enter the repo.

## Running it

- `make check` runs everything CI runs: the tests, in the `.venv` that `install/dev.sh` makes, then a diff of
  [org-standards.md](org-standards.md) against the org's current file, so a hand edit or a stale copy fails. That
  diff fetches the org's file, so `make check` needs network. `make sync-standards` refreshes the copy.
- Install and deployment live in `install/`, one script per target, in the order [install.md](install.md) gives. The
  rules for those scripts are in [AGENTS.md](../AGENTS.md#installation).

## Privacy

- Overrides the org rule's `.env.example`: config shape lives in the `install/` scripts, which write each env file
  outside the repo.
- Overrides the org rule on deployment config. Hearsay has one deployment, and its layout is committed: paths, dataset,
  ports and the apps UID in `install/lib.sh` and `install/compose.yaml`, and docs may name them. Hostnames, IPs,
  tailnet names, personal accounts and secrets never are. `nas` is an ssh alias for the role, not a hostname.

## Prose

- Overrides the org rule's "no dates" for a dated measurement that backs a constant (e.g. "Thresholds tuned on
  2026-09-27" in `hearsay/speakers.py`). It's evidence for the value, not history.

# Coding standards

Hearsay follows the [org standards](org-standards.md) and the org's [Python standards](org-standards-python.md),
unchanged copies of [junglestyle's](https://github.com/junglestyle/.github/tree/main/code-standards). `.org-standards`
lists the topics it copies beyond the general one. This file adds Hearsay's own rules and examples. Where it differs
from an org rule, it says so, and it wins.

## YAGNI

- The current milestone is the current slice in [AGENTS.md](../AGENTS.md#current-slice). A deferred requirement gets a
  slice in the [roadmap](roadmap.md).
- When several solutions satisfy the requirements, prefer, in order: fewer concepts, fewer moving parts, fewer
  dependencies, less hidden behavior, easier failure recovery.

## Zen

- One obvious way: only the reprocess builds the database and writes the stream, from raw payloads, imports and labels.
  The portal and `hearsay-label` write labels and read the database; they never change it.
- Code never handles a database older than itself: the database is rebuilt from raw, so a missing table or column is a
  real failure, not one to work around.
- Errors never pass silently on the unattended clients either: the iPhone and Mac apps surface any error that loses
  audio where the operator will see it, not just in a log.
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

- `make check` runs everything CI runs except the org standards check: the tests, in the `.venv` that
  `install/dev.sh` makes.
- `make standards` checks [org-standards.md](org-standards.md) and each topic's copy against the org's current
  files, so a hand edit, a stale copy, or a copy whose topic isn't in `.org-standards` fails. It needs the network,
  so it isn't part of `make check`; CI runs it as its own job. `make sync-standards` refreshes the copies; to follow
  another topic, add it to `.org-standards` and run it.
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

## Known deviations

Where Hearsay doesn't follow the [Python standards](org-standards-python.md) yet. These are debt to pay down, not
overrides: each one should go.

- Tooling: there's no `uv.lock` or `.python-version`. CI installs with `uv pip install -e ".[dev]"`, not
  `uv sync --frozen`, and the dev tools are an extra, not a `dev` dependency group. It builds with setuptools, not
  hatchling.
- `make check` runs pytest only. Ruff and mypy aren't configured or run.
- Images install with `pip`, and torch comes from the CPU index through `pip --index-url`, not `[tool.uv.sources]`.
- Dependencies: the extras pin exactly (`==`). Only `transformers` in `transcribe` names the break its pin avoids.
- Layout: the package is `hearsay/`, not `src/hearsay/`.

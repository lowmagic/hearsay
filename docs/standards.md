# Coding standards

Hearsay follows the [org standards](org-standards.md) and the org's standards for [Python](org-standards-python.md),
[SQL](org-standards-sql.md), [containers](org-standards-containers.md) and [shell scripts](org-standards-shell.md),
unchanged copies of [lowmagic's](https://github.com/lowmagic/.github/tree/main/code-standards). `.org-standards`
lists the topics it copies beyond the general one. This file adds Hearsay's own rules and examples. Where it differs
from an org rule, it says so, and it wins.

## Structure

- When several solutions satisfy the requirements, prefer, in order: fewer concepts, fewer moving parts, fewer
  dependencies, less hidden behavior, easier failure recovery.
- One obvious way: only the reprocess builds the database and writes the stream, from raw payloads, imports and labels.
  The portal and `hearsay-label` write labels and read the database; they never change it.
- Keep functions cohesive and reasonably small, but don't split code just to meet a line count.
- Keep the existing boundaries (capture receiver, raw payloads, reprocess, transcription on the GPU host, the stream)
  unless the task requires changing them.

## Errors and recovery

- Code never handles a database older than itself: the database is rebuilt from raw, so a missing table or column is a
  real failure, not one to work around. That overrides the Python standards' numbered migrations: a schema change ships
  with the code, and the next reprocess rebuilds the database.
- Errors never pass silently on the unattended clients either: the iPhone and Mac apps surface any error that loses
  audio where the operator will see it, not just in a log.

## Testing

- Also worth a test here: upload parsing, deduplication and retry (the same upload or import twice), crash consistency
  (a failed reprocess, atomic stream writes), and artifact identity (stream revisions).
- Prefer an integration test when the property crosses a process, persistence, protocol or filesystem boundary.
- Don't test every enum value or branch on its own, or what types, compilation or the framework already guarantee.
- Where compiling, a manual run or a check by ear gives more confidence than a unit test, use that instead.
- Skip a test for behavior that a slice's end-to-end acceptance check already covers better.
- Fixtures are synthetic and committed. Real captures never enter the repo.

## Running it

- `make check` runs the tests, in the `.venv` that `install/dev.sh` makes.
- Install and deployment live in `install/`, one script per target, in the order [install.md](install.md) gives. The
  rules for those scripts are in [AGENTS.md](../AGENTS.md#installation).

## Privacy

- Config shape lives in the `install/` scripts, which write each env file outside the repo.
- Overrides the org rule on deployment config. Hearsay has one deployment, and its layout is committed: paths, dataset,
  ports and the apps UID in `install/lib.sh` and `install/compose.yaml`, and docs may name them. Hostnames, IPs,
  tailnet names, personal accounts and secrets never are. `nas` is an ssh alias for the role, not a hostname.

## Docs

- A deferred requirement gets a slice in the [roadmap](roadmap.md). The current slice is in
  [AGENTS.md](../AGENTS.md#current-slice).
- Overrides the org rule's ADRs in `docs/stack.md`: Hearsay records a decision in the docstring of the module it shapes
  (e.g. `hearsay/transcribe.py`, `hearsay/affect.py`) and in the [roadmap](roadmap.md).

## Prose

- Overrides the org rule's "no dates" for a dated measurement that backs a constant (e.g. "Thresholds tuned on
  2026-09-27" in `hearsay/speakers.py`). It's evidence for the value, not history.

## Known deviations

Where Hearsay doesn't follow the org standards yet. These are debt to pay down, not overrides: each one should go.

- Dependencies: FastAPI has no recorded decision saying what it gives. The [roadmap](roadmap.md)'s first slice only
  names it.
- Versions and pins: the deployment runs whatever each host's clone has checked out. [install.md](install.md) updates
  with `git pull`, and `install/nas.sh:105` builds the images from that clone, so no commit is pinned (`:102` only
  records `git describe`). The images build from `python:3.12-slim` (`Dockerfile:1`), a tag with no digest.
- Versions and pins: `fastapi`, `uvicorn`, `pytest` and `httpx2` have no lower bound (`pyproject.toml:6-7`, `:32-33`),
  and no lockfile holds the exact versions (see Python tooling).
- Versions and pins: `hearsay/transcribe.py:144` and `:160` download the Parakeet and pyannote models by name, with no
  revision. CI (`.github/workflows/test.yml:13-15`) and `install/gpu.sh:34` take Python `3.12`, not an exact release
  (in `gpu.sh` also shell.md's pinned-tool rule), and nothing sets `[tool.uv] required-version`, so CI's setup-uv
  takes the latest uv.
- Versions and pins: the annotated tag `v1` isn't a `vX.Y.Z` tag, so it isn't a release (its commit's manifest said
  `0.1.0`). The manifest's version is `0.0.0`, and no CI job would fail a `vX.Y.Z` tag that doesn't match it:
  `test.yml` runs only on pushes to main and on pull requests.
- Structure: `STORED_AUDIO` in `hearsay/capture.py` is never read.
- Privacy: the places tests use coordinates in a real city, one of them named `HOME` (`tests/test_places.py:19-20`,
  `tests/test_portal.py:104-135`), not made-up values.
- Privacy: the [roadmap](roadmap.md) names four people from real conversations (`:210-211`) and cites real
  conversation ids with what happened in them (`:212`, `:229`, `:577`, `:594-597`; also `hearsay/music.py:4`).
- Docs: there's no `docs/SPEC.md`, so what Hearsay is meant to do is spread across [AGENTS.md](../AGENTS.md) and the
  [roadmap](roadmap.md)'s slices, which name no spec requirements. The roadmap is `roadmap.md`, not `ROADMAP.md`,
  and its slices have no `Status:` or `Delivers:` line.
- Deployment repos: the deployment's layout is in this repo: `/mnt/fast/...` paths, the `fast/hearsay` and
  `fast/repos` datasets and `ssh nas`, in `install/`, in `hearsay/label.py:38-43` and `hearsay/transcribe.py:45-50`,
  and in [install.md](install.md), [stream.md](stream.md) and the [roadmap](roadmap.md). These facts belong in a
  future `hearsay-deployment` repo, and the Privacy override above goes with them.
- Prose: README.md's Status, [AGENTS.md](../AGENTS.md) (`:6`, `:30`, `:46`), [stream.md](stream.md) (`:70`),
  [install.md](install.md) (`:219`) and `hearsay/portal.py:12` date when things changed, and the
  [roadmap](roadmap.md)'s slices carry dated Retired, Revised and Shelved notes (e.g. `:16`, `:56`, `:364`). The
  override above covers only a dated measurement behind a constant.
- Containers: images install with `pip` (`Dockerfile:7`, `:16-17`, `:41`), not `uv sync --frozen`, and copy in no uv.
- Containers: `python:3.12-slim` doesn't name the Debian release.
- Containers: the `Dockerfile` has no `USER`; compose sets `568:568`.
- Python tooling: there's no `uv.lock` or `.python-version`. CI installs with `uv pip install -e ".[dev]"`,
  `install/dev.sh:15` with pip and `install/gpu.sh:39` with `uv pip`, not `uv sync --frozen`, and the dev tools are an
  extra, not a `dev` dependency group. It builds with setuptools, not hatchling.
- Python tooling: `make check` runs pytest only. Ruff and mypy aren't configured or run.
- Python layout: the package is `hearsay/`, not `src/hearsay/`. Only `hearsay-label` is a `[project.scripts]` entry;
  `hearsay.reprocess`, `hearsay.check`, `hearsay.transcribe` and `hearsay.enqueue` run as `python -m`, and
  `hearsay.label`'s `main()` reads `sys.argv` by hand, not with argparse.
- Python typing: 20 functions lack a parameter or return annotation, most of them model code
  (`hearsay/affect.py:47-80`, `hearsay/speakers.py:84-124`, `hearsay/transcribe.py:137-229`).
- Python data modeling: records are tuples, and `OperatorInput` (`hearsay/turns.py:56`) is a `NamedTuple` of mutable
  dicts and sets. The package has no frozen dataclass.
- Python data modeling: the closed sets the code compares or parses are bare strings, not `StrEnum`s tested against a
  `CHECK`: `payloads.type` (`hearsay/parse.py:233-241`), `mac_audio.channel` (`hearsay/speakers.py:188-201`),
  `operator_input.kind` (`hearsay/turns.py:227-251`), the owner/not_owner labels (`hearsay/stream.py:82-84`,
  `hearsay/label.py:44`) and `turn_speakers.basis` (`hearsay/stream.py:70`). `marks.kind` and `turn_stutters.kind`
  are only written from Python and compared in SQL, so they need only the `CHECK`.
- Python errors: a missing setting raises `KeyError` (`HEARSAY_DB` and the rest at `hearsay/portal.py:1345`, the
  directories at `hearsay/reprocess.py:248` and `hearsay/check.py:100`, `hearsay/receiver.py:102`,
  `hearsay/enqueue.py:73`), and an empty one `RuntimeError` (`hearsay/portal.py:1339`, `hearsay/receiver.py:85`),
  not `SystemExit`.
- Python testing: test files are one per module (`tests/test_capture.py`, `tests/test_portal.py`, ...), not named for
  the behavior they cover; only `tests/test_inheritance.py` is. `make check` runs `pytest -q`, not `-rs`, so the skips
  don't show.
- Python dependencies: the extras pin exactly (`==`). Only `transformers` in `transcribe` names the break its pin
  avoids.
- Python dependencies: torch comes from the CPU index through `pip --index-url` (`Dockerfile:16`), not
  `[tool.uv.sources]`.
- Shell: `install/lib.sh` is executable and doesn't start with `# shellcheck shell=bash`.
- Shell: `install/ios.sh`'s `devices_json` removes its temp file without a trap.
- Shell: heredocs whose text doesn't interpolate have an unquoted delimiter: `install/ios.sh:20`, `:49`, `:98`, `:131`,
  `:163`, `:173`, `:186` and `install/mac.sh:111`.
- Shell: `install/dev.sh`'s header doesn't say it's safe to re-run.
- Shell: `install/ios.sh` puts the number the operator types into a `sed` program before checking it (`:64`, `:107`).
- Shell: `make check` doesn't run shellcheck, which reports 29 findings.
- SQL: no closed-set column has a `CHECK`; their names are in comments: `payloads.type` and `marks.kind`
  (`hearsay/parse.py:23`, `:105`), `mac_audio.channel` (`hearsay/capture.py:139`), `operator_input.kind` and its label
  `value` (`hearsay/turns.py:50-51`), `turn_stutters.kind` (`hearsay/delivery.py:53`), and `turn_speakers.label` and
  `.basis` (`hearsay/speakers.py:72-73`).
- SQL: the schema declares 17 foreign keys, and no connection turns them on.
- SQL: the portal and the label and transcribe tools read the database while `reprocess` writes it, and it isn't in WAL.

"""Words Parakeet is nudged toward while it transcribes: names, jargon,
anything it keeps mishearing.

The operator types them on the portal's Words page. They don't reach
transcription until the operator presses Update: that writes them, as they
were then, to labels/words.json in one atomic rename. Every Update changes
the transcription settings, so every conversation is transcribed again on
the next run (hearsay/transcribe.py). Edits wait in labels/words.draft.txt
until then, for as long as it takes.
"""

import json
import os
from pathlib import Path


def tidy(lines: list[str]) -> list[str]:
    """One word or phrase per entry: spaces collapsed, blanks and exact duplicates
    dropped, in alphabetical order regardless of case. Case itself is kept:
    Parakeet writes capitals, so the spelling given is the one it leans toward."""
    words = {" ".join(line.split()) for line in lines} - {""}
    return sorted(words, key=lambda w: (w.casefold(), w))


def read_committed(labels_dir: Path) -> dict[str, list[str]]:
    path = labels_dir / "words.json"
    if not path.exists():
        return {"typed": []}
    return json.loads(path.read_text())


def boosted(committed: dict[str, list[str]]) -> list[str]:
    return committed["typed"]


def read_draft(labels_dir: Path) -> list[str] | None:
    path = labels_dir / "words.draft.txt"
    return path.read_text().splitlines() if path.exists() else None


def write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_draft(labels_dir: Path, typed: list[str]) -> None:
    """A draft the same as what's committed is no draft."""
    path = labels_dir / "words.draft.txt"
    if typed == read_committed(labels_dir)["typed"]:
        path.unlink(missing_ok=True)
        return
    write_atomically(path, "".join(f"{w}\n" for w in typed))


def commit(labels_dir: Path, typed: list[str]) -> None:
    write_atomically(labels_dir / "words.json", json.dumps({"typed": typed}, indent=1) + "\n")
    (labels_dir / "words.draft.txt").unlink(missing_ok=True)

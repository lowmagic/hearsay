"""How much each turn sounds like a song rather than someone talking.

A recording near a speaker playing music sometimes gets the song's words
transcribed as turns: five rap lines at a party in c20000101T000012Z, tagged
_music by the operator. Music under people talking is real speech and has to
stay; only turns whose words are the song's should go.

Scored with AST (MIT's Audio Spectrogram Transformer fine-tuned on AudioSet,
16x16 patches), as sigmoids of three of its classes: speech (Speech), singing
(the highest of Singing and its children, Rapping among them; not Humming,
which has no words) and music (Music). AST over PANNs or YAMNet: it runs on
the transformers already in the worker image, where PANNs would add
torchlibrosa and librosa, and YAMNet TensorFlow; and the earlier music
check (roadmap, slice 21) used AST too. The 16x16 model over the
better-known 10x10 (AudioSet mAP 0.442 against 0.459): 2.7 times faster on
CPU, 0.25 s a turn on four cores of the dev box, and every turn is scored,
about 1,000 an hour of conversation.

Every turn of at least MIN_SECONDS with MIN_COVERAGE of real audio is
scored, the owner's too, so any threshold's cost to real speech can be
measured. Turns are scored on their first MAX_SECONDS, the model's whole
input. The scores stay in the database and aren't streamed.

A turn is a song's words (SONG_SQL) when AST hears it as music and little
speech, and it lasts long enough to judge: it becomes _music, left out of
the stream, unless the operator tagged it or answered `hearsay-label music`
for it, whose answer wins either way (hearsay/turns.py, categories). Music
alone didn't separate song from talk: below 0.55 the operator's answers were
mostly talk, people speaking quietly over a band. Speech under SONG_MAX_SPEECH
took most of that talk out, and SONG_MIN_SECONDS the rest, 0.6-0.8 s turns
whose padding to MAX_SECONDS keeps their speech score low. Chosen 2026-10-30 on
114 answered turns (25 song, 89 talk): the rule caught 18 songs and 2 talk.
On all turns it drops 30, 1.8 minutes, 3 of them the owner's by voice.

The model's license is BSD-3-Clause.
"""

import sqlite3
from pathlib import Path

from hearsay.assemble import cut, place_bursts, timestamp
from hearsay.cache import Cache, audio_key
from hearsay.speakers import MIN_COVERAGE

# Cache key prefix: the model revision and the classes kept. Change it with either.
MUSIC_VERSION = "music-ast-16-16-bf4d03f3"
# AST's input is 1024 frames of 10 ms; longer audio is cut off, shorter padded.
MAX_SECONDS = 10.24
# A word at most: too little sound to tell song from speech.
MIN_SECONDS = 0.5
SONG_MIN_MUSIC = 0.3
SONG_MAX_SPEECH = 0.22
SONG_MIN_SECONDS = 1.0
SINGING = ("Singing", "Choir", "Yodeling", "Chant", "Mantra", "Male singing", "Female singing", "Child singing",
           "Synthetic singing", "Rapping")

# Turns that are a song's words by their scores alone.
SONG_SQL = f"""
SELECT m.turn_id FROM turn_music m JOIN turns t ON t.turn_id = m.turn_id
WHERE m.music >= {SONG_MIN_MUSIC} AND m.speech < {SONG_MAX_SPEECH} AND t.end - t.start >= {SONG_MIN_SECONDS}
"""

SCHEMA = """
DROP TABLE IF EXISTS turn_music;
-- AudioSet class scores per turn, 0-1 (hearsay/music.py); not streamed.
CREATE TABLE turn_music (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    speech REAL NOT NULL,
    singing REAL NOT NULL,           -- highest of Singing and its children, Rapping among them
    music REAL NOT NULL
);
"""


def load_model(model_dir: Path):
    # Imported here: torch and transformers are heavy, and only this step needs them.
    from transformers import ASTFeatureExtractor, ASTForAudioClassification

    network = ASTForAudioClassification.from_pretrained(str(model_dir)).eval()
    index = {name: i for i, name in network.config.id2label.items()}
    classes = (index["Speech"], [index[name] for name in SINGING], index["Music"])
    return ASTFeatureExtractor.from_pretrained(str(model_dir)), network, classes


def score(model, pcm: bytes, cache: Cache | None = None) -> tuple[float, float, float]:
    """(speech, singing, music); with a cache, audio scored before isn't scored again."""
    import numpy as np
    import torch

    key = audio_key(MUSIC_VERSION, pcm)
    cached = cache.get(key) if cache else None
    if cached is not None:
        return tuple(np.frombuffer(cached, dtype="<f4").tolist())
    extractor, network, (speech, singing, music) = model
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    with torch.inference_mode():
        inputs = extractor(samples, sampling_rate=16000, return_tensors="pt")
        p = torch.sigmoid(network(**inputs).logits[0])
    values = np.array([p[speech], p[singing].max(), p[music]], dtype="<f4")
    if cache:
        cache.put(key, values.tobytes())
    return tuple(values.tolist())


def score_music(raw_dir: Path, db_path: Path, model_dir: Path, cache: Cache | None = None) -> dict:
    db = sqlite3.connect(db_path)
    try:
        turns = db.execute(
            "SELECT t.turn_id, ca.zero_at, t.start, t.end FROM turns t"
            " JOIN conversation_audio ca ON ca.conversation_id = t.conversation_id"
            " WHERE t.end - t.start >= ? ORDER BY t.turn_id",
            (MIN_SECONDS,),
        ).fetchall()
        bursts = place_bursts(db)
        model = load_model(model_dir) if turns else None
        rows = []
        for turn_id, zero_at, start, end in turns:
            pcm, coverage = cut(raw_dir, bursts, timestamp(zero_at) + start, min(end - start, MAX_SECONDS))
            if coverage >= MIN_COVERAGE:
                rows.append((turn_id, *(round(v, 3) for v in score(model, pcm, cache))))
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO turn_music VALUES (?,?,?,?)", rows)
        db.commit()
    finally:
        db.close()
    return {"turns": len(turns), "scored": len(rows), "singing_over_half": sum(1 for row in rows if row[2] >= 0.5)}

"""Enqueue Hearsay's work on corral (github.com/junglestyle/corral).

CHAIN is the pipeline: assemble new audio into WAVs on the NAS, transcribe
them on the GPU host, fold the transcripts into the stream on the NAS (FOLD),
then let Idea Machine read it. It runs once a conversation ends, not on every
upload: the capture receiver enqueues CHECK after each upload, which enqueues
CHAIN only when a conversation has ended that the database doesn't have as
ended yet (hearsay/check.py). An hourly cron enqueues CHECK too, for uploads
that couldn't reach corral (install/nas.sh). Each step has its own coalescing
key, so a burst queues one of each, and one enqueued during a run queues one
more behind it. Reprocess appears under two keys, so a queued fold never
swallows a new first step.

Transcription takes a backlog in chunks and its `then` runs after each one,
so the fold is held (corral's wait_seconds) while transcription or the
assembly before it would enqueue it: a backlog is folded once at the end,
and at least every 30 minutes while it lasts.

A portal edit enqueues APPLY (hearsay/portal.py): a full reprocess under the
fold's key, so one run serves both, held until the edits have stopped for 10
minutes, and at most 30 minutes after the first; the portal's Apply now
releases it at once. A nightly cron enqueues CHAIN itself, so what's due by
time alone (a hold ending, sealing, retention) happens on a day nothing is
recorded or edited too, and anything a check missed is transcribed by then.
A Words update enqueues CHAIN as well: it changes what transcription is
nudged toward, so everything not yet sealed is transcribed again.
"""

import argparse
import json
import os
import urllib.request

IDEAMACHINE = {"kind": "ideamachine.run", "needs": ["nas"], "key": "ideamachine.run"}
FOLD = {"kind": "hearsay.reprocess", "needs": ["nas"], "key": "hearsay.reprocess:transcripts",
        "wait_seconds": 1800, "then": [IDEAMACHINE]}
CHAIN = {"kind": "hearsay.reprocess", "needs": ["nas"], "key": "hearsay.reprocess:audio",
         "then": [{"kind": "hearsay.transcribe", "needs": ["gpu"], "key": "hearsay.transcribe", "then": [FOLD]}]}
CHECK = {"kind": "hearsay.check", "needs": ["nas"], "key": "hearsay.check"}
# A portal edit: the same key as FOLD, so one full reprocess serves both; 10 min quiet so a session is one run.
APPLY = FOLD | {"quiet_seconds": 600}

SPECS = {"check": CHECK, "chain": CHAIN}


def call(corral_url: str, corral_token: str, path: str, body: dict | None = None) -> tuple[dict, int]:
    request = urllib.request.Request(
        corral_url.rstrip("/") + path,
        data=json.dumps(body or {}).encode(),
        headers={"Authorization": f"Bearer {corral_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response), response.status


def post(corral_url: str, corral_token: str, spec: dict) -> tuple[int, bool]:
    """POST /jobs. Returns the job id and whether it was created rather than coalesced."""
    job, status = call(corral_url, corral_token, "/jobs", spec)
    return job["id"], status == 201


def release(corral_url: str, corral_token: str, job_id: int) -> None:
    """POST /jobs/{id}/release."""
    call(corral_url, corral_token, f"/jobs/{job_id}/release")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hearsay.enqueue", description="Enqueue Hearsay's work on corral.")
    parser.add_argument("what", choices=SPECS, help="check: whether a conversation has ended (hourly);"
                        " chain: the whole pipeline (nightly)")
    what = parser.parse_args().what
    job_id, created = post(os.environ["CORRAL_URL"], os.environ["CORRAL_TOKEN"], SPECS[what])
    print(f"enqueued {what}: job {job_id}" if created else f"{what} was already queued: job {job_id}")


if __name__ == "__main__":
    main()

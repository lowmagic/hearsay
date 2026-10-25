"""Enqueue the pipeline on corral (github.com/junglestyle/corral): assemble
new audio into WAVs on the NAS, transcribe them on the GPU host, fold the
transcripts into the stream on the NAS, then let Idea Machine read it.

The capture receiver calls this after every upload, and an hourly cron runs it
as a safety net (install/nas.sh). Each step has its own coalescing key, so a
burst of uploads queues one chain, and an upload during a run queues one more
behind it. Reprocess appears twice, under two keys, so a queued second pass
never swallows a new first one.
"""

import json
import os
import urllib.request

CHAIN = {
    "kind": "hearsay.reprocess", "needs": ["nas"], "key": "hearsay.reprocess:audio",
    "then": [{
        "kind": "hearsay.transcribe", "needs": ["gpu"], "key": "hearsay.transcribe",
        "then": [{
            "kind": "hearsay.reprocess", "needs": ["nas"], "key": "hearsay.reprocess:transcripts",
            "then": [{"kind": "ideamachine.run", "needs": ["nas"], "key": "ideamachine.run"}],
        }],
    }],
}


def enqueue(corral_url: str, corral_token: str) -> tuple[int, bool]:
    """Returns the first step's job id, and whether it's new rather than already queued."""
    request = urllib.request.Request(
        corral_url.rstrip("/") + "/jobs",
        data=json.dumps(CHAIN).encode(),
        headers={"Authorization": f"Bearer {corral_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["id"], response.status == 201


def main() -> None:
    job_id, created = enqueue(os.environ["CORRAL_URL"], os.environ["CORRAL_TOKEN"])
    print(f"enqueued job {job_id}" if created else f"job {job_id} was already queued")


if __name__ == "__main__":
    main()

"""The capture receiver: authenticate, then write the body to disk verbatim.

It takes our own recorders' uploads (hearsay/capture.py) and listens on the
tailnet only, since its bodies are audio. Its token is a bearer header,
never written to disk or logged.

Omi's webhooks used to land here too, through a public Cloudflare tunnel;
both were removed once Omi was no longer used (its last payload is from
2026-09-30). Those raw payloads are kept and still parsed (hearsay/parse.py).

After each upload it enqueues the pipeline on corral (hearsay/enqueue.py),
once the response is sent. If corral can't be reached, the hourly cron's
enqueue picks the upload up instead.

Rejected requests are logged (whether a token was sent, and the client
address), so a sender with a wrong token shows up in the journal instead of
vanishing.
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Response

from hearsay import enqueue

log = logging.getLogger("hearsay.receiver")


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def authorized(request: Request, kind: str, sent: str, token: str) -> bool:
    if hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
        return True
    log.warning(
        "rejected %s upload: %s token, client %s",
        kind,
        "wrong" if sent else "no",
        request.client.host if request.client else "unknown",
    )
    return False


async def store(raw_dir: Path, request: Request, kind: str) -> None:
    body = await request.body()
    now = datetime.now(timezone.utc)
    day_dir = raw_dir / kind / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:8]}"

    sidecar = {
        "received_at": now.isoformat(),
        "webhook_type": kind,
        "method": request.method,
        "path": request.url.path,
        "query": [[k, v] for k, v in request.query_params.multi_items() if k != "token"],
        "headers": [[k, v] for k, v in request.headers.items() if k != "authorization"],
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "body_file": f"{stem}.body",
    }
    # Body first: a sidecar only exists once its body is complete.
    write_atomically(day_dir / f"{stem}.body", body)
    write_atomically(day_dir / f"{stem}.json", json.dumps(sidecar, indent=2).encode())


def create_capture_app(raw_dir: Path, token: str, after_upload: Callable[[], None] | None = None) -> FastAPI:
    # Refused here, so no caller can build a receiver open to anyone.
    if not token:
        raise RuntimeError("the capture token is empty")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/capture")
    async def capture(request: Request, background: BackgroundTasks) -> Response:
        sent = request.headers.get("authorization", "")
        if not authorized(request, "capture", sent, token):
            return Response(status_code=401)
        await store(raw_dir, request, "capture")
        if after_upload:
            background.add_task(after_upload)
        return Response(status_code=200)

    return app


def capture_app_from_env() -> FastAPI:
    token = os.environ["HEARSAY_CAPTURE_TOKEN"]
    raw_dir = Path(os.environ["HEARSAY_RAW_DIR"])
    corral_url = os.environ.get("CORRAL_URL")
    if not corral_url:
        # A local dev receiver (install/dev.sh) has no corral.
        log.warning("CORRAL_URL isn't set: uploads are stored but nothing is enqueued")
        return create_capture_app(raw_dir, token)
    corral_token = os.environ["CORRAL_TOKEN"]

    def enqueue_pipeline() -> None:
        try:
            enqueue.enqueue(corral_url, corral_token)
        except OSError as e:  # urllib's errors, refused connections and timeouts included
            log.warning("couldn't enqueue on corral (%s); the hourly enqueue will pick this upload up", e)

    return create_capture_app(raw_dir, token, enqueue_pipeline)


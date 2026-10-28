#!/usr/bin/env bash
# Dev box GPU worker: transcription (hearsay/transcribe.py) as a kind on the
# dev box's corral worker, which runs it when the pipeline reaches it. It
# transcribes and diarizes conversation WAVs from the NAS and writes the
# transcripts back. Run as your normal user on the dev box, after
# install/nas.sh. Safe to re-run; also how updates are deployed.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

GPU_VENV="$HOME/.local/share/hearsay/gpu-venv"
DEV_ENV="$HOME/.config/hearsay/dev.env"
CORRAL_CONFIG="$HOME/.config/corral"
DIARIZATION_MODEL="pyannote/speaker-diarization-community-1"

[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root: the corral worker and its config are per user"

say "Checking the GPU"
# A driver/library version mismatch after a driver update also lands here.
nvidia-smi -L >/dev/null 2>&1 || die "nvidia-smi fails. If it reports a version mismatch, reboot, then re-run."
nvidia-smi -L

say "Checking ssh to the NAS works unattended"
# Corral's worker has no terminal and no ssh agent, so the key must work without either.
env -u SSH_AUTH_SOCK ssh -o BatchMode=yes nas true \
    || die "'ssh nas' needs a key that works without a passphrase prompt or agent. Fix ~/.ssh, then re-run."

say "Checking the NAS side (install/nas.sh)"
ssh -o BatchMode=yes nas "test -w '$NAS_TRANSCRIPTS' && test -r '$NAS_ROOT/db/hearsay.sqlite'" \
    || die "the NAS isn't ready: run install/nas.sh there, then a reprocess, then re-run this."

say "Python 3.12 environment in $GPU_VENV"
# 3.12: the Python the transcribe extras are pinned and tested against.
command -v uv >/dev/null || die "install uv first: https://docs.astral.sh/uv/getting-started/installation/"
uv python install 3.12
[ -x "$GPU_VENV/bin/python" ] || uv venv --python 3.12 "$GPU_VENV"
# A copy, not editable: an editable install points at this clone, and broke
# every transcription for a day when the clone moved. Reinstalled every run,
# since the version doesn't change with the code.
VIRTUAL_ENV="$GPU_VENV" uv pip install --quiet --reinstall-package hearsay "$REPO_DIR[transcribe]"
# From /, so the clone in the working directory can't stand in for the install.
(cd / && "$GPU_VENV/bin/python" -c "import hearsay.transcribe") || die "the GPU environment can't import hearsay.transcribe"
"$GPU_VENV/bin/python" -c "import torch; assert torch.cuda.is_available(), 'CUDA not available to torch'"

say "Hugging Face token in $DEV_ENV"
if [ -z "$(env_get "$DEV_ENV" HF_TOKEN)" ]; then
    # Manual: needs the operator's Hugging Face account.
    cat <<EOF
The diarization model is gated. In a browser, logged in to Hugging Face:
  1. Accept the terms at https://huggingface.co/$DIARIZATION_MODEL
  2. Create a read token at https://huggingface.co/settings/tokens
EOF
    read -rsp "Paste the token (hf_...): " token
    echo
    mkdir -p "$(dirname "$DEV_ENV")"
    env_ensure "$DEV_ENV" HF_TOKEN "$token"
fi

say "Checking the token can reach $DIARIZATION_MODEL"
HF_TOKEN="$(env_get "$DEV_ENV" HF_TOKEN)" "$GPU_VENV/bin/python" -c "
import os, sys
from huggingface_hub import auth_check
try:
    auth_check('$DIARIZATION_MODEL', token=os.environ['HF_TOKEN'])
except Exception as e:
    sys.exit(f'no access ({type(e).__name__}): accept the terms at https://huggingface.co/$DIARIZATION_MODEL')
" || die "fix model access, then re-run"

say "Transcribe kind on the corral worker (github.com/junglestyle/corral)"
# Corral's worker is set up from its own repo, as a user unit offering gpu.
systemctl --user is-active -q corral-worker && [ -f "$CORRAL_CONFIG/worker.toml" ] \
    || die "no corral worker running from $CORRAL_CONFIG. Set one up as a user unit (docs/running.md in
junglestyle/corral), then re-run."
grep -q '"gpu"' "$CORRAL_CONFIG/worker.toml" \
    || die "the corral worker in $CORRAL_CONFIG/worker.toml doesn't offer \"gpu\"; add it to offers, then re-run"
mkdir -p "$CORRAL_CONFIG/kinds.d"
# A shell only to load the Hugging Face token; exec, so the worker's kill reaches Python.
if write_if_changed "$CORRAL_CONFIG/kinds.d/hearsay.toml" <<EOF
# Written by hearsay's install/gpu.sh on every run.
[kinds."hearsay.transcribe"]
argv = ["/bin/sh", "-c", "set -a; . $DEV_ENV; exec $GPU_VENV/bin/python -m hearsay.transcribe"]
EOF
then
    systemctl --user restart corral-worker
    echo "kinds updated, worker restarted"
fi
echo "kinds in $CORRAL_CONFIG/kinds.d/hearsay.toml"
# Linger keeps the user's corral worker running when you're not logged in.
if ! loginctl show-user "$USER" -p Linger | grep -q yes; then
    loginctl enable-linger "$USER" || die "run: sudo loginctl enable-linger $USER, then re-run this"
fi

say "First run (transcribes everything pending; can take a few minutes)"
# Run here, not through corral, to see it work. The worker counts it as
# foreign GPU use and holds off GPU jobs until it ends.
(set -a; . "$DEV_ENV"; "$GPU_VENV/bin/python" -m hearsay.transcribe) || die "the run failed (above)"

say "Done. Uploads now enqueue transcription through corral (journalctl --user -u corral-worker)."

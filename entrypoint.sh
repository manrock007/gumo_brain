#!/usr/bin/env bash
set -euo pipefail

# Git identity + HTTPS auth for clones/pushes done by the service and by Claude
git config --global user.name "${CTRLLOOP_GIT_NAME:-ctrlloop}"
git config --global user.email "${CTRLLOOP_GIT_EMAIL:-engine@ctrlloop.local}"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8010

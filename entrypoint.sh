#!/usr/bin/env bash
set -euo pipefail

# Git identity + HTTPS auth for clones/pushes done by the service and by Claude
git config --global user.name "gumo-brain"
git config --global user.email "brain@gumo.co.in"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8010

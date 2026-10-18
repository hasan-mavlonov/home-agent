#!/usr/bin/env bash
# Update to the latest code and restart: scripts/update.sh
# Database migrations run automatically at startup; your data and .env are untouched.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "You have local changes to tracked files; commit or stash them first:" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi
git pull --ff-only
uv sync --frozen --no-dev
if [[ "${BROWSER_ENABLED:-true}" != "false" ]]; then
  uv run --no-dev playwright install chromium >/dev/null 2>&1 || echo "Note: could not update Chromium (JavaScript rendering may be unavailable)"
fi
"$REPO/scripts/service.sh" restart

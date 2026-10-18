#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon). Safe to re-run.
#   scripts/install-macos.sh [--no-browser] [--no-service] [--boot]
# Installs: uv (via Homebrew, if missing), Python dependencies (pinned in uv.lock),
# Playwright's Chromium (for JavaScript-heavy pages), .env + secret key, web password,
# and the launchd service. Does not touch Caddy, other services, or system settings.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
WITH_BROWSER=1
WITH_SERVICE=1
SERVICE_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-browser) WITH_BROWSER=0 ;;
    --no-service) WITH_SERVICE=0 ;;
    --boot) SERVICE_ARGS+=(--boot) ;;
    *) echo "unknown option $arg" >&2; exit 1 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is for macOS. On Linux, follow 'Install (Linux, systemd)' in README.md." >&2
  exit 1
fi
[[ "$(uname -m)" == "arm64" ]] || echo "Note: this is not an Apple Silicon Mac; everything works, local models will be slow."

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh (then re-run this script)" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv (Python package manager) with Homebrew"
  brew install uv
fi

echo "==> Installing Python and pinned dependencies (uv.lock)"
uv sync --frozen --no-dev

if [[ "$WITH_BROWSER" == "1" ]]; then
  echo "==> Installing Chromium for JavaScript-heavy pages (about 150 MB; skip with --no-browser)"
  uv run --no-dev playwright install chromium || echo "Chromium install failed; JavaScript rendering stays off until it succeeds (re-run: uv run playwright install chromium)"
fi

echo "==> Creating .env and secret key"
uv run --no-dev home-agent init

if ! uv run --no-dev python -c "
from home_agent.config import load_settings
from home_agent.db import Database
from home_agent.security.auth import AuthService
s = load_settings(); db = Database(s.db_path); db.migrate()
raise SystemExit(0 if AuthService(db).password_is_set() else 1)" 2>/dev/null; then
  echo "==> Choose the password for the web interface"
  uv run --no-dev home-agent set-password
fi

echo "==> This Mac"
uv run --no-dev home-agent recommend-model || true

if [[ "$WITH_SERVICE" == "1" ]]; then
  echo "==> Installing the background service"
  "$REPO/scripts/service.sh" install "${SERVICE_ARGS[@]+"${SERVICE_ARGS[@]}"}"
fi

echo
uv run --no-dev home-agent doctor || true
cat <<EOF

Next steps
  1. Put your API keys in $REPO/.env (ANTHROPIC_API_KEY, BRAVE_API_KEY or TAVILY_API_KEY), then:
       scripts/service.sh restart
  2. Open http://127.0.0.1:$(grep -E '^HOME_AGENT_PORT=' .env | cut -d= -f2 || echo 18789) on this Mac.
  3. Remote access over Tailscale: scripts/tailscale-serve.sh
EOF

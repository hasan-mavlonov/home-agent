#!/usr/bin/env bash
# Publish Home Agent privately to your tailnet with HTTPS (Tailscale Serve).
#   scripts/tailscale-serve.sh [HTTPS_PORT]      default HTTPS port: 8443
#   scripts/tailscale-serve.sh --off [HTTPS_PORT]
# Adds ONE handler on its own port and refuses to replace an existing handler on that
# port, so other Serve mappings (and Caddy) are left alone. Not public: Funnel is not used.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OFF=0
if [[ "${1:-}" == "--off" ]]; then OFF=1; shift; fi
HTTPS_PORT="${1:-8443}"
APP_PORT="$(grep -E '^HOME_AGENT_PORT=' "$REPO/.env" 2>/dev/null | cut -d= -f2)"
APP_PORT="${APP_PORT:-18789}"

TS="$(command -v tailscale || true)"
if [[ -z "$TS" && -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
[[ -n "$TS" ]] || { echo "Tailscale CLI not found. Install Tailscale (https://tailscale.com/download/mac) and sign in." >&2; exit 1; }

if [[ "$OFF" == "1" ]]; then
  "$TS" serve --https="$HTTPS_PORT" off
  echo "Removed the Home Agent handler on port $HTTPS_PORT."
  exit 0
fi

status_json="$("$TS" serve status --json 2>/dev/null || echo '{}')"
if python3 - "$HTTPS_PORT" "$status_json" <<'PY'
import json, sys
port, raw = sys.argv[1], sys.argv[2]
try:
    data = json.loads(raw or "{}")
except ValueError:
    data = {}
tcp = data.get("TCP") or {}
web = data.get("Web") or {}
used = port in tcp or any(key.endswith(":" + port) for key in web)
sys.exit(0 if used else 1)
PY
then
  echo "Port $HTTPS_PORT already has a Tailscale Serve handler:" >&2
  "$TS" serve status >&2 || true
  echo "Pick another port, e.g.: scripts/tailscale-serve.sh 9443" >&2
  exit 1
fi

"$TS" serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$APP_PORT"
"$TS" serve status || true

dns_name="$("$TS" status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
if [[ -n "$dns_name" ]]; then
  url="https://$dns_name:$HTTPS_PORT"
  echo
  echo "Home Agent is available to devices on your tailnet at: $url"
  echo "Set HOME_AGENT_PUBLIC_URL=$url in .env and run scripts/service.sh restart."
fi

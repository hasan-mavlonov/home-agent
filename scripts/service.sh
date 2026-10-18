#!/usr/bin/env bash
# Manage the Home Agent background service.
#   macOS: launchd. Default is a LaunchAgent (starts when you log in, no sudo).
#          --boot installs a LaunchDaemon instead (starts at boot, before login; needs sudo).
#   Linux: systemd user service.
# Usage: scripts/service.sh install [--boot] | uninstall | start | stop | restart | status | logs
set -euo pipefail

LABEL="com.homeagent.server"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$REPO/.venv/bin/home-agent"
ENV_FILE="${HOME_AGENT_ENV_FILE:-$REPO/.env}"

env_value() {  # read a value from .env without sourcing it
  local key="$1" default="$2" value
  value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  echo "${value:-$default}"
}

PORT="$(env_value HOME_AGENT_PORT 18789)"
DATA_DIR="$(env_value HOME_AGENT_DATA_DIR "")"
if [[ -z "$DATA_DIR" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then DATA_DIR="$HOME/Library/Application Support/HomeAgent"; else DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/home-agent"; fi
fi
LOG_DIR="$DATA_DIR/logs"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DAEMON_PLIST="/Library/LaunchDaemons/$LABEL.plist"

die() { echo "error: $*" >&2; exit 1; }

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

write_plist() {  # $1 = destination, $2 = "daemon" or "agent"
  local dest="$1" kind="$2" user_keys=""
  if [[ "$kind" == "daemon" ]]; then
    user_keys="  <key>UserName</key><string>$(xml_escape "$(id -un)")</string>
  <key>GroupName</key><string>staff</string>"
  fi
  cat >"$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
$user_keys
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$BIN")</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$REPO")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$(xml_escape "$HOME")</string>
    <key>HOME_AGENT_ENV_FILE</key><string>$(xml_escape "$ENV_FILE")</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_DIR/launchd.out.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_DIR/launchd.err.log")</string>
</dict>
</plist>
EOF
  plutil -lint "$dest" >/dev/null
}

mac_domain() {
  if [[ -f "$DAEMON_PLIST" ]]; then echo "system"; else echo "gui/$(id -u)"; fi
}

mac_sudo() { if [[ "$(mac_domain)" == "system" ]]; then sudo "$@"; else "$@"; fi; }

health() {
  local _
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      echo "Home Agent is up: http://127.0.0.1:$PORT"
      return 0
    fi
    sleep 1
  done
  echo "Home Agent did not answer on port $PORT yet. Check: scripts/service.sh logs" >&2
  return 1
}

macos() {
  local cmd="$1"; shift || true
  case "$cmd" in
    install)
      [[ -x "$BIN" ]] || die "missing $BIN - run scripts/install-macos.sh first"
      mkdir -p "$LOG_DIR"
      if [[ "${1:-}" == "--boot" ]]; then
        [[ -f "$AGENT_PLIST" ]] && { launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; rm -f "$AGENT_PLIST"; }
        local tmp; tmp="$(mktemp)"
        write_plist "$tmp" daemon
        sudo install -o root -g wheel -m 644 "$tmp" "$DAEMON_PLIST"
        rm -f "$tmp"
        sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
        sudo launchctl bootstrap system "$DAEMON_PLIST"
        echo "Installed LaunchDaemon: starts at boot (after FileVault unlock), runs as $(id -un)."
      else
        [[ -f "$DAEMON_PLIST" ]] && die "a boot-time LaunchDaemon is installed; run 'scripts/service.sh uninstall' first"
        mkdir -p "$(dirname "$AGENT_PLIST")"
        write_plist "$AGENT_PLIST" agent
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"
        launchctl enable "gui/$(id -u)/$LABEL"
        echo "Installed LaunchAgent: starts when you log in and keeps running after you close Terminal."
      fi
      health || true
      ;;
    uninstall)
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      rm -f "$AGENT_PLIST"
      if [[ -f "$DAEMON_PLIST" ]]; then sudo launchctl bootout "system/$LABEL" 2>/dev/null || true; sudo rm -f "$DAEMON_PLIST"; fi
      echo "Service removed (your data in $DATA_DIR is kept)."
      ;;
    start)
      local plist="$AGENT_PLIST"; [[ "$(mac_domain)" == "system" ]] && plist="$DAEMON_PLIST"
      [[ -f "$plist" ]] || die "not installed - run: scripts/service.sh install"
      mac_sudo launchctl bootstrap "$(mac_domain)" "$plist" 2>/dev/null || mac_sudo launchctl kickstart "$(mac_domain)/$LABEL"
      health
      ;;
    stop)
      mac_sudo launchctl bootout "$(mac_domain)/$LABEL" 2>/dev/null || true
      echo "Stopped. It starts again at next login/boot, or with: scripts/service.sh start"
      ;;
    restart)
      if ! mac_sudo launchctl kickstart -k "$(mac_domain)/$LABEL" 2>/dev/null; then
        macos start
        return
      fi
      health
      ;;
    status)
      mac_sudo launchctl print "$(mac_domain)/$LABEL" 2>/dev/null | grep -E "^\s*(state|pid|last exit code|runs) =" || echo "not loaded"
      curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" && echo || echo "not answering on port $PORT"
      ;;
    logs)
      tail -n 100 -F "$LOG_DIR/home-agent.log" "$LOG_DIR/launchd.err.log"
      ;;
    *) die "unknown command '$cmd'" ;;
  esac
}

linux() {
  local cmd="$1"; shift || true
  local unit_dir="$HOME/.config/systemd/user" unit="home-agent.service"
  case "$cmd" in
    install)
      [[ -x "$BIN" ]] || die "missing $BIN - run: uv sync --frozen"
      mkdir -p "$unit_dir" "$LOG_DIR"
      sed -e "s#@REPO@#$REPO#g" -e "s#@ENV_FILE@#$ENV_FILE#g" "$REPO/deploy/systemd/home-agent.service" >"$unit_dir/$unit"
      systemctl --user daemon-reload
      systemctl --user enable --now "$unit"
      echo "Installed systemd user service. To start it at boot without logging in: sudo loginctl enable-linger $(id -un)"
      health || true
      ;;
    uninstall) systemctl --user disable --now "$unit" || true; rm -f "$unit_dir/$unit"; systemctl --user daemon-reload ;;
    start) systemctl --user start "$unit"; health ;;
    stop) systemctl --user stop "$unit" ;;
    restart) systemctl --user restart "$unit"; health ;;
    status) systemctl --user --no-pager status "$unit" || true ;;
    logs) journalctl --user -u "$unit" -n 100 -f ;;
    *) die "unknown command '$cmd'" ;;
  esac
}

[[ $# -ge 1 ]] || die "usage: scripts/service.sh install [--boot] | uninstall | start | stop | restart | status | logs"
case "$(uname -s)" in
  Darwin) macos "$@" ;;
  Linux) linux "$@" ;;
  *) die "unsupported OS" ;;
esac

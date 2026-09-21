#!/bin/bash
# discord-rpc installer for macOS (silver MacBook, Mac mini).
#
#   bash install.sh           install/update + start at login, then verify
#   bash install.sh --status  is it running? last log lines
#   bash install.sh --uninstall stop it and remove the login item (keeps the timer)
#
# Env overrides (mainly for tests): PYTHON, DISCORD_RPC_SKIP_CHECK=1
set -euo pipefail

LABEL="com.chun.discord-rpc"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/discord-rpc"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOG="$HOME/Library/Logs/discord-rpc.log"
DOMAIN="gui/$(id -u)"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then printf '%s' "$PYTHON"; return; fi
  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [[ -x "$p" ]] && { printf '%s' "$p"; return; }
  done
  # /usr/bin/python3 is only a stub that pops an installer unless the
  # Command Line Tools are present, so only use it when they are.
  if [[ -x /usr/bin/python3 ]] && xcode-select -p >/dev/null 2>&1; then
    printf '%s' /usr/bin/python3; return
  fi
  return 1
}

stop_agent() { launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true; }

case "${1:-}" in
  --status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      say "agent: loaded ($LABEL)"
    else
      say "agent: NOT loaded"
    fi
    [[ -f "$LOG" ]] && { say "--- last log lines ($LOG)"; tail -n 8 "$LOG"; }
    exit 0 ;;
  --uninstall)
    stop_agent
    rm -f "$PLIST"
    say "uninstalled: agent stopped, login item removed."
    say "kept: $DEST (script + timer). Delete that folder yourself if you want it gone."
    exit 0 ;;
  "") ;;
  *) die "unknown option: $1 (use --status or --uninstall)" ;;
esac

[[ -f "$HERE/presence.py" ]] || die "presence.py must sit next to install.sh"
PY="$(find_python)" || die "no python3 found. Install one with: brew install python"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "$PY is older than 3.8"
say "python: $PY"

mkdir -p "$DEST" "$AGENTS" "$(dirname "$LOG")"
cp "$HERE/presence.py" "$DEST/presence.py"
chmod 755 "$DEST/presence.py"
say "installed: $DEST/presence.py"

# Stop any running copy before the check so two presences never compete.
stop_agent

if [[ "${DISCORD_RPC_SKIP_CHECK:-0}" != "1" ]]; then
  say "checking against the Discord app (20 s)…"
  if ! "$PY" "$DEST/presence.py" --check; then
    die "the check failed — is the Discord desktop app open and signed in? Nothing was started."
  fi
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/presence.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid: $PLIST"

BEFORE=0
[[ -f "$LOG" ]] && BEFORE=$(wc -l < "$LOG" | tr -d ' ')
launchctl bootstrap "$DOMAIN" "$PLIST"
say "started: $LABEL (runs at every login, restarts if it stops)"

# Confirm from the log rather than trusting launchctl's exit code.
for _ in $(seq 1 20); do
  # only lines written after this start count, not an old install's
  if [[ -f "$LOG" ]] && tail -n +"$((BEFORE + 1))" "$LOG" | grep -q "presence set via"; then
    say "verified: the agent set your presence. Log: $LOG"
    exit 0
  fi
  sleep 1
done
say "WARNING: no 'presence set' line in $LOG after 20 s. Run: bash install.sh --status"
exit 1

#!/usr/bin/env bash
# One-shot setup of a personal assistant on macOS: Telegram listener, notify,
# health check, memory skeleton, brain instructions and the /assistant command.
#
#   ./install.sh                 asks for a name, your Telegram id and the bot token
#   ASSISTANT_NO_LAUNCHD=1 ...   writes everything but does not load launchd (for testing)
#   --whatsapp                   also installs the WhatsApp listener (needs node + QR scan)
#
# The bot token is read with `read -s` in YOUR terminal. Never paste it in a chat.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WITH_WA=0
for a in "$@"; do [ "$a" = "--whatsapp" ] && WITH_WA=1; done

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required"
PY="$(command -v python3)"

say "== Personal assistant setup =="
DEFAULT_NAME="${ASSISTANT_NAME:-assistant}"
read -r -p "What should the assistant be called? [$DEFAULT_NAME] " NAME
NAME="${NAME:-$DEFAULT_NAME}"

OWNER_ID="${OWNER_TELEGRAM_ID:-}"
if [ -z "$OWNER_ID" ]; then
  say "Your numeric Telegram id: send any message to @userinfobot on Telegram."
  read -r -p "Your Telegram id: " OWNER_ID
fi
[[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "the Telegram id must be a number"

STATE="${ASSISTANT_STATE:-$HOME/.assistant}"
AGENT_DIR="${ASSISTANT_AGENT_DIR:-$HOME/assistant-agent}"
mkdir -p "$STATE"/{secrets,inbox/media,logs,state,memory} "$AGENT_DIR"
chmod 700 "$STATE/secrets"

TOKEN_FILE="$STATE/secrets/telegram_bot_token"
if [ ! -s "$TOKEN_FILE" ]; then
  say "Create a bot with @BotFather on Telegram, then paste its token below (it is not shown)."
  read -r -s -p "Bot token: " TOKEN; echo
  [[ "$TOKEN" == *:* ]] || die "that does not look like a bot token"
  umask 077
  printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
  unset TOKEN
fi
chmod 600 "$TOKEN_FILE"

# config.json (no secrets in it)
"$PY" - "$STATE/config.json" "$NAME" "$OWNER_ID" <<'PYEOF'
import json, sys
path, name, owner = sys.argv[1], sys.argv[2], int(sys.argv[3])
json.dump({"name": name, "owner": {"telegram_id": owner}, "channels": {}}, open(path, "w"), indent=2)
PYEOF

# code, brain instructions, command, memory skeleton
mkdir -p "$AGENT_DIR/src" "$AGENT_DIR/.claude/commands"
cp "$HERE"/src/*.py "$AGENT_DIR/src/"
[ "$WITH_WA" = 1 ] && cp "$HERE"/src/whatsapp_listener.js "$HERE"/src/package.json "$HERE"/src/restart_whatsapp.sh "$AGENT_DIR/src/"
cp "$HERE/templates/claude_commands/assistant.md" "$AGENT_DIR/.claude/commands/assistant.md"
sed -e "s|@NAME@|$NAME|g" -e "s|@STATE@|$STATE|g" -e "s|@AGENT_DIR@|$AGENT_DIR|g" \
    "$HERE/templates/CLAUDE.md.tmpl" > "$AGENT_DIR/CLAUDE.md"
[ -f "$STATE/memory/MEMORY.md" ] || cp "$HERE"/templates/memory/* "$STATE/memory/"
[ -f "$STATE/TASKS.md" ]   || cp "$HERE/templates/TASKS.md" "$STATE/TASKS.md"
[ -f "$STATE/HANDOFF.md" ] || cp "$HERE/templates/HANDOFF.md" "$STATE/HANDOFF.md"
# the commands and scripts say ~/.assistant; point them at a custom state dir if one was set
if [ "$STATE" != "$HOME/.assistant" ]; then
  grep -rl "\.assistant" "$AGENT_DIR" 2>/dev/null | xargs sed -i '' "s|~/.assistant|$STATE|g" || true
fi

if [ "$WITH_WA" = 1 ]; then
  command -v npm >/dev/null || die "npm is required for --whatsapp"
  (cd "$AGENT_DIR/src" && npm install --silent)
fi

# launchd agents
make_plist() { # label name keepalive(1/0) interval args...
  local label="$1" pname="$2" keep="$3" interval="$4"; shift 4
  local args="" a
  for a in "$@"; do args="$args    <string>$a</string>"$'\n'; done
  local ka
  if [ "$keep" = 1 ]; then ka="  <key>KeepAlive</key><true/>"; else ka="  <key>StartInterval</key><integer>$interval</integer>"; fi
  local out="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|@LABEL@|$label|g" -e "s|@NAME@|$pname|g" -e "s|@STATE@|$STATE|g" \
      -e "s|@AGENT_DIR@|$AGENT_DIR|g" "$HERE/templates/launchd.plist.tmpl" \
    | AWK_ARGS="$args" AWK_KA="$ka" "$PY" -c '
import os, sys
t = sys.stdin.read()
sys.stdout.write(t.replace("@ARGS@", os.environ["AWK_ARGS"].rstrip("\n")).replace("@KEEPALIVE@", os.environ["AWK_KA"]))
' > "$out"
  if [ "${ASSISTANT_NO_LAUNCHD:-0}" != 1 ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$out"
  fi
  say "  installed $label"
}

say "Installing launchd agents..."
make_plist "ai.assistant.listener.telegram" listener_telegram 1 0 "$PY" "$AGENT_DIR/src/telegram_listener.py"
make_plist "ai.assistant.healthcheck" healthcheck 0 60 "$PY" "$AGENT_DIR/src/healthcheck.py"
if [ "$WITH_WA" = 1 ]; then
  make_plist "ai.assistant.listener.whatsapp" listener_whatsapp 1 0 "$(command -v node)" "$AGENT_DIR/src/whatsapp_listener.js"
  say "WhatsApp: the first run prints a QR code in $STATE/logs/listener_whatsapp.log; scan it from your phone."
fi

# first message
if [ "${ASSISTANT_NO_LAUNCHD:-0}" != 1 ]; then
  sleep 3
  "$PY" "$AGENT_DIR/src/notify.py" "Hi, I am $NAME. Setup finished. Open a terminal in $AGENT_DIR, run claude, then type /assistant." \
    && say "Sent you a hello on Telegram." || say "Could not send the hello yet: check $STATE/logs."
fi

say ""
say "Done. Next:"
say "  cd $AGENT_DIR && claude"
say "  then type:  /assistant"

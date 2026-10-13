#!/usr/bin/env bash
# One-shot setup of a personal assistant on macOS: Telegram listener, notify,
# health check, memory skeleton, brain instructions and the /assistant command.
#
#   ./install.sh                 asks for a name, your Telegram id and the bot token
#   ASSISTANT_NO_LAUNCHD=1 ...   writes everything but does not load launchd (for testing)
#   --whatsapp                   also installs the WhatsApp listener (needs node + QR scan)
#   --no-graph                   skip the knowledge-graph memory (MCP server + indexing hooks)
#   --no-keep-awake              do not install the agent that stops the Mac sleeping while on power
#   --email                      connect a Gmail (or other IMAP) inbox: address + app password (hidden input)
#   --voice                      voice replies (edge-tts) and local transcription of voice notes (whisper.cpp)
#
# The bot token is read with `read -s` in YOUR terminal. Never paste it in a chat.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
WITH_WA=0
WITH_GRAPH=1
WITH_VOICE=0
WITH_EMAIL=0
KEEP_AWAKE=1
for a in "$@"; do
  [ "$a" = "--whatsapp" ] && WITH_WA=1
  [ "$a" = "--no-graph" ] && WITH_GRAPH=0
  [ "$a" = "--voice" ] && WITH_VOICE=1
  [ "$a" = "--email" ] && WITH_EMAIL=1
  [ "$a" = "--no-keep-awake" ] && KEEP_AWAKE=0
done

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
import json, os, sys
path, name, owner = sys.argv[1], sys.argv[2], int(sys.argv[3])
d = json.load(open(path)) if os.path.exists(path) else {}   # keep channels added by an earlier run
d["name"] = name
d.setdefault("owner", {})["telegram_id"] = owner
d.setdefault("channels", {})
json.dump(d, open(path, "w"), indent=2)
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


# email: an IMAP inbox read by the multi-account listener (owner-only sending comes later)
if [ "$WITH_EMAIL" = 1 ]; then
  say "Connecting email..."
  say "  Gmail: turn on 2-step verification, then create an App Password at"
  say "  https://myaccount.google.com/apppasswords (a 16-letter code). Use that, not your normal password."
  read -r -p "  Email address: " EMAIL_ADDR
  read -r -p "  Short name for this inbox (for example: gmail) [gmail]: " EMAIL_NAME
  EMAIL_NAME="${EMAIL_NAME:-gmail}"
  read -r -p "  IMAP server [imap.gmail.com]: " IMAP_HOST
  IMAP_HOST="${IMAP_HOST:-imap.gmail.com}"
  read -r -s -p "  App password (not shown): " EMAIL_PW; echo
  [ -n "$EMAIL_ADDR" ] && [ -n "$EMAIL_PW" ] || die "address and app password are required"
  umask 077
  printf '%s\n' "$EMAIL_PW" > "$STATE/secrets/email_${EMAIL_NAME}_app_password"
  unset EMAIL_PW
  "$PY" - "$STATE/config.json" "$EMAIL_NAME" "$EMAIL_ADDR" "$IMAP_HOST" "$STATE/secrets/email_${EMAIL_NAME}_app_password" <<'PYEOF'
import json, sys
path, name, addr, host, pw = sys.argv[1:6]
d = json.load(open(path))
d.setdefault("channels", {}).setdefault("email_accounts", {})[name] = {
    "enabled": True, "address": addr, "imap_host": host, "imap_port": 993, "password_path": pw}
json.dump(d, open(path, "w"), indent=2)
PYEOF
fi

# voice: replies as voice notes (edge-tts) and local transcription of incoming voice notes
if [ "$WITH_VOICE" = 1 ]; then
  say "Setting up voice..."
  if command -v brew >/dev/null; then
    brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
    brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
  else
    say "  Homebrew not found: install ffmpeg and whisper-cpp yourself"
  fi
  [ -d "$STATE/venv" ] || "$PY" -m venv "$STATE/venv"
  "$STATE/venv/bin/pip" install --quiet edge-tts
  mkdir -p "$AGENT_DIR/scripts"; cp "$HERE/scripts/fetch_whisper_model.sh" "$AGENT_DIR/scripts/"
  ASSISTANT_STATE="$STATE" bash "$AGENT_DIR/scripts/fetch_whisper_model.sh" || say "  model download failed: run scripts/fetch_whisper_model.sh later"
  say "  voice is set up (edit \"voice\" in $STATE/config.json to change the voice)"
fi

# knowledge-graph memory: an MCP memory server + hooks that index each session into it.
# The hooks use a headless Claude run (it consumes some of your Claude usage).
if [ "$WITH_GRAPH" = 1 ]; then
  say "Setting up the knowledge graph..."
  GRAPH="$STATE/graph"; mkdir -p "$GRAPH/logs"
  cat > "$GRAPH/mcp.json" <<JSON
{"mcpServers":{"memory":{"command":"npx","args":["-y","@modelcontextprotocol/server-memory"],"env":{"MEMORY_FILE_PATH":"$GRAPH/graph.jsonl"}}}}
JSON
  if command -v claude >/dev/null && command -v npx >/dev/null; then
    claude mcp remove memory --scope user >/dev/null 2>&1 || true
    claude mcp add --scope user memory -e "MEMORY_FILE_PATH=$GRAPH/graph.jsonl" -- npx -y @modelcontextprotocol/server-memory >/dev/null \
      && say "  registered the memory MCP server" || say "  could not register the MCP server: run it by hand (see README)"
  else
    say "  claude or npx not found: install them, then run the 'claude mcp add' line from the README"
  fi
  SETTINGS="$HOME/.claude/settings.json"; mkdir -p "$HOME/.claude"
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-assistant-$(date +%s)"
  "$PY" - "$SETTINGS" "$AGENT_DIR" <<'PYEOF'
import json, os, sys
path, agent = sys.argv[1], sys.argv[2]
d = json.load(open(path)) if os.path.exists(path) else {}
hooks = d.setdefault("hooks", {})
cmd = f"python3 {agent}/src/memory_index.py"
for event in ("PreCompact", "SessionEnd"):
    groups = hooks.setdefault(event, [])
    if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 30}]})
note = "echo 'Knowledge graph: long-term memory is in the memory MCP server. Before continuing any topic, call mcp__memory__search_nodes (project or person name) and open_nodes on the hits.'"
groups = hooks.setdefault("SessionStart", [])
if not any(h.get("command") == note for g in groups for h in g.get("hooks", [])):
    groups.append({"matcher": "compact", "hooks": [{"type": "command", "command": note}]})
json.dump(d, open(path, "w"), indent=2)
PYEOF
  say "  added indexing hooks to $SETTINGS (a backup was saved next to it)"
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
if [ "$KEEP_AWAKE" = 1 ]; then
  # caffeinate -i -s: no idle sleep while on power, even with the screen locked
  make_plist "ai.assistant.keepawake" keepawake 1 0 /usr/bin/caffeinate -i -s
  say "  the Mac will not sleep while plugged in (a closed lid still sleeps: see docs/KEEP-AWAKE.md)"
fi
if [ "$WITH_EMAIL" = 1 ]; then
  make_plist "ai.assistant.listener.email.$EMAIL_NAME" "listener_email_$EMAIL_NAME" 1 0 "$PY" "$AGENT_DIR/src/listener_email.py" --account "$EMAIL_NAME"
fi
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

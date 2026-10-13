# Architecture

## Pieces

**State root** `~/.assistant/` (never in git)

```
config.json        name, owner telegram id, non-secret settings
secrets/           bot token, app passwords (chmod 600)
inbox/stream.jsonl one JSON line per accepted incoming message
inbox/media/       downloaded photos, voice notes, files
logs/ state/       listener logs, offsets, heartbeats
memory/            curated facts, one per file, plus MEMORY.md
```

**Code repo** `~/assistant-agent/` (git): listeners, notify, skills.

## Message flow

1. A listener receives a message and checks it against the owner id (Telegram) or
   the allow-list (other channels). Anything else is dropped or logged as data.
2. It appends a line to `inbox/stream.jsonl` and writes media next to it.
3. The Monitor in the Claude Code session surfaces the line.
4. The brain decides: answer, act, remind, or stay quiet.
5. It replies through `notify.py` on the channel the owner used.
6. Anything durable (a decision, a status change, a preference) is written to
   memory the same minute, not at the end of the day.

## Always-on

- Listeners run under launchd (macOS) or systemd (Linux), restart on crash, start
  at boot.
- Each listener touches a heartbeat file from a background thread every few
  seconds. A health check reads heartbeat age and also **message recency**: a
  process that is running but stuck looks exactly like a quiet inbox.
- The Monitor is not a daemon. It lives inside the session and expires after at
  most 30 minutes, so it must be re-armed. Check that no other session is running
  the same monitor before arming one.

## What the brain may do without asking

Read, summarise, search, remind, prepare drafts for the owner's own use, and
update its own memory and task list. Everything outward-facing (messages to other
people, posts, purchases, deploys, deletions) needs a fresh yes for that action.

## Growing it

Add one channel at a time: Telegram, then email, then WhatsApp. Get each one
boring before the next. For each new idea, add a short note to `docs/` in the
same commit as the code.

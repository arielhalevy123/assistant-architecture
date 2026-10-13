# One brain: do not build a second agent that listens and forwards

The most common way to over-build this: starting a separate "listener agent" (or
one agent per channel) that reads Telegram, WhatsApp or email and *relays* what it
finds to the main session. Do not do that.

## The rule

- **Listeners are dumb daemons.** `telegram_listener.py`, `whatsapp_listener.js`,
  `listener_email.py` and `listener_gmail.py` do no thinking. Each accepted message
  becomes one JSON line in the same file, `inbox/stream.jsonl`.
- **There is exactly one brain**: the Claude Code session the owner talks to. It
  watches `inbox/stream.jsonl` with one Monitor and sees every channel in one
  place, in order.
- **No relay chain.** No agent whose job is to listen and pass things on, no
  per-channel agent, no "inbox agent" that summarises for the main one.

## Why

- A relay loses context: the main session no longer sees the original message,
  only someone's paraphrase of it.
- Two sessions answering the same message means double replies.
- Memory splits in two. What the listener agent learned is not in the session that
  has to act on it.
- It adds a second thing that can silently hang, and nobody notices until the
  owner does.

## What is allowed

- **Subagents for one-off jobs** (build a document, research a question, run a
  long check), started by the main session, that return a result and stop. They
  do not message the owner, do not arm Monitors, and do not run in a loop.
- **A headless Claude run by a hook** (the memory indexer) that only writes to the
  knowledge graph.
- **A health check** that is plain code (no model) and alerts the owner when a
  listener is stuck.

## How the pieces connect

```
Telegram / WhatsApp / email listeners ──► inbox/stream.jsonl ──► ONE Monitor ──► ONE Claude Code session
                                                                                      │
                          one notify.py, reply on the channel the owner used ◄────────┘
```

When the session is closed, the listeners keep collecting. A new session reads the
inbox first (anything that arrived while it was away), then arms the Monitor.

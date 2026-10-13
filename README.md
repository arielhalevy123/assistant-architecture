# Assistant architecture

A small, boring architecture for an always-on personal assistant built on
Claude Code. It talks to its owner on Telegram (and optionally WhatsApp and
email), remembers things between sessions, and keeps running by itself.

This repo is the **architecture and the reflexes**, not one person's data. It
contains no secrets, no contacts and no private state. Every account you create
for it must be new.

## The idea in one picture

```
 Telegram ──► listener ─┐
 WhatsApp ──► listener ─┼──► inbox/stream.jsonl ──► Monitor ──► Claude Code (the brain)
 Email    ──► listener ─┘                                           │
                                                                    ├──► notify.py ──► owner
 launchd/systemd keeps the listeners alive                          ├──► memory/*.md + MEMORY.md
                                                                    └──► TASKS.md, HANDOFF.md
```

- **Listeners** are tiny daemons: one per channel. Each appends one JSON line per
  accepted message to a single inbox stream. They do not think.
- **The Monitor** is Claude Code's live `tail -F` of that stream. It pushes each
  new line into the session. It expires after 30 minutes, so you re-arm it.
- **The brain** is the Claude Code session: it reads, decides, and acts.
- **Notify** is how it answers. Reply on the channel the owner wrote on.
- **Memory** is one fact per file plus an index, so a new session continues
  where the last one stopped.

## What is here

| path | what |
|---|---|
| `docs/SETUP-PROMPT.md` | the prompt you paste into Claude Code to build yourself (Hebrew how-to on top) |
| `docs/ARCHITECTURE.md` | the pieces and how they fit |
| `docs/MEMORY.md` | how to keep memory useful and not a junk drawer |
| `docs/RULES.md` | the safety rules and reflexes, learned the hard way |
| `src/telegram_listener.py` | Telegram long-poll listener, owner id only |
| `src/whatsapp_listener.js` | WhatsApp Web listener (QR login) |
| `src/monitor_filter.py` | optional filter in front of the Monitor |
| `src/notify.py` | send a Telegram message to the owner |
| `config.example.json` | the one config file; the token lives elsewhere |

## Start here

1. Read `docs/SETUP-PROMPT.md` and paste it into a fresh Claude Code session in
   an empty folder.
2. Create a Telegram bot with `@BotFather`, put the token in
   `~/.assistant/secrets/telegram_bot_token`.
3. Get your numeric Telegram id from `@userinfobot` and put it in
   `~/.assistant/config.json`.
4. Start with Telegram only. Add email, then WhatsApp, only when the last step
   is boring.

## License

Use it, change it, share it. No warranty: it can message people on your behalf,
so read `docs/RULES.md` first.

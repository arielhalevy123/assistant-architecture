# מדריך מהיר בעברית

עוזר אישי שרץ על Claude Code וידבר איתך בטלגרם. הקמה בפעם אחת, בלי לכתוב קוד.

**מה צריך:** מק עם Python 3 ו-Claude Code. לא חובה: Node (לוואטסאפ), Homebrew (לקול).

**שלבים:**
1. פותחים בטלגרם את `@BotFather`, יוצרים בוט, ושומרים את הטוקן (לא שולחים אותו לאף אחד).
2. שולחים הודעה ל-`@userinfobot` כדי לקבל את המזהה המספרי שלך.
3. בטרמינל:
   ```bash
   git clone https://github.com/arielhalevy123/assistant-architecture
   cd assistant-architecture
   ./install.sh            # שואל שם, מזהה טלגרם וטוקן (ההקלדה סמויה)
   ```
   אפשר להוסיף: `--whatsapp` (וואטסאפ, סריקת QR), `--voice` (תשובות קוליות ותמלול), `--no-graph` (בלי גרף הזיכרון).
4. מתחילים סשן: `cd ~/assistant-agent && claude` ואז כותבים `/assistant`.
5. הסוכן שולח הודעה בטלגרם. מכאן מדברים איתו שם.

**חשוב לדעת:**
- מוח אחד: כל המאזינים כותבים לקובץ אחד וסשן אחד קורא אותו. לא מרימים סוכן נפרד שמאזין ומעביר הודעות (`docs/ONE-BRAIN.md`).
- הטוקן והסיסמאות נשמרים רק בתיקייה `~/.assistant/secrets` ולא בגיט.
- הסוכן שואל לפני כל דבר שיוצא החוצה או שאי אפשר להחזיר (שליחה לאנשים, פרסום, קנייה, מחיקה).
- ההתקנה כוללת גרף זיכרון (שרת MCP) ומריצה Claude קצר ברקע לאינדוקס, אז תראה שימוש. אפשר לדלג עם `--no-graph`.
- בעיות? `docs/RULES.md` ו-`docs/SETUP-PROMPT.md` מסבירים את הכללים ואת הפרומפט להקמה.

---

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

**One brain.** Every listener writes to the same inbox and one session reads it.
Do not start a separate agent that listens and relays: see `docs/ONE-BRAIN.md`.

## What is here

| path | what |
|---|---|
| `docs/SETUP-PROMPT.md` | the prompt you paste into Claude Code to build yourself (Hebrew how-to on top) |
| `docs/ARCHITECTURE.md` | the pieces and how they fit |
| `docs/ONE-BRAIN.md` | why there is no second agent that listens and forwards |
| `docs/MEMORY.md` | how to keep memory useful and not a junk drawer |
| `docs/RULES.md` | the safety rules and reflexes, learned the hard way |
| `src/telegram_listener.py` | Telegram long-poll listener, owner id only |
| `src/whatsapp_listener.js` | WhatsApp Web listener (QR login) |
| `src/monitor_filter.py` | optional filter in front of the Monitor |
| `src/notify.py` | send a Telegram message to the owner |
| `config.example.json` | the one config file; the token lives elsewhere |
| `install.sh` | one-shot setup (macOS) |
| `templates/` | brain instructions, `/assistant` command, memory skeleton, launchd template |
| `src/healthcheck.py`, `attention.py` | liveness and "is it useful, not just alive" checks |
| `src/index_stream.py` | full-text index over the message stream |
| `src/listener_email.py`, `listener_gmail.py`, `sort_inbox.py`, `spam_scan.py`, `calendar_write.py` | optional email, inbox sorting and calendar helpers |

## One-shot install (macOS)

```bash
git clone <this repo> && cd assistant-architecture
./install.sh            # asks for a name, your Telegram id, and the bot token (hidden input)
cd ~/assistant-agent && claude
/assistant              # loads memory, arms the monitors, reports
```

`install.sh` creates `~/.assistant/` (config, secrets, inbox, logs, memory, tasks,
handoff), copies the code to `~/assistant-agent/`, writes the brain's
instructions (`CLAUDE.md`) and the `/assistant` command, installs launchd agents
for the Telegram listener and the health check, and sends you a hello on
Telegram. Add `--whatsapp` for the WhatsApp listener (needs node and a QR scan).
The assistant does not have to write any plumbing code; it only has to run
`/assistant` and follow the rules in `CLAUDE.md`.

The installer also sets up the **knowledge graph**: the official MCP memory server plus
`PreCompact`/`SessionEnd` hooks (`src/memory_index.py`) that index each session into it, so
a fresh session can `search_nodes` and continue. It edits `~/.claude/settings.json` (a backup is
saved next to it). The indexing runs a short headless Claude, which uses some of your usage.
Skip it with `--no-graph`. `--voice` adds voice-note replies (edge-tts) and local
transcription of voice notes you send (whisper.cpp + ffmpeg, a ~466 MB model). Manual registration:
`claude mcp add --scope user memory -e MEMORY_FILE_PATH=~/.assistant/graph/graph.jsonl -- npx -y @modelcontextprotocol/server-memory`.

Linux is not covered by the installer yet (the scripts are portable, the launchd
step is not).

## Start here (manual route)

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

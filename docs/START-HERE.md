# START HERE: send this file to your Claude Code agent and it builds everything

**בעברית, לך:** פותחים Claude Code בתיקייה ריקה, ושולחים לו את הקובץ הזה (הדבקה או "קרא את הקובץ ובנה לי הכל לפיו"). הוא ישכפל את הריפו, יסביר ויכין הכל. את הסיסמאות והטוקנים מקלידים **אתה**, בטרמינל, באמצעות הפקודה שהוא ייתן לך (מתחילה ב-`!`). לא שולחים אותם בצ'אט.

---

# Instructions for the agent (you are Claude Code)

Your owner wants a personal assistant that talks to them on Telegram (and,
optionally, email, WhatsApp and voice), remembers things between sessions, and
keeps working when the Mac is locked and plugged in. Everything is already written
in a repo. Your job is to **set it up with them, not to write it from scratch**.

Repo: https://github.com/arielhalevy123/assistant-architecture

## Rules that apply to every step

1. **The owner types every secret** (bot token, app password) in their own
   terminal. Never ask for one in the chat and never print one. You can run the
   installer *for* them only by telling them to run it with the `!` prefix inside
   this session or in their own terminal, because it asks for hidden input.
2. **Ask before anything outward-facing or irreversible**, once per action.
3. **One brain.** Do not start a separate agent that listens and relays. All
   listeners write to one inbox and **this** session reads it (`docs/ONE-BRAIN.md`).
4. **Verify the real effect**: after each step, have the owner send a real message
   and confirm it appears in `~/.assistant/inbox/stream.jsonl`. "Running" proves
   nothing.
5. Say what you did not do or could not verify.

## Steps

### 1. Get the repo
```bash
git clone https://github.com/arielhalevy123/assistant-architecture ~/assistant-architecture
```
If the clone fails because the repo is private, tell the owner to ask for access
(or to run `gh auth login`) and wait. Then read `~/assistant-architecture/README.md`
(Hebrew quick guide on top) and the files in `docs/`: `ARCHITECTURE.md`,
`ONE-BRAIN.md`, `RULES.md`, `MEMORY.md`, `CONNECT-CHANNELS.md`, `KEEP-AWAKE.md`.

### 2. Make sure the basics exist
Check `python3 --version`, `node --version` (only needed for WhatsApp), `claude --version`,
and Homebrew (only needed for voice). Tell the owner what is missing and how to
install it. Do not install system software without asking.

### 3. Telegram first
Walk the owner through:
1. In Telegram, open `@BotFather`, send `/newbot`, pick a name. Keep the token.
2. Send any message to `@userinfobot` to get the numeric id.

Then tell them to run, in their own terminal (or here with the `!` prefix):

```bash
cd ~/assistant-architecture && ./install.sh
```

It asks for a name, the Telegram id and the token (hidden), then creates
`~/.assistant/`, installs the listener and health check as launchd agents, installs
the knowledge graph (MCP memory server + indexing hooks), a keep-awake agent, and
sends a hello on Telegram. **Test:** the owner sends the bot a message and you see
it in the stream, and you reply with `python3 ~/assistant-agent/src/notify.py "..."`.

### 4. The other channels, one at a time (ask which they want)
Follow `docs/CONNECT-CHANNELS.md`. Each is the same installer with a flag, run by
the owner so the secrets stay with them:

- email: `./install.sh --email` (Gmail App Password; 2-step verification first)
- WhatsApp: `./install.sh --whatsapp` (needs Node; the first run shows a QR to scan from the phone)
- voice: `./install.sh --voice` (voice replies and local transcription; needs Homebrew; downloads a ~466 MB model)

Running the installer again is safe: it keeps existing state. Test each channel
with a real message before moving on.

### 5. Locked and plugged in
The installer already adds `caffeinate` (no sleep while on power, screen may be
locked). Explain `docs/KEEP-AWAKE.md`: a closed lid on a MacBook still sleeps, and
after a restart the agents start only after login. Have the owner lock the screen,
wait 15 minutes, and message the bot from the phone.

### 6. Start working
```bash
cd ~/assistant-agent && claude
```
then `/assistant`. It loads memory, checks listeners, arms the two Monitors
(re-arm them when they expire after 30 minutes; check no other session already runs
them) and reports on Telegram.

### 7. First conversation
Ask what to call the assistant (the installer asked once; confirm), how the owner
likes to be spoken to (language, length), and what deserves an alert. Save those as
the first memories.

## Done means
- The owner can message the bot at any hour and get a reply, with the screen locked.
- A restart plus login brings everything back.
- A new session runs `/assistant` and continues where the last one stopped.
- No secret is in git or in the chat.

Now start with step 1 and tell the owner what you are doing in short, plain sentences.

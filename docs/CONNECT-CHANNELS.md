# Connecting the channels

Telegram comes first and is installed by `install.sh`. Email and WhatsApp are
optional and each takes a few minutes. Do one at a time and test each before the
next. **The owner types every secret in their own terminal. The assistant never
asks for a secret in the chat.**

## Telegram (required)

1. In Telegram, open `@BotFather`, send `/newbot`, choose a name. Keep the token.
2. Send any message to `@userinfobot` to get your numeric id.
3. `./install.sh` asks for the name, the id and the token (hidden input).
4. Test: send the bot a message, the assistant answers. Add the bot to a group
   only if you want it there; in groups a bot sees only commands unless you turn
   off privacy mode with `@BotFather` (`/setprivacy`, Disable) and re-add it.

## Email (Gmail or any IMAP)

1. Google account, 2-step verification on.
2. Create an **App Password**: https://myaccount.google.com/apppasswords (16 letters).
3. `./install.sh --email` asks for the address, a short name and the app password
   (hidden). It writes the secret to `~/.assistant/secrets/` and the account to
   `config.json`, and starts the listener.
4. Test: send yourself an email; a new line appears in
   `~/.assistant/inbox/stream.jsonl` within a minute.
5. Sending email on the owner's behalf is a separate step and needs the owner's
   yes for each message.

Other providers: use their IMAP server (iCloud: `imap.mail.me.com`) and an
app-specific password.

## WhatsApp

1. `./install.sh --whatsapp` (needs Node).
2. The first run prints a QR code in `~/.assistant/logs/listener_whatsapp.log` and
   saves it as a PNG. On the phone: WhatsApp, Settings, Linked devices, Link a
   device, scan.
3. Test: send yourself a WhatsApp message; it appears in the inbox stream.
4. Notes: the session lasts weeks and then needs a new scan. Never open the
   saved browser profile in a normal browser: it can log you out. WhatsApp has no
   official API for this; use a number you can afford to lose and send at human
   pace.

## Order and checks

Telegram, then email, then WhatsApp, then voice (`--voice`). After each: send a
real message through that channel and see it arrive in the stream. A listener that
is "running" proves nothing.

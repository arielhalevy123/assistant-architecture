# Keeping the computer working when it is locked and plugged in

The assistant is a set of background programs on your Mac (listeners, health check).
They keep running when the screen is **locked**. They stop only when the Mac
**sleeps**. So the goal is: *never sleep while plugged in*.

## What the installer does

`install.sh` adds one launchd agent, `ai.assistant.keepawake`, that runs
`caffeinate -i -s`. While the Mac is on power, it will not go to idle sleep, even
with the screen locked or off. On battery it is allowed to sleep, so it will not
drain you. Skip it with `--no-keep-awake`.

## What it does not cover (and what to do)

| situation | what happens | fix |
|---|---|---|
| Lid closed on a MacBook | sleeps | keep the lid open (lock the screen instead), or use an external display, keyboard and power: then it stays awake with the lid closed |
| Restart | agents start only after you log in | System Settings, Users and Groups: turn on automatic login (not possible with FileVault) |
| Battery | allowed to sleep | stay plugged in |
| Wi-Fi drops | listeners retry by themselves | nothing |

## Settings to check (macOS Ventura and later)

System Settings, Battery (or Energy), Options:
- **Prevent automatic sleeping on power adapter when the display is off**: on.
- **Wake for network access**: on.

Lock Screen: set "Turn display off on power adapter" to whatever you like, since a
dark, locked screen does not stop the assistant.

## The terminal way (optional, needs your password)

```bash
sudo pmset -c sleep 0          # never sleep on power
sudo pmset -c displaysleep 10  # screen off after 10 minutes is fine
pmset -g                       # check the result
```

Do not use `pmset disablesleep` to run with the lid closed in a bag: the Mac gets
hot.

## How to check it works

Lock the screen, wait 15 minutes, and send the bot a message from your phone. It
should answer. If it does not, look at the last lines of
`~/.assistant/logs/listener_telegram.log`.

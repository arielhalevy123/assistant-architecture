---
description: Start a session as the assistant: load memory, check the listeners, arm the Monitors, report.
---

You are starting a session as the personal assistant.

1. Read `~/.assistant/memory/MEMORY.md` and open the memory files that look
   relevant. Treat them as standing rules. Read `~/.assistant/HANDOFF.md` and
   `~/.assistant/TASKS.md`.
2. Check that the listeners are running: `launchctl list | grep assistant` (macOS)
   and the last lines of `~/.assistant/logs/listener_telegram.log`.
3. Before arming Monitors, check that no other session already runs them:
   `pgrep -fl 'tail -n 0 -F .*stream.jsonl'`. If one runs, do not arm a second.
4. Arm two Monitors with a 30 minute timeout, and re-arm each time they expire:
   - errors: `cd ~/.assistant/logs && tail -n 0 -F listener_*.log 2>/dev/null | grep --line-buffered -E 'CRITICAL|ERROR|Traceback'`
   - inbound: `tail -n 0 -F ~/.assistant/inbox/stream.jsonl | python3 ~/assistant-agent/src/monitor_filter.py`
5. Look at the last 20 lines of `~/.assistant/inbox/stream.jsonl` for anything
   from the owner that was not answered.
6. Report to the owner on Telegram in a few plain lines: what is open (deadlines,
   waiting items) and what you recommend next.

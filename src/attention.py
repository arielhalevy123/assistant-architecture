#!/usr/bin/env python3
"""Two checks that ask whether the assistant is *useful*, not merely whether it is alive.

Everything that already exists watches liveness: a pid, a heartbeat, a launchd
label. Between 2026-08-26 and 2026-09-05 three separate outages slipped past all
of it, because in each case the process was running perfectly and simply was not
doing anything:

  - sort_inbox blocked on a dead socket for 9 days holding a PID lock
  - the WhatsApp listener stayed "connected" for 32 hours without one message
  - the Claude session (the assistant's brain) detached three times; the listeners kept
    filing mail into stream.jsonl and nobody answered any of it

The two checks here are deliberately narrow, because a noisy alarm is worse than
none — 15 straight days of a false xlsx warning already proved that here.

  unanswered  the owner sent something and nothing went back. This is the one
              that matters: it detects a detached brain without knowing anything
              about sessions, monitors or subscriptions. It only ever looks at
              messages the owner himself sent.

  silent      a listener has produced zero events for far longer than its own
              history says is plausible. Independent of pid and heartbeat.

Both are read-only and import cleanly, so healthcheck.py can call them without
taking on any new failure modes of its own.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

STATE_ROOT = Path.home() / ".assistant"
STREAM = STATE_ROOT / "inbox" / "stream.jsonl"
LEDGER = STATE_ROOT / "state" / "ledger.jsonl"
ACTIVITY = STATE_ROOT / "state" / "activity.jsonl"
CONFIG = STATE_ROOT / "config.json"

# How long the owner may wait before silence is a fault rather than the assistant thinking.
# 20 minutes: long enough to survive a slow reply or a long tool run, short enough
# that he is not left talking to a wall for an afternoon.
UNANSWERED_AFTER_S = 20 * 60

# Per-channel silence thresholds, in hours, derived from each channel's own
# measured history over 2026-08-09..09-06 rather than picked by feel:
#
#   channel    events/day   median gap   p95 gap   threshold
#   whatsapp        141.5       0.01h       0.28h        8h   (~28x p95)
#   email            14.3       0.48h       5.04h       24h   (~5x p95)
#   gmail             0.7       0.69h     272.24h     EXCLUDED
#   telegram         24.6       0.06h       4.28h     EXCLUDED
#
# gmail is excluded because at 0.7 events/day its p95 gap is eleven days — its
# silence carries no information at all, so any threshold short enough to be
# useful would fire constantly. telegram is excluded because it is owner-only and
# may legitimately be quiet for days; its silence is what `unanswered` covers.
#
# Both exclusions are deliberate. A channel we cannot judge is left unjudged
# rather than given a number that would generate noise — 15 days of a false xlsx
# alarm already showed what that costs.
SILENCE_HOURS = {
    "whatsapp": 8,
    "email": 24,
}

# Only complain about silence during hours the owner is plausibly awake; a quiet
# 04:00 is not evidence of anything.
ACTIVE_HOURS = range(9, 24)

# After a boot or a wake the listeners need time to reconnect and refill. Alarming
# during that window would have fired every time the laptop came out of sleep —
# which is exactly what happened when the machine travelled to Greece and slept.
GRACE_AFTER_BOOT_S = 20 * 60


def _tail_lines(path: Path, max_bytes: int = 512_000) -> list[str]:
    """Last chunk of a large append-only file, whole lines only.

    stream.jsonl is multi-megabyte and this runs every minute; reading it whole
    each time is pointless work. The first (possibly partial) line is dropped.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _iter_json(lines: list[str]):
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except Exception:
            continue


def _owner_id() -> int | None:
    try:
        return int(json.loads(CONFIG.read_text())["owner"]["telegram_id"])
    except Exception:
        return None


def seconds_since_boot() -> float:
    """Seconds since the kernel booted. Used only to suppress post-wake noise."""
    try:
        out = subprocess.check_output(["sysctl", "-n", "kern.boottime"],
                                      text=True, timeout=5)
        # kern.boottime: { sec = 1788500000, usec = 0 } ...
        sec = int(out.split("sec = ")[1].split(",")[0].strip())
        return time.time() - sec
    except Exception:
        return float("inf")      # unknown: assume long up, do not suppress


# --------------------------------------------------------------------- checks

def last_owner_message() -> dict | None:
    """Newest Telegram message the owner actually sent."""
    oid = _owner_id()
    if oid is None:
        return None
    newest = None
    for e in _iter_json(_tail_lines(STREAM)):
        if e.get("channel") != "telegram":
            continue
        if e.get("from_id") != oid:
            continue
        if not (e.get("ts") or 0):
            continue
        if newest is None or e["ts"] > newest["ts"]:
            newest = e
    return newest


def last_outbound_ts() -> float:
    """Newest moment the assistant said anything to the owner over Telegram.

    Reads both the ledger and the activity journal: notify.py records to one and
    guard-routed sends to the other, and a reply through either counts.
    """
    newest = 0.0
    for path, match in ((LEDGER, ("telegram_out", "telegram_file_out")),
                        (ACTIVITY, None)):
        for e in _iter_json(_tail_lines(path, 256_000)):
            ts = e.get("ts") or 0
            if not ts or ts <= newest:
                continue
            if match is None:
                if e.get("kind") == "notify" and e.get("ok"):
                    newest = ts
            elif e.get("action") in match and e.get("ok"):
                newest = ts
    return newest


def check_unanswered(now: float | None = None) -> list[str]:
    """Owner spoke, the assistant did not. The detached-brain detector."""
    now = now or time.time()
    msg = last_owner_message()
    if not msg:
        return []
    waited = now - msg["ts"]
    if waited <= UNANSWERED_AFTER_S:
        return []
    if last_outbound_ts() >= msg["ts"]:
        return []                      # answered after he wrote — fine
    preview = (msg.get("text") or msg.get("summary") or "")[:60].replace("\n", " ")
    return [f"unanswered name=owner waited_min={waited/60:.0f} "
            f"msg_id={msg.get('message_id')} preview={preview!r}"]


def active_seconds_between(start: float, end: float) -> float:
    """Seconds in [start, end) that fall inside ACTIVE_HOURS.

    Gating *when* the check runs is not enough. Elapsed wall-clock still counts
    the night, so at 09:00 an ordinary quiet night since 23:00 reads as ten
    hours and trips an eight-hour threshold. That exact false positive fired on
    2026-09-08 09:00, latched the healthcheck's `silence` state to ALARM, and —
    because the DM only fires on an OK->ALARM transition — silently disabled the
    alarm for everything that came after, including a real 28h WhatsApp wedge.
    Measure only the hours the feed is actually expected to carry traffic.
    """
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        lt = time.localtime(cur)
        # advance to the end of this local hour, or to `end`, whichever is first
        nxt = min(end, cur + (3600 - (lt.tm_min * 60 + lt.tm_sec)))
        if lt.tm_hour in ACTIVE_HOURS:
            total += nxt - cur
        cur = nxt
    return total


def check_silence(now: float | None = None) -> list[str]:
    """Listeners that are running but have produced nothing for far too long."""
    now = now or time.time()
    if time.localtime(now).tm_hour not in ACTIVE_HOURS:
        return []
    if seconds_since_boot() < GRACE_AFTER_BOOT_S:
        return []

    newest: dict[str, float] = {}
    for e in _iter_json(_tail_lines(STREAM)):
        ch = e.get("channel")
        ts = e.get("ts") or 0
        if not ch or not ts:
            continue
        # 'email' covers every IMAP account; 'gmail' and 'telegram' are excluded
        # above and must not be folded into it, or their quiet would mask a real
        # mail outage.
        if ch in ("telegram", "gmail"):
            continue
        key = ch if ch in SILENCE_HOURS else "email"
        if key in SILENCE_HOURS and ts > newest.get(key, 0):
            newest[key] = ts

    alarms = []
    for key, hours in SILENCE_HOURS.items():
        seen = newest.get(key)
        if not seen:
            continue           # nothing in the retained window; no basis to judge
        quiet_h = active_seconds_between(seen, now) / 3600
        if quiet_h > hours:
            wall_h = (now - seen) / 3600
            alarms.append(f"silent name={key} quiet_h={quiet_h:.1f} max_h={hours} "
                          f"wall_h={wall_h:.1f} "
                          f"(process may be up but is receiving nothing)")
    return alarms


def summary_line() -> str:
    """One compact field for the healthcheck log."""
    msg = last_owner_message()
    if not msg:
        return "attention(no_owner_msg)"
    gap = time.time() - msg["ts"]
    answered = last_outbound_ts() >= msg["ts"]
    return f"attention(last_owner_msg_min={gap/60:.0f} answered={answered})"


if __name__ == "__main__":
    print(summary_line())
    for a in check_unanswered() + check_silence():
        print("ALARM", a)

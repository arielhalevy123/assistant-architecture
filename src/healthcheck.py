#!/usr/bin/env python3
"""
Health check for the assistant's listeners.

Reads each listener's heartbeat file, checks launchctl status,
and appends a single line to ~/.assistant/logs/healthcheck.log per run.
Emits ALARM lines when something is off (stale heartbeat, missing
launchd job, unexpected exit) so a Monitor tail can catch them.

Exit code 0 always (so a launchd StartInterval keeps it running);
the alarm channel is the log, not the exit status.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# launchd gives almost no environment and the cwd is not guaranteed, so make the
# sibling modules importable explicitly rather than relying on WorkingDirectory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

STATE_ROOT = Path.home() / ".assistant"
LOG = STATE_ROOT / "logs" / "healthcheck.log"
LAST_STATE_PATH = STATE_ROOT / "state" / "healthcheck_last_state.json"
LAST_DM_PATH = STATE_ROOT / "state" / "healthcheck_last_dm.json"
# How long an alarm may stay unreported before it is re-sent. Six hours is short
# enough that a real outage cannot hide behind a latched state for a whole day,
# and long enough that a persistent alarm does not become the noise it is meant
# to cut through.
RENAG_AFTER_S = 6 * 3600
CONFIG_PATH = STATE_ROOT / "config.json"
TG_TOKEN_PATH = STATE_ROOT / "secrets" / "telegram_bot_token"

# name -> (label, heartbeat, max_age_seconds)
LISTENERS: dict[str, tuple[str, Path, int]] = {
    "telegram": ("ai.assistant.listener.telegram", STATE_ROOT / "state" / "listener_telegram.heartbeat", 60),
    "gmail": ("ai.assistant.listener.gmail", STATE_ROOT / "state" / "listener_gmail.heartbeat", 120),
    "whatsapp": ("ai.assistant.listener.whatsapp", STATE_ROOT / "state" / "listener_whatsapp.heartbeat", 60),
}

# Dynamically add multi-account email listeners from config.
try:
    _cfg = json.loads(CONFIG_PATH.read_text())
    for _name, _a in _cfg.get("channels", {}).get("email_accounts", {}).items():
        if _a.get("enabled"):
            LISTENERS[f"email_{_name}"] = (
                f"ai.assistant.listener.email.{_name}",
                STATE_ROOT / "state" / f"listener_email_{_name}.heartbeat",
                120,
            )
except Exception:
    pass

# Only watch the listeners that are actually installed (a plist exists).
_AGENTS = Path.home() / "Library" / "LaunchAgents"
if _AGENTS.exists():
    LISTENERS = {k: v for k, v in LISTENERS.items() if (_AGENTS / f"{v[0]}.plist").exists()}


def launchctl_status(label: str) -> tuple[str, str]:
    """Return (pid_str, last_exit_str). pid_str is '-' when not running."""
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True, timeout=10)
    except Exception as e:
        return ("?", f"launchctl_err:{e}")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            return (parts[0], parts[1])
    return ("-", "not_loaded")


def check(name: str, label: str, hb: Path, max_age: int) -> list[str]:
    alarms: list[str] = []
    pid, exit_s = launchctl_status(label)
    if pid == "-" or pid == "?":
        alarms.append(f"launchd_missing name={name} label={label} status={exit_s}")

    if not hb.exists():
        alarms.append(f"heartbeat_missing name={name} path={hb}")
    else:
        age = time.time() - hb.stat().st_mtime
        if age > max_age:
            alarms.append(f"heartbeat_stale name={name} age_s={age:.1f} max_s={max_age}")

    return alarms


def load_last_dm() -> dict[str, float]:
    if LAST_DM_PATH.exists():
        try:
            return json.loads(LAST_DM_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_last_dm(state: dict[str, float]) -> None:
    LAST_DM_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_DM_PATH.write_text(json.dumps(state))


def load_last_state() -> dict[str, str]:
    if LAST_STATE_PATH.exists():
        try:
            return json.loads(LAST_STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_last_state(state: dict[str, str]) -> None:
    LAST_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_STATE_PATH.write_text(json.dumps(state))


def send_telegram(text: str) -> bool:
    """Fire-and-forget text DM to the owner. Returns True on ok."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        owner_id = int(cfg["owner"]["telegram_id"])
        token = TG_TOKEN_PATH.read_text().strip()
        data = urllib.parse.urlencode({"chat_id": owner_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
            return bool(r.get("ok"))
    except Exception:
        return False


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    per_listener_alarms: dict[str, list[str]] = {}
    for name, (label, hb, max_age) in LISTENERS.items():
        pid, exit_s = launchctl_status(label)
        age = None
        if hb.exists():
            age = time.time() - hb.stat().st_mtime
        parts.append(f"{name}(pid={pid} exit={exit_s} hb_age_s={age:.1f})" if age is not None else f"{name}(pid={pid} exit={exit_s} hb=missing)")
        per_listener_alarms[name] = check(name, label, hb, max_age)

    # Liveness above answers "is the process running". These two answer "is it
    # doing anything" — the question three separate multi-hour outages slipped
    # through because nothing was asking it. Imported defensively: a bug in the
    # newer check must never take down the liveness check that already works.
    try:
        import attention
        per_listener_alarms["attention"] = attention.check_unanswered()
        per_listener_alarms["silence"] = attention.check_silence()
        parts.append(attention.summary_line())
    except Exception as e:
        per_listener_alarms["attention"] = []
        per_listener_alarms["silence"] = []
        parts.append(f"attention(error:{type(e).__name__})")

    iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with LOG.open("a") as f:
        for name, alarms in per_listener_alarms.items():
            for a in alarms:
                f.write(f"{iso} ALARM {a}\n")
        f.write(f"{iso} OK {' '.join(parts)}\n")

    # Transition-based DM, plus a re-nag while an alarm persists.
    #
    # Transition-only was not enough. On 2026-09-08 09:00 a false `silence`
    # alarm (ordinary overnight quiet counted against an 8h threshold) latched
    # this state to ALARM. It never returned to OK, so no OK->ALARM edge ever
    # occurred again, and a genuine 28h WhatsApp wedge on 09-09/09-10 produced
    # 730 alarm lines in the log and not one DM. A single false positive
    # permanently disabled the alert. Re-notify while still bad so a stuck
    # alarm can never mean permanent silence.
    last = load_last_state()
    last_dm = load_last_dm()
    new: dict[str, str] = {}
    new_dm: dict[str, float] = {}
    cold_start = not last
    now_ts = time.time()
    for name, alarms in per_listener_alarms.items():
        cur = "ALARM" if alarms else "OK"
        new[name] = cur
        if cold_start:
            continue
        prev = last.get(name, "OK")
        sent_at = last_dm.get(name, 0.0)
        # sent_at == 0 means we have no DM history for this name yet (first run
        # after this file was introduced, or an alarm that latched before it
        # existed). Treat that as due, but without the "N hours since" prefix,
        # which would otherwise be measured from the epoch.
        renag = (cur == "ALARM" and prev == "ALARM"
                 and now_ts - sent_at >= RENAG_AFTER_S)
        first_ever = renag and sent_at == 0
        if prev != cur or renag:
            if cur == "ALARM":
                if name == "attention":
                    # Worth its own wording: this one is telling the owner that the assistant
                    # is not answering him, which is not a generic health alarm
                    # and needs to say what to do about it.
                    msg = ("👋 שלחת לי הודעה ואני לא עניתי.\n"
                           "כנראה הסשן שלי נפל או מנותק. המאזינים עצמם חיים "
                           "וההודעה שלך נשמרה — היא לא אבדה.\n"
                           "כדי להחזיר אותי: תפתח את Claude Code בתיקיית "
                           "assistant-agent ותכתוב 'תמשיך'.\n\n" + "\n".join(alarms[:2]))
                elif name == "silence":
                    msg = ("🔇 מאזין רץ אבל לא קלט שום הודעה הרבה זמן.\n"
                           "זה בדיוק מה שקרה לוואטסאפ ב-4.9 — התהליך היה חי "
                           "והחיבור מת.\n\n" + "\n".join(alarms[:3]))
                else:
                    msg = f"🚨 the assistant health ALARM [{name}]\n" + "\n".join(alarms[:5])
            else:
                if name == "attention":
                    msg = "✅ חזרתי — עניתי על ההודעה שלך"
                elif name == "silence":
                    msg = "✅ המאזין קולט הודעות שוב"
                else:
                    msg = f"✅ the assistant recovered [{name}] — back to OK"
            if renag and not first_ever:
                hours = (now_ts - sent_at) / 3600
                msg = f"(עדיין פתוח, {hours:.0f} שעות מאז ההתראה הקודמת)\n" + msg
            if send_telegram(msg):
                new_dm[name] = now_ts
            else:
                # keep the old stamp so a failed send retries on the next tick
                # instead of resetting the re-nag clock
                new_dm[name] = sent_at
        else:
            new_dm[name] = last_dm.get(name, 0.0)
    save_last_state(new)
    save_last_dm(new_dm)
    return 0


if __name__ == "__main__":
    main()

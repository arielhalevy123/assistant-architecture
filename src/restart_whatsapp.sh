#!/bin/bash
# Scheduled restart of the WhatsApp listener.
#
# Why this exists: the listener wedges silently. It stays alive, keeps its heartbeat
# fresh, and receives nothing — 18.6h on 2026-09-16 and 20.3h on 2026-09-17. The
# library never notices, so nothing self-recovers. the owner chose a scheduled restart
# (option 2 of 3) over root-cause work for now: ten seconds of planned downtime
# instead of another day blind.
#
# This restart is UNCONDITIONAL by design. The conditional version — restart only
# when silence is detected — was deliberately rejected: a checker that can restart
# services can also loop restarting them, and that failure mode is worse than the
# one it fixes.
#
# KNOWN COST: any WhatsApp message arriving during the few seconds of restart may be
# missed, because WhatsApp does not backfill on reconnect. Scheduled times therefore
# favour quiet hours. This has not been measured — stated as a risk, not a fact.

set -uo pipefail

LABEL="ai.assistant.listener.whatsapp"
LOG="$HOME/.assistant/logs/restart_whatsapp.log"
AGENT_DIR="$HOME/assistant-agent"
PY="$HOME/.assistant/venv/bin/python3"

mkdir -p "$(dirname "$LOG")"
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

before_pid="$(launchctl list | awk -v l="$LABEL" '$3==l {print $1}')"
echo "$(ts) scheduled restart starting (pid_before=${before_pid:-none})" >> "$LOG"

if launchctl kickstart -k "gui/$(id -u)/$LABEL" >>"$LOG" 2>&1; then
    sleep 12
    after_pid="$(launchctl list | awk -v l="$LABEL" '$3==l {print $1}')"
    if [ -n "$after_pid" ] && [ "$after_pid" != "$before_pid" ]; then
        echo "$(ts) OK restarted (pid_after=$after_pid)" >> "$LOG"
        ok=true
    else
        echo "$(ts) WARN pid did not change (before=${before_pid:-none} after=${after_pid:-none})" >> "$LOG"
        ok=false
    fi
else
    echo "$(ts) FAIL kickstart returned nonzero" >> "$LOG"
    ok=false
    after_pid=""
fi

# Record it. A daemon restarting itself on a timer should still be auditable — if the
# listener is later found wedged, the ledger shows whether a scheduled restart ran.
cd "$AGENT_DIR" 2>/dev/null && "$PY" - <<PYEOF >> "$LOG" 2>&1
import sys
sys.path.insert(0, "$AGENT_DIR")
import guard
guard.record(
    "listener_scheduled_restart",
    target="$LABEL",
    detail={
        "before_pid": "${before_pid:-none}",
        "after_pid": "${after_pid:-none}",
        "succeeded": $( [ "$ok" = true ] && echo True || echo False ),
        "trigger": "launchd timer, not a human instruction",
        "verified_receiving": False,
        "note": "restart only — receipt is not verified here. A wedge can only be "
                "confirmed resolved by a real inbound message reaching stream.jsonl.",
    },
    authority={"path": "scheduled", "chosen_by": "owner 2026-09-18, option 2 of 3"},
)
PYEOF

echo "$(ts) done" >> "$LOG"

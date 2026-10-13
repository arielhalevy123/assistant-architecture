#!/usr/bin/env python3
"""Create and update events in the local macOS Calendar. Never delete.

Talks to Calendar.app via AppleScript, so it works with whatever accounts the Mac
already has configured (iCloud, Google/CalDAV, local) with no OAuth and no cloud
project. macOS asks for permission once, per the usual TCC prompt.

Two deliberate constraints:

  * WRITES ARE OPT-IN. Without --apply this prints exactly what it would create
    and touches nothing. A calendar is not a scratch pad.
  * IT NEVER DELETES. Create and field-level update only. An assistant that can
    quietly remove a meeting is a worse trade than one that occasionally leaves a
    duplicate behind, so removal stays a human action.
  * UPDATES ARE ADDRESSED BY UID, one event at a time, and only the fields you
    actually pass are touched. Unset arguments are None rather than "", because an
    update that blanks the notes just because they weren't supplied is the obvious
    way to lose data here. Use --find to get the uid first.

Dates are built field by field rather than parsed from a string. AppleScript's
`date "..."` coercion follows the Mac's locale, so a string that works on an
en_US machine silently means something else on he_IL — the kind of bug that puts
an interview on the wrong day.

Usage:
    calendar_write.py --list
    calendar_write.py --add --calendar Work --title "X" \
        --start 2026-08-24T18:30 --end 2026-08-24T19:15 \
        [--notes "..."] [--location "..."] [--alarm 30] [--apply]
    calendar_write.py --add-json events.json [--apply]
    calendar_write.py --find --calendar X [--on 2026-08-24] [--match-title "Google"]
    calendar_write.py --update --calendar X --uid UID [--location "..."] \
        [--start ...] [--end ...] [--title ...] [--append-notes "..."] [--apply]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def osa(script: str) -> str:
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"AppleScript failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def list_calendars() -> list[tuple[str, bool]]:
    out = osa('''
tell application "Calendar"
    set acc to ""
    repeat with c in calendars
        set acc to acc & (name of c) & "\\t" & (writable of c) & linefeed
    end repeat
    return acc
end tell''')
    rows = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, w = line.rsplit("\t", 1)
        rows.append((name.strip(), w.strip().lower() == "true"))
    return rows


def _as_date(var: str, dt: datetime) -> str:
    """AppleScript that builds a date field by field — locale independent."""
    return (f'set {var} to current date\n'
            f'set year of {var} to {dt.year}\n'
            f'set month of {var} to {MONTHS[dt.month - 1]}\n'
            f'set day of {var} to {dt.day}\n'
            f'set time of {var} to {dt.hour * 3600 + dt.minute * 60}\n')


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def add_event(calendar: str, title: str, start: datetime, end: datetime,
              notes: str = "", location: str = "", alarm_min: int | None = None,
              apply: bool = False) -> str:
    if end <= start:
        raise ValueError(f"end {end} is not after start {start}")
    names = {n for n, _ in list_calendars()}
    if calendar not in names:
        raise ValueError(f"no calendar named {calendar!r}. Available: {sorted(names)}")
    writable = dict(list_calendars()).get(calendar)
    if not writable:
        raise ValueError(f"calendar {calendar!r} is not writable")

    props = [f'summary:"{_esc(title)}"', "start date:sd", "end date:ed"]
    if notes:
        props.append(f'description:"{_esc(notes)}"')
    if location:
        props.append(f'location:"{_esc(location)}"')

    alarm = ""
    if alarm_min:
        alarm = (f'\n        make new display alarm at end of display alarms of newEv '
                 f'with properties {{trigger interval:-{int(alarm_min)}}}')

    script = ("tell application \"Calendar\"\n"
              + _as_date("sd", start) + _as_date("ed", end)
              + f'    tell calendar "{_esc(calendar)}"\n'
              f'        set newEv to make new event with properties {{{", ".join(props)}}}{alarm}\n'
              '        return uid of newEv\n'
              '    end tell\n'
              'end tell')

    if not apply:
        return (f"DRY RUN — would create in {calendar!r}: {title} "
                f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}"
                + (f" @ {location}" if location else "")
                + (f" (alarm {alarm_min}m before)" if alarm_min else ""))
    uid = osa(script)
    return f"created uid={uid} in {calendar!r}: {title} {start:%Y-%m-%d %H:%M}"


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def find_events(calendar: str, on: datetime | None = None,
                title_contains: str = "", uid: str = "") -> list[dict]:
    """Locate events so an update can be aimed precisely. Read-only."""
    if uid:
        where = f'uid = "{_esc(uid)}"'
        pre = ""
    elif on:
        pre = _as_date("d0", on.replace(hour=0, minute=0)) + _as_date(
            "d1", on.replace(hour=23, minute=59))
        where = "start date >= d0 and start date <= d1"
        if title_contains:
            where += f' and summary contains "{_esc(title_contains)}"'
    elif title_contains:
        pre = ""
        where = f'summary contains "{_esc(title_contains)}"'
    else:
        raise ValueError("need uid, or a date, or a title fragment")

    out = osa("tell application \"Calendar\"\n" + pre
              + f'    tell calendar "{_esc(calendar)}"\n'
              f'        set evs to (every event whose {where})\n'
              '        set acc to ""\n'
              '        repeat with e in evs\n'
              '            set acc to acc & (uid of e) & "\\t" & (summary of e) & "\\t" '
              '& ((start date of e) as string) & "\\t" & ((end date of e) as string) '
              '& "\\t" & (location of e) & "\\n"\n'
              '        end repeat\n'
              '        return acc\n'
              '    end tell\n'
              'end tell')
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 4:
            loc = p[4] if len(p) > 4 else ""
            if loc.strip() == "missing value":
                loc = ""
            rows.append({"uid": p[0], "title": p[1], "start": p[2],
                         "end": p[3], "location": loc})
    return rows


def update_event(calendar: str, uid: str, title: str | None = None,
                 start: datetime | None = None, end: datetime | None = None,
                 notes: str | None = None, append_notes: str | None = None,
                 location: str | None = None, apply: bool = False) -> str:
    """Change fields on ONE existing event, addressed by uid.

    Only fields explicitly passed are touched. Everything else is left exactly as
    it was — an update that blanks the notes because they weren't supplied is the
    obvious way to lose data here, so unset arguments are `None`, never "".

    There is deliberately no delete. Update reaches one uid at a time, so a
    mistyped title can't rewrite the week.
    """
    matches = find_events(calendar, uid=uid)
    if not matches:
        raise ValueError(f"no event with uid {uid!r} in {calendar!r}")
    before = matches[0]

    sets = []
    pre = ""
    if title is not None:
        sets.append(f'set summary of e to "{_esc(title)}"')
    if start is not None:
        pre += _as_date("sd", start)
        sets.append("set start date of e to sd")
    if end is not None:
        pre += _as_date("ed", end)
        sets.append("set end date of e to ed")
    if location is not None:
        sets.append(f'set location of e to "{_esc(location)}"')
    if notes is not None:
        sets.append(f'set description of e to "{_esc(notes)}"')
    elif append_notes:
        cur = osa("tell application \"Calendar\"\n"
                  f'    tell calendar "{_esc(calendar)}"\n'
                  f'        return description of (first event whose uid = "{_esc(uid)}")\n'
                  '    end tell\nend tell')
        joined = (cur + "\n\n" + append_notes) if cur.strip() else append_notes
        sets.append(f'set description of e to "{_esc(joined)}"')

    if not sets:
        return f"nothing to change on {before['title']!r} — no fields given"

    changes = []
    if title is not None:
        changes.append(f"title: {before['title']!r} -> {title!r}")
    if start is not None:
        changes.append(f"start: {before['start']} -> {start:%Y-%m-%d %H:%M}")
    if end is not None:
        changes.append(f"end: {before['end']} -> {end:%Y-%m-%d %H:%M}")
    if location is not None:
        changes.append(f"location: {before['location']!r} -> {location!r}")
    if notes is not None:
        changes.append("notes: replaced")
    elif append_notes:
        changes.append("notes: appended")

    if not apply:
        return (f"DRY RUN — would update {before['title']!r} ({uid[:8]}…)\n    "
                + "\n    ".join(changes))

    osa("tell application \"Calendar\"\n" + pre
        + f'    tell calendar "{_esc(calendar)}"\n'
        f'        set e to first event whose uid = "{_esc(uid)}"\n'
        + "".join(f"        {s}\n" for s in sets)
        + '    end tell\nend tell')
    return f"updated {before['title']!r}: " + "; ".join(changes)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--add-json")
    ap.add_argument("--find", action="store_true",
                    help="list matching events with their uids (read-only)")
    ap.add_argument("--update", action="store_true",
                    help="change fields on ONE event, addressed by --uid")
    ap.add_argument("--uid", help="which event to update")
    ap.add_argument("--on", help="YYYY-MM-DD, for --find")
    ap.add_argument("--match-title", default="", help="title fragment, for --find")
    ap.add_argument("--append-notes", help="add to the existing notes instead of replacing")
    ap.add_argument("--calendar")
    ap.add_argument("--title")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--notes", default=None)
    ap.add_argument("--location", default=None)
    ap.add_argument("--alarm", type=int, default=None)
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, nothing changes.")
    a = ap.parse_args()

    if a.list:
        for name, w in list_calendars():
            print(f"{'rw' if w else 'ro'}  {name}")
        return 0

    if a.find:
        if not a.calendar:
            print("--find needs --calendar", file=sys.stderr)
            return 2
        on = datetime.strptime(a.on, "%Y-%m-%d") if a.on else None
        rows = find_events(a.calendar, on=on, title_contains=a.match_title, uid=a.uid or "")
        if not rows:
            print("no matching events")
        for r in rows:
            print(f"{r['uid']}\n    {r['title']}\n    {r['start']} -> {r['end']}"
                  + (f"\n    @ {r['location']}" if r['location'] else ""))
        return 0

    if a.update:
        if not (a.calendar and a.uid):
            print("--update needs --calendar and --uid (get it from --find)", file=sys.stderr)
            return 2
        try:
            msg = update_event(
                a.calendar, a.uid, title=a.title,
                start=_parse(a.start) if a.start else None,
                end=_parse(a.end) if a.end else None,
                notes=a.notes, append_notes=a.append_notes,
                location=a.location, apply=a.apply)
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print(msg)
        if a.apply:
            try:
                sys.path.insert(0, str(Path.home() / "assistant-agent"))
                import guard
                guard.record("calendar_update", target=f"{a.calendar}:{a.uid}",
                             detail={"change": msg,
                                     "note": "field-level update; this tool cannot delete events"},
                             authority=None, ok=True)
            except Exception as exc:
                print(f"(ledger note failed: {exc})", file=sys.stderr)
        return 0

    events = []
    if a.add_json:
        events = json.loads(Path(a.add_json).read_text())
    elif a.add:
        if not (a.calendar and a.title and a.start and a.end):
            print("--add needs --calendar --title --start --end", file=sys.stderr)
            return 2
        events = [{"calendar": a.calendar, "title": a.title, "start": a.start,
                   "end": a.end, "notes": a.notes, "location": a.location,
                   "alarm": a.alarm}]
    else:
        print("nothing to do: pass --list, --add or --add-json", file=sys.stderr)
        return 2

    failed = 0
    for e in events:
        try:
            print(add_event(e["calendar"], e["title"], _parse(e["start"]),
                            _parse(e["end"]), e.get("notes", ""),
                            e.get("location", ""), e.get("alarm"), a.apply))
        except Exception as exc:
            failed += 1
            print(f"FAILED {e.get('title')!r}: {exc}", file=sys.stderr)

    if a.apply and not failed:
        try:
            sys.path.insert(0, str(Path.home() / "assistant-agent"))
            import guard
            guard.record("calendar_write", target="macOS Calendar",
                         detail={"events": [e.get("title") for e in events],
                                 "calendars": sorted({e.get("calendar") for e in events}),
                                 "note": "create-only; this tool cannot edit or delete"},
                         authority=None, ok=True)
        except Exception as exc:
            print(f"(ledger note failed: {exc})", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

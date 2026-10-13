#!/usr/bin/env python3
"""Classify and file inbox mail into the existing the assistant/* labels.

DRY RUN BY DEFAULT. Nothing moves unless --apply is passed.

Rules first, model second, and deliberately so. 2,746 messages is more than anyone will
re-check by hand, so the classifier has to be auditable: every decision here names the
signal that produced it, which means a wrong rule can be found and corrected once instead
of being rediscovered per-message. A model asked to sort thousands of emails produces
decisions nobody can review, and the failure mode is mail that is not lost but is no longer
findable — which is worse than an untidy inbox.

Anything the rules cannot place stays in the INBOX rather than being guessed into a folder.
An unsorted message is a small annoyance; a misfiled one is invisible.

    sort_inbox.py                 # report only
    sort_inbox.py --apply         # actually move
    sort_inbox.py --limit 50 --apply    # pilot
"""
from __future__ import annotations

import argparse
import email
import email.header
import imaplib
import re
import ssl
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ACCOUNT = "owner@example.com"
HOST = "imap.gmail.com"
SECRET = Path.home() / ".assistant" / "secrets" / "owner_gmail_gmail_app_password"

# Existing labels — extend, never reinvent. A second competing taxonomy is worse than none.
L_JOBS = "assistant/Jobs"
L_NEWS = "assistant/Newsletters"
L_NOTIF = "assistant/Notifications"
L_PROMO = "assistant/Promotions"
L_RECEIPTS = "assistant/Receipts"
L_PERSONAL = "assistant/Personal"

# Deliberately excludes glassdoor: 73 of its 99 matches in the dry run were community
# digests and review round-ups, not listings. Putting them here would make the assistant/Jobs a
# folder the owner stops trusting, which is worse than them sitting in Newsletters.
JOB_SENDERS = ("hiremetech", "referally", "secrethunter", "greenhouse", "comeet",
               "lever.co", "smartrecruiters", "workday", "jobs-noreply@linkedin",
               "jobalerts", "drushim", "alljobs")
RECEIPT_HINTS = ("receipt", "invoice", "חשבונית", "קבלה", "order confirmation",
                 "payment", "תשלום", "הזמנה מספר")
PROMO_HINTS = ("פרסומת", "unsubscribe to stop", "sale", "% off", "מבצע", "הנחה",
               "black friday", "discount")
NOTIF_SENDERS = ("no-reply", "noreply", "no_reply", "donotreply", "notification",
                 "alerts@", "security@", "support@", "automated")


def _hdr(msg, name: str) -> str:
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return str(raw)


def classify(msg) -> tuple[str | None, str]:
    """Return (label, the signal that decided it). None means leave it in the inbox."""
    frm = (_hdr(msg, "From") or "").lower()
    subj = _hdr(msg, "Subject") or ""
    subj_l = subj.lower()
    unsub = bool(msg.get("List-Unsubscribe"))

    for s in JOB_SENDERS:
        if s in frm:
            return L_JOBS, f"sender contains {s!r}"

    for h in RECEIPT_HINTS:
        if h in subj_l:
            return L_RECEIPTS, f"subject contains {h!r}"

    for h in PROMO_HINTS:
        if h in subj_l:
            return L_PROMO, f"subject contains {h!r}"

    # List-Unsubscribe means bulk mail the sender expects people to leave. It separates
    # newsletters from genuine one-to-one mail far more reliably than any keyword.
    if unsub:
        return L_NEWS, "has List-Unsubscribe header"

    for s in NOTIF_SENDERS:
        if s in frm:
            return L_NOTIF, f"sender contains {s!r}"

    return None, "no rule matched — left in INBOX"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually move messages")
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--limit", type=int, default=0, help="cap messages processed")
    args = ap.parse_args()

    since = (date.today() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    counts: Counter = Counter()
    reasons: defaultdict = defaultdict(Counter)
    samples: defaultdict = defaultdict(list)

    # timeout: imaplib otherwise blocks forever on a silently-dropped Gmail socket.
    with imaplib.IMAP4_SSL(HOST, 993, ssl_context=ssl.create_default_context(),
                           timeout=120) as M:
        M.login(ACCOUNT, SECRET.read_text().strip())
        M.select("INBOX", readonly=not args.apply)
        typ, data = M.search(None, "SINCE", since)
        ids = (data[0] or b"").split()
        if args.limit:
            ids = ids[-args.limit:]
        print(f"{ACCOUNT}: {len(ids)} INBOX messages since {since}"
              f"{'  [APPLY]' if args.apply else '  [DRY RUN]'}\n")

        by_label: defaultdict = defaultdict(list)
        for n, i in enumerate(ids, 1):
            typ, d = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE DATE)])")
            raw = b"".join(x[1] for x in d if isinstance(x, tuple) and x[1])
            if not raw:
                counts["UNREADABLE"] += 1
                continue
            msg = email.message_from_bytes(raw)
            label, why = classify(msg)
            key = label or "(stays in INBOX)"
            counts[key] += 1
            reasons[key][why] += 1
            if len(samples[key]) < 3:
                samples[key].append(f"{_hdr(msg,'From')[:44]} | {_hdr(msg,'Subject')[:56]}")
            if label:
                by_label[label].append(i)
            if n % 400 == 0:
                print(f"  ... {n}/{len(ids)}", file=sys.stderr)

        print("PLAN")
        for k, v in counts.most_common():
            print(f"  {v:5}  {k}")
            for why, c in reasons[k].most_common(3):
                print(f"           {c:5} via {why}")
            for s in samples[k]:
                print(f"           e.g. {s}")
            print()

        if not args.apply:
            print("DRY RUN — nothing moved. Re-run with --apply.")
            return 0

        typ, boxes = M.list()
        existing = {b.decode(errors="replace").split(' "/" ')[-1].strip('"') for b in boxes}
        for label in by_label:
            if label not in existing:
                typ, _ = M.create(label)
                print(f"  created label {label} ({typ})")

        failures = []
        for label, msg_ids in by_label.items():
            for chunk_start in range(0, len(msg_ids), 200):
                chunk = msg_ids[chunk_start:chunk_start + 200]
                seq = b",".join(chunk).decode()
                typ, _ = M.copy(seq, f'"{label}"')
                if typ != "OK":
                    # Do NOT flag for removal if the copy did not land. Losing the copy and
                    # removing the original is the one outcome that is not recoverable.
                    failures.append(label)
                    print(f"  COPY to {label} FAILED — left in INBOX, nothing removed",
                          file=sys.stderr)
                    break
                M.store(seq, "+FLAGS", "\\Deleted")
            else:
                print(f"  filed {len(msg_ids):5} -> {label}")
        M.expunge()
        if failures:
            print(f"\n  labels that failed and were skipped: {sorted(set(failures))}",
                  file=sys.stderr)
        print("\ndone. Messages remain in [Gmail]/All Mail — nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

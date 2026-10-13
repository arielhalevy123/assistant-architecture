#!/usr/bin/env python3
"""Targeted read-only scan of the folders the listeners do NOT ingest.

The listeners follow INBOX only. Anything a provider filters into Junk/Spam —
or that the user files away by hand — is invisible to the assistant, and on 2026-09-14 a
an application process went dark for exactly that reason: the confirmation landed,
the follow-up did not, and the assistant reported "no email found" as though that were
evidence of nothing having been sent.

So this is deliberately narrow. It takes a term you already have a reason to
look for (an employer in an active process), searches every folder EXCEPT the
ones already ingested, and prints what it finds. It does not write to
stream.jsonl, does not mark anything read, and opens every mailbox readonly.
A broad "show me my spam" sweep is not what this is for.

Usage:
    spam_scan.py --account owner_mail --term acme [--days 45]
    spam_scan.py --account owner_mail --term acme --all-folders
"""
import argparse
import email
import email.message
import imaplib
import json
import re
import ssl
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path

CONFIG_PATH = Path.home() / ".assistant" / "config.json"
IMAP_TIMEOUT = 120

# INBOX is normally already on the stream, so re-reporting it buries the thing we came
# for. Normally. The listeners halt when the laptop sleeps and nothing backfills the
# gap, so an INBOX message can be absent from the stream and present in the mailbox —
# which is why --include-inbox exists and why "not on the stream" is never a finding.
SKIP_FOLDERS = {"inbox"}


def decode(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def body_text(msg: email.message.Message, limit: int = 4000) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_type() == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    out = re.sub(r"[ \t]+", " ", "\n".join(parts))
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:limit]


def list_folders(imap: imaplib.IMAP4_SSL, all_folders: bool) -> list[str]:
    typ, data = imap.list()
    if typ != "OK":
        raise RuntimeError(f"LIST failed: {data}")
    names = []
    for raw in data:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        # ... (flags) "delim" "Name"  — the name is the final quoted field
        m = re.match(r'\((?P<flags>[^)]*)\)\s+"[^"]*"\s+"?(?P<name>[^"]+)"?\s*$', line)
        if not m:
            continue
        name, flags = m.group("name"), m.group("flags").lower()
        if "\\noselect" in flags:
            continue
        if name.lower() in SKIP_FOLDERS:
            continue
        if all_folders:
            names.append(name)
        elif "\\junk" in flags or re.search(r"junk|spam|דואר זבח|bulk", name, re.I):
            names.append(name)
    return names


def search(imap: imaplib.IMAP4_SSL, folder: str, term: str, since: str) -> list[bytes]:
    typ, _ = imap.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        return []
    # OR is prefix-form and binary in IMAP, so three fields need it nested twice.
    query = f'(SINCE {since}) (OR (OR FROM "{term}" SUBJECT "{term}") BODY "{term}")'
    typ, data = imap.uid("SEARCH", None, query)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--term", required=True, help="employer name or domain fragment")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--all-folders", action="store_true",
                    help="every selectable folder, not just Junk/Spam")
    ap.add_argument("--include-inbox", action="store_true",
                    help="also search INBOX (it can hold mail the listeners slept through)")
    ap.add_argument("--full", action="store_true", help="print the whole body, not an excerpt")
    args = ap.parse_args()

    if args.include_inbox:
        SKIP_FOLDERS.discard("inbox")

    cfg = json.loads(CONFIG_PATH.read_text())
    acct = cfg["channels"]["email_accounts"].get(args.account)
    if not acct:
        print(f"unknown account {args.account!r}", file=sys.stderr)
        return 2
    pw = Path(acct["password_path"].replace("~", str(Path.home()))).read_text().strip()

    since = (datetime.now() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(acct["imap_host"], acct.get("imap_port", 993),
                             ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT)
    imap.login(acct["address"], pw)
    try:
        folders = list_folders(imap, args.all_folders)
        print(f"[SCAN] {acct['address']} term={args.term!r} since={since} "
              f"folders={len(folders)}: {', '.join(folders)}\n")
        total = 0
        for folder in folders:
            try:
                uids = search(imap, folder, args.term, since)
            except Exception as exc:
                print(f"  [{folder}] search failed: {exc}")
                continue
            if not uids:
                continue
            print(f"=== {folder}: {len(uids)} match(es) ===")
            for uid in uids:
                typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                msg = email.message_from_bytes(data[0][1])
                total += 1
                print(f"\n--- {decode(msg.get('Date',''))}")
                print(f"From:    {decode(msg.get('From',''))}")
                print(f"Subject: {decode(msg.get('Subject',''))}")
                text = body_text(msg, 100000 if args.full else 4000)
                print(f"\n{text}\n")
        print(f"[SCAN] {total} message(s) found outside INBOX")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

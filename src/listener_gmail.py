#!/usr/bin/env python3
"""
Gmail IMAP listener for the the assistant assistant.

Polls INBOX every ~25s, tracks last-seen UID + UIDVALIDITY so we never
re-emit a message, appends each new message to ~/.assistant/inbox/stream.jsonl
with channel="gmail", saves attachments to ~/.assistant/inbox/media/gmail_<uid>/,
touches a heartbeat every cycle.

Zero third-party deps: stdlib imaplib + email + ssl only.
"""

import email
import imaplib
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any

# Shared with the multi-account IMAP listener so the HTML renderer exists in one
# place only. Same directory, so sys.path[0] covers this import.
from listener_email import html_to_text

STATE_ROOT = Path.home() / ".assistant"
CONFIG_PATH = STATE_ROOT / "config.json"
PASSWORD_PATH = STATE_ROOT / "secrets" / "gmail_app_password"
INBOX = STATE_ROOT / "inbox"
MEDIA = INBOX / "media"
STREAM = INBOX / "stream.jsonl"
OFFSET_PATH = STATE_ROOT / "state" / "gmail_offset.json"
HEARTBEAT = STATE_ROOT / "state" / "listener_gmail.heartbeat"
LOG_PATH = STATE_ROOT / "logs" / "listener_gmail.log"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL = 25
BACKOFF_MIN = 5
BACKOFF_MAX = 300
MAX_BODY_CHARS = 20000
HEARTBEAT_INTERVAL = 15  # background thread touches heartbeat every 15s so a
#                          hung IMAP fetch or long reconnect backoff can't age
#                          past the healthcheck 120s email threshold


def _heartbeat_loop(stop: threading.Event) -> None:
    """See identical pattern in listener_telegram.py / listener_email.py."""
    while not stop.is_set():
        try:
            HEARTBEAT.touch()
        except Exception:
            pass
        stop.wait(HEARTBEAT_INTERVAL)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def load_offset() -> dict[str, Any]:
    if OFFSET_PATH.exists():
        try:
            return json.loads(OFFSET_PATH.read_text())
        except Exception:
            pass
    return {"uidvalidity": None, "next_uid": None}


def save_offset(state: dict[str, Any]) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps(state))


def hdr(msg: Message, name: str) -> str:
    raw = msg.get(name, "")
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def extract_text(msg: Message) -> tuple[str, str]:
    """Return (text_body, html_body). Prefer text/plain; fall back to text/html."""
    text, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain" and not text:
                try:
                    text = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and not html:
                try:
                    html = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        try:
            body = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            html = body
        else:
            text = body
    return text[:MAX_BODY_CHARS], html[:MAX_BODY_CHARS]


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def save_attachments(msg: Message, uid: int) -> list[str]:
    saved: list[str] = []
    if not msg.is_multipart():
        return saved
    dest = MEDIA / f"gmail_{uid}"
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if not filename or "attachment" not in disp.lower():
            continue
        try:
            filename = str(make_header(decode_header(filename)))
        except Exception:
            pass
        safe = SAFE_NAME.sub("_", filename)[:120] or "attachment"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / safe
        with out.open("wb") as f:
            f.write(payload)
        saved.append(str(out))
    return saved


def emit(record: dict[str, Any]) -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    with STREAM.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_uid(imap: imaplib.IMAP4_SSL, uid: int) -> None:
    # Same defensive fallback as listener_email.py: iCloud silently strips
    # the RFC822 body from a combined FETCH. Gmail currently doesn't
    # exhibit this, but the BODY.PEEK[] retry costs nothing when the
    # first fetch succeeds and rescues the message otherwise.
    def _extract_raw(fetched: list) -> bytes | None:
        for item in fetched or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    typ, data = imap.uid("FETCH", str(uid), "(RFC822 INTERNALDATE FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        logging.warning("fetch uid=%s failed: typ=%s", uid, typ)
        return
    raw = _extract_raw(data)
    if raw is None:
        typ, data = imap.uid("FETCH", str(uid), "BODY.PEEK[]")
        if typ != "OK":
            logging.warning("fetch uid=%s BODY.PEEK[] fallback failed: typ=%s", uid, typ)
            return
        raw = _extract_raw(data)
    if raw is None:
        logging.warning("fetch uid=%s: no body payload (both RFC822 and BODY.PEEK[] came back empty)", uid)
        return
    msg = email.message_from_bytes(raw)
    text, html = extract_text(msg)
    # html-only mail was previously stored as text="" with html_present=True,
    # leaving nothing recoverable in the stream. Render it instead.
    text_source = "plain" if text.strip() else ""
    if not text.strip() and html:
        rendered = html_to_text(html)
        if rendered:
            text = rendered[:MAX_BODY_CHARS]
            text_source = "html"
    media = save_attachments(msg, uid)
    subject = hdr(msg, "Subject")
    from_ = hdr(msg, "From")
    to_ = hdr(msg, "To")
    date_ = hdr(msg, "Date")
    summary = subject or (text[:200].strip() if text else "[no body]")

    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channel": "gmail",
        "uid": uid,
        "from": from_,
        "to": to_,
        "subject": subject,
        "date": date_,
        "text": text,
        "html_present": bool(html),
        "text_source": text_source,
        "summary": summary,
        "media_paths": media,
        "message_id": msg.get("Message-ID", ""),
    }
    emit(record)
    logging.info("inbox uid=%s from=%r subject=%r", uid, from_[:80], subject[:80])


# See listener_email.py for the full reasoning: an untimed imaplib socket blocks forever
# on a dropped connection, logging nothing while the daemon-thread heartbeat keeps the
# health check green. Same value, same reason.
IMAP_TIMEOUT = 120


def connect(address: str, password: str) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx, timeout=IMAP_TIMEOUT)
    imap.login(address, password)
    return imap


def select_and_sync(imap: imaplib.IMAP4_SSL, state: dict[str, Any]) -> dict[str, Any]:
    typ, data = imap.select("INBOX", readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT INBOX failed: {data}")
    typ, uv_data = imap.response("UIDVALIDITY")
    typ2, un_data = imap.response("UIDNEXT")
    # imaplib returns these differently on some servers; also stash from status.
    typ3, status = imap.status("INBOX", "(UIDVALIDITY UIDNEXT)")
    uv = None
    un = None
    if typ3 == "OK" and status and status[0]:
        s = status[0].decode() if isinstance(status[0], (bytes, bytearray)) else status[0]
        m1 = re.search(r"UIDVALIDITY (\d+)", s)
        m2 = re.search(r"UIDNEXT (\d+)", s)
        if m1:
            uv = int(m1.group(1))
        if m2:
            un = int(m2.group(1))
    if uv is None:
        raise RuntimeError("could not read UIDVALIDITY")

    prev_uv = state.get("uidvalidity")
    prev_next = state.get("next_uid")

    if prev_uv != uv:
        # First run or mailbox reset: start from *now*, don't dump history.
        logging.info("UIDVALIDITY change or first run: uv=%s next_uid=%s (starting fresh)", uv, un)
        return {"uidvalidity": uv, "next_uid": un}

    if un is None or prev_next is None or un <= prev_next:
        return state  # no new mail

    # Fetch UIDs [prev_next .. un-1].
    typ, data = imap.uid("SEARCH", None, f"UID {prev_next}:{un - 1}")
    if typ != "OK":
        logging.warning("UID SEARCH failed: %s", data)
        return state
    uids = [int(x) for x in (data[0] or b"").split()]
    for uid in uids:
        try:
            process_uid(imap, uid)
        except Exception as e:
            logging.exception("process_uid uid=%s failed: %s", uid, e)
    return {"uidvalidity": uv, "next_uid": un}


def main() -> int:
    for p in (INBOX, MEDIA, LOG_PATH.parent, HEARTBEAT.parent, OFFSET_PATH.parent):
        p.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("gmail listener starting pid=%s", os.getpid())

    cfg = load_config()
    email_cfg = cfg.get("channels", {}).get("email", {})
    if not email_cfg.get("enabled"):
        logging.error("channels.email.enabled is false; refusing to start")
        return 3
    address = email_cfg.get("address")
    if not address:
        logging.error("channels.email.address missing")
        return 4
    password = PASSWORD_PATH.read_text().strip()
    if not password:
        logging.error("no password at %s", PASSWORD_PATH)
        return 5

    state = load_offset()
    backoff = BACKOFF_MIN
    imap: imaplib.IMAP4_SSL | None = None

    hb_stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(hb_stop,), daemon=True)
    hb_thread.start()

    while True:
        try:
            if imap is None:
                imap = connect(address, password)
                logging.info("connected as %s", address)
                backoff = BACKOFF_MIN
            new_state = select_and_sync(imap, state)
            if new_state != state:
                state = new_state
                save_offset(state)
            time.sleep(POLL_INTERVAL)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            logging.warning("imap error, reconnecting: %s", e)
            try:
                if imap is not None:
                    imap.logout()
            except Exception:
                pass
            imap = None
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        except KeyboardInterrupt:
            logging.info("gmail listener stopping (SIGINT)")
            try:
                if imap is not None:
                    imap.logout()
            except Exception:
                pass
            return 0
        except Exception as e:
            logging.exception("poll cycle failed: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


if __name__ == "__main__":
    sys.exit(main())

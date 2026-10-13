#!/usr/bin/env python3
"""
Multi-account IMAP listener for the assistant.

Parametrized by --account NAME. Reads config.channels.email_accounts[NAME]
for host/port/address/password. Polls INBOX every ~25s, tracks last-seen
UID + UIDVALIDITY per account so we never re-emit a message nor dump history.
Emits new messages to ~/.assistant/inbox/stream.jsonl with channel="email"
and account=NAME. Saves attachments to ~/.assistant/inbox/media/email_{account}_{uid}/.

Zero third-party deps: stdlib imaplib + email + ssl only.
"""

import argparse
import email
import imaplib
import json
import logging
import os
import codecs
import re
import ssl
import sys
import threading
import time
from email.header import decode_header, make_header
from email.message import Message
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

# Older Hebrew mail systems (Exchange at afeka.ac.il among them) label parts and
# headers "iso-8859-8-i". The -i and -e suffixes are bidirectional *ordering*
# hints, not distinct byte encodings, and Python's codec registry does not know
# them — so decode() raised LookupError and the whole message was dropped
# instead of ingested. That silently swallowed a final-project exam invitation
# on 2026-08-13 (uid 30654). Alias the variants onto plain iso-8859-8.
_BIDI_ALIASES = {
    "iso-8859-8-i", "iso-8859-8-e", "iso8859-8-i", "iso8859-8-e",
    "iso-8859-6-i", "iso-8859-6-e", "iso8859-6-i", "iso8859-6-e",  # same story for Arabic
}


def _bidi_codec_search(name: str):
    if name.replace("_", "-").lower() in _BIDI_ALIASES:
        base = "iso-8859-6" if "8859-6" in name.replace("_", "-") else "iso-8859-8"
        return codecs.lookup(base)
    return None


codecs.register(_bidi_codec_search)

STATE_ROOT = Path.home() / ".assistant"
CONFIG_PATH = STATE_ROOT / "config.json"
INBOX = STATE_ROOT / "inbox"
MEDIA = INBOX / "media"
STREAM = INBOX / "stream.jsonl"

POLL_INTERVAL = 25
BACKOFF_MIN = 5
BACKOFF_MAX = 300
MAX_BODY_CHARS = 20000
MAX_UID_ATTEMPTS = 4  # a uid that won't fetch is retried this many cycles before
#                       we give up on it — and giving up is recorded in the stream,
#                       never silent, because a skipped uid is a lost message
HEARTBEAT_INTERVAL = 15  # background thread touches heartbeat every 15s so a
#                          hung IMAP fetch or long reconnect backoff can't age
#                          past the healthcheck 120s email threshold


def _heartbeat_loop(hb: Path, stop: threading.Event) -> None:
    """Background touch loop.

    An IMAP FETCH can park the main thread for tens of seconds, and the
    reconnect backoff climbs to 300s. Both would age the heartbeat past
    the 120s alarm threshold and trigger a spurious ALARM DM. Touching
    from a daemon thread keeps liveness tied to the process itself.
    """
    while not stop.is_set():
        try:
            hb.touch()
        except Exception:
            pass
        stop.wait(HEARTBEAT_INTERVAL)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def load_offset(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"uidvalidity": None, "next_uid": None}


def save_offset(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def hdr(msg: Message, name: str) -> str:
    raw = msg.get(name, "")
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def decode_part(part: Message) -> str:
    """Bytes -> str for one part, never raising on a charset we do not know.

    A bogus or unregistered charset makes decode() raise LookupError, which is
    not covered by errors="replace" — that is how a message gets dropped whole
    rather than arriving slightly mangled. Prefer the declared charset, then
    utf-8, then latin-1 (which cannot fail). Garbled text beats silent loss.
    """
    payload = part.get_payload(decode=True) or b""
    if not payload:
        return ""
    declared = (part.get_content_charset() or "").strip()
    for cs in (declared, "utf-8", "iso-8859-8", "latin-1"):
        if not cs:
            continue
        try:
            return payload.decode(cs, errors="replace")
        except LookupError:
            continue
    return payload.decode("latin-1", errors="replace")


def extract_text(msg: Message) -> tuple[str, str]:
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
                    text = decode_part(part)
            elif ctype == "text/html" and not html:
                try:
                    html = part.get_content()
                except Exception:
                    html = decode_part(part)
            elif ctype == "text/calendar" and not text:
                # Meeting invitations carry the substance in the .ics part, and
                # Exchange often sends no useful text/plain alongside it.
                try:
                    text = part.get_content()
                except Exception:
                    text = decode_part(part)
    else:
        try:
            body = msg.get_content()
        except Exception:
            body = decode_part(msg)
        if msg.get_content_type() == "text/html":
            html = body
        else:
            text = body
    return text[:MAX_BODY_CHARS], html[:MAX_BODY_CHARS]


# Invisible padding mail senders use to stretch the preview line: soft hyphens,
# zero-width spaces/joiners, BOM, and the combining grapheme joiner that shows up
# as the "͏ ͏ ͏" run in marketing preheaders.
_INVISIBLE = re.compile(r"[­͏​-‏  ﻿]")


class _HTMLToText(HTMLParser):
    """Minimal HTML -> text, stdlib only, tolerant of malformed email markup.

    Link targets are kept as `text <url>`: for a job digest or a booking
    confirmation the URLs *are* the content, so dropping them would leave a
    record that reads fine and is useless.
    """

    # Tags whose *contents* must not appear in the text at all.
    SKIP = {"script", "style", "head", "title"}
    # Tags that imply a line break.
    BLOCK = {
        "p", "div", "br", "tr", "li", "table", "thead", "tbody", "blockquote",
        "section", "article", "header", "footer", "hr", "ul", "ol", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A missing </head> would otherwise leave _skip stuck above zero and
        # swallow the entire body.
        if tag == "body":
            self._skip = 0
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "a":
            self._href = dict(attrs).get("href")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a":
            href, self._href = self._href, None
            tail = "".join(self.parts[-4:])
            if (href and href not in tail
                    and not href.startswith(("mailto:", "#", "javascript:", "data:"))):
                self.parts.append(f" <{href}>")
            # Anchors are inline, so two adjacent links would otherwise fuse
            # into one word ("mail" + "top" -> "mailtop").
            self.parts.append(" ")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = _INVISIBLE.sub("", "".join(self.parts))
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.split("\n")]
        out: list[str] = []
        for ln in lines:
            if ln:
                out.append(ln)
            elif out and out[-1]:
                out.append("")
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """Render HTML to text, never raising — a bad parse must not lose a message."""
    if not html:
        return ""
    try:
        p = _HTMLToText()
        p.feed(html)
        p.close()
        return p.text()
    except Exception:
        logging.warning("html_to_text failed; falling back to tag strip", exc_info=True)
        try:
            stripped = re.sub(r"<[^>]+>", " ", html)
            return re.sub(r"\s+", " ", unescape(stripped)).strip()
        except Exception:
            return ""


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def save_attachments(account: str, msg: Message, uid: int) -> list[str]:
    saved: list[str] = []
    if not msg.is_multipart():
        return saved
    dest = MEDIA / f"email_{account}_{uid}"
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


def alert(account: str, kind: str, detail: str) -> None:
    """Write a listener-level problem into the stream itself.

    The stream is the only thing a session reads, so a failure that never
    reaches it is invisible no matter how loud the log is.
    """
    logging.error("ALERT %s: %s", kind, detail)
    emit({
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channel": "listener_alert",
        "account": account,
        "listener": "email",
        "kind": kind,
        "summary": f"[listener_alert] {kind}: {detail}",
        "detail": detail,
    })


def process_uid(account: str, imap: imaplib.IMAP4_SSL, uid: int) -> bool:
    """Fetch, parse and emit one uid. Returns True only if it reached the stream."""
    # iCloud's IMAP silently drops the RFC822 body when it's combined with
    # INTERNALDATE/FLAGS in a single FETCH — the response comes back with
    # only metadata. BODY.PEEK[] is the modern RFC 3501 form that both
    # Gmail and iCloud honour. We try the legacy form first (Gmail has
    # been working with it) and fall back to BODY.PEEK[] when the payload
    # is missing.
    def _extract_raw(fetched: list) -> bytes | None:
        for item in fetched or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    typ, data = imap.uid("FETCH", str(uid), "(RFC822 INTERNALDATE FLAGS)")
    if typ != "OK" or not data or data[0] is None:
        logging.warning("fetch uid=%s failed: typ=%s", uid, typ)
        return False
    raw = _extract_raw(data)
    if raw is None:
        typ, data = imap.uid("FETCH", str(uid), "BODY.PEEK[]")
        if typ != "OK":
            logging.warning("fetch uid=%s BODY.PEEK[] fallback failed: typ=%s", uid, typ)
            return False
        raw = _extract_raw(data)
    if raw is None:
        logging.warning("fetch uid=%s: no body payload (both RFC822 and BODY.PEEK[] came back empty)", uid)
        return False
    msg = email.message_from_bytes(raw)
    text, html = extract_text(msg)
    # An html-only email used to be recorded as text="" with html_present=True,
    # which left nothing recoverable in the stream — the body was extracted and
    # then thrown away. Job digests and booking confirmations arrive exactly this
    # way. Render the HTML instead, and record where the text came from.
    text_source = "plain" if text.strip() else ""
    if not text.strip() and html:
        rendered = html_to_text(html)
        if rendered:
            text = rendered[:MAX_BODY_CHARS]
            text_source = "html"
    media = save_attachments(account, msg, uid)
    subject = hdr(msg, "Subject")
    from_ = hdr(msg, "From")
    to_ = hdr(msg, "To")
    date_ = hdr(msg, "Date")
    summary = subject or (text[:200].strip() if text else "[no body]")

    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channel": "email",
        "account": account,
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
    return True


# Without this, imaplib builds a blocking socket with NO deadline, so a connection the
# server drops without a FIN — or a DNS/route failure mid-handshake — parks the listener
# inside connect() or a fetch forever. It logs nothing (it never returns to log either
# outcome), holds no visible socket, and the heartbeat keeps ticking from its own daemon
# thread, so the health check stays green over a listener that is receiving nothing.
#
# This is the third occurrence of the same root cause: repair_sort.py wedged 9 days on it
# (2026-08-26..09-04) and owner_gmail wedged on it during the DNS blip of 2026-09-06 09:24.
# 120s is far above a normal IMAP round trip, so a timeout here always means a real fault
# — and a raised timeout is recoverable, because the loop already reconnects with backoff.
IMAP_TIMEOUT = 120


def connect(host: str, port: int, address: str, password: str) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=IMAP_TIMEOUT)
    imap.login(address, password)
    return imap


def select_and_sync(account: str, imap: imaplib.IMAP4_SSL, state: dict[str, Any]) -> dict[str, Any]:
    typ, data = imap.select("INBOX", readonly=True)
    if typ != "OK":
        raise RuntimeError(f"SELECT INBOX failed: {data}")
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
    failed = dict(state.get("failed_uids") or {})

    if prev_uv is None:
        # Genuine first run for this account: adopt the current position and
        # deliberately skip history.
        logging.info("first run: uv=%s next_uid=%s (starting fresh - no history)", uv, un)
        return {"uidvalidity": uv, "next_uid": un, "failed_uids": {}}

    if prev_uv != uv:
        # The server says every UID we have stored is now meaningless, so we do
        # have to reset — but this is also the one code path that can silently
        # skip real mail, so it never happens quietly again. Anything that
        # arrived between prev_next and un is unrecoverable by UID after this.
        alert(
            account, "uidvalidity_change",
            f"UIDVALIDITY moved {prev_uv} -> {uv}; stored next_uid={prev_next} is void. "
            f"Resetting to {un}. Mail that arrived before this reset may never have been "
            f"captured — check the mailbox directly for that window.",
        )
        return {"uidvalidity": uv, "next_uid": un, "failed_uids": {}}

    # Retry anything that previously failed to fetch, independently of the range
    # scan below. Retrying by uid rather than rewinding next_uid means a bad
    # message cannot block the ones behind it, and good ones are never re-emitted.
    for uid_s in sorted(failed, key=int):
        uid = int(uid_s)
        ok = False
        try:
            ok = process_uid(account, imap, uid)
        except Exception as e:
            logging.exception("retry process_uid uid=%s failed: %s", uid, e)
        if ok:
            logging.info("uid=%s captured on retry %d", uid, failed[uid_s])
            failed.pop(uid_s, None)
            continue
        failed[uid_s] = failed[uid_s] + 1
        if failed[uid_s] >= MAX_UID_ATTEMPTS:
            alert(
                account, "uid_dropped",
                f"uid={uid} could not be fetched after {failed[uid_s]} attempts and is "
                f"being abandoned. That message exists in the mailbox but is NOT in this "
                f"stream — read it directly.",
            )
            failed.pop(uid_s, None)

    if un is None or prev_next is None or un <= prev_next:
        if failed != (state.get("failed_uids") or {}):
            return {"uidvalidity": uv, "next_uid": prev_next, "failed_uids": failed}
        return state

    typ, data = imap.uid("SEARCH", None, f"UID {prev_next}:{un - 1}")
    if typ != "OK":
        logging.warning("UID SEARCH failed: %s", data)
        return state
    uids = [int(x) for x in (data[0] or b"").split()]
    for uid in uids:
        ok = False
        try:
            ok = process_uid(account, imap, uid)
        except Exception as e:
            logging.exception("process_uid uid=%s failed: %s", uid, e)
        if not ok:
            # Queue it instead of letting next_uid step over it. A uid that is
            # skipped and forgotten is a message nobody will ever know about.
            failed[str(uid)] = failed.get(str(uid), 0) + 1
            logging.warning(
                "uid=%s not captured (attempt %d/%d) - queued for retry",
                uid, failed[str(uid)], MAX_UID_ATTEMPTS,
            )
    return {"uidvalidity": uv, "next_uid": un, "failed_uids": failed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="account name matching config.channels.email_accounts")
    args = ap.parse_args()
    account = args.account

    log_path = STATE_ROOT / "logs" / f"listener_email_{account}.log"
    heartbeat = STATE_ROOT / "state" / f"listener_email_{account}.heartbeat"
    offset_path = STATE_ROOT / "state" / f"email_offset_{account}.json"

    for p in (INBOX, MEDIA, log_path.parent, heartbeat.parent, offset_path.parent):
        p.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("email listener starting account=%s pid=%s", account, os.getpid())

    cfg = load_config()
    accts = cfg.get("channels", {}).get("email_accounts", {})
    a = accts.get(account)
    if not a or not a.get("enabled"):
        logging.error("account %r missing or not enabled in config.channels.email_accounts", account)
        return 3

    address = a.get("address")
    host = a.get("imap_host", "imap.gmail.com")
    port = int(a.get("imap_port", 993))
    pw_path = Path(a.get("password_path", "").replace("~", str(Path.home())))
    if not (address and pw_path.exists()):
        logging.error("missing address or password file: address=%s pw_path=%s", address, pw_path)
        return 4
    password = pw_path.read_text().strip()
    if not password:
        logging.error("empty password at %s", pw_path)
        return 5

    state = load_offset(offset_path)
    backoff = BACKOFF_MIN
    imap: imaplib.IMAP4_SSL | None = None

    hb_stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(heartbeat, hb_stop), daemon=True)
    hb_thread.start()

    while True:
        try:
            if imap is None:
                imap = connect(host, port, address, password)
                logging.info("connected as %s @ %s:%d", address, host, port)
            new_state = select_and_sync(account, imap, state)
            # Reset the backoff only on a poll that actually worked. Resetting it
            # after a successful connect() was wrong: when the server accepts the
            # login but refuses SELECT (iCloud "Server Busy"), every reconnect
            # dropped the delay back to 5s, so the listener hammered a server that
            # was explicitly asking it to wait. The backoff existed and could not
            # take effect.
            backoff = BACKOFF_MIN
            if new_state != state:
                state = new_state
                save_offset(offset_path, state)
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
            logging.info("email listener stopping (SIGINT)")
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

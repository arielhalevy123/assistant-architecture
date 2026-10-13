#!/usr/bin/env python3
"""
Telegram listener daemon for the assistant.

Long-polls getUpdates, filters strictly to the owner's chat id,
appends each accepted message to ~/.assistant/inbox/stream.jsonl,
downloads media into ~/.assistant/inbox/media/, touches a heartbeat
file each cycle so wedged-listener detectors can spot silence.

Zero third-party deps: stdlib urllib + json only.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STATE_ROOT = Path.home() / ".assistant"
CONFIG_PATH = STATE_ROOT / "config.json"
TOKEN_PATH = STATE_ROOT / "secrets" / "telegram_bot_token"
INBOX = STATE_ROOT / "inbox"
MEDIA = INBOX / "media"
STREAM = INBOX / "stream.jsonl"
OFFSET_PATH = STATE_ROOT / "state" / "tg_offset.json"
HEARTBEAT = STATE_ROOT / "state" / "listener_telegram.heartbeat"
LOG_PATH = STATE_ROOT / "logs" / "listener_telegram.log"

POLL_TIMEOUT = 15  # shorter long-poll so stuck sockets clear faster
BACKOFF_MIN = 2
BACKOFF_MAX = 30  # cap tighter so retries don't push heartbeat past healthcheck threshold
HEARTBEAT_INTERVAL = 10  # background thread touches the heartbeat every 10s so
#                          a stuck urlopen can't age the heartbeat past the
#                          healthcheck 60s threshold on its own


def _heartbeat_loop(stop: threading.Event) -> None:
    """Background touch loop.

    The main loop parks on urlopen for up to POLL_TIMEOUT+10 seconds
    at a time; if we tied heartbeat freshness to that loop, one stuck
    socket would push hb_age past the healthcheck alarm threshold and
    trigger a spurious ALARM DM. Touching from a daemon thread means
    the heartbeat only goes stale when the process itself is stuck,
    which is what we actually want to detect.
    """
    while not stop.is_set():
        try:
            HEARTBEAT.touch()
        except Exception:
            pass
        stop.wait(HEARTBEAT_INTERVAL)


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def load_token() -> str:
    token = TOKEN_PATH.read_text().strip()
    if not token or ":" not in token:
        raise RuntimeError(f"invalid token at {TOKEN_PATH}")
    return token


def load_offset() -> int:
    if OFFSET_PATH.exists():
        try:
            return int(json.loads(OFFSET_PATH.read_text())["offset"])
        except Exception:
            return 0
    return 0


def save_offset(offset: int) -> None:
    OFFSET_PATH.write_text(json.dumps({"offset": offset}))


def tg_call(token: str, method: str, params: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data or None, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def download_file(token: str, file_id: str, dest_dir: Path) -> Path | None:
    try:
        info = tg_call(token, "getFile", {"file_id": file_id}, timeout=30)
        if not info.get("ok"):
            return None
        file_path = info["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / f"{file_id}_{Path(file_path).name}"
        urllib.request.urlretrieve(url, str(local))
        return local
    except Exception as e:
        logging.warning("download_file failed for %s: %s", file_id, e)
        return None


def extract_media(token: str, msg: dict[str, Any], update_id: int) -> tuple[list[str], list[str]]:
    """Download media referenced by a Telegram message.

    Returns (all_saved_paths, voice_paths). voice_paths is the subset that should
    be transcribed — voice notes and video_notes. Other audio (background music,
    forwarded songs) is deliberately excluded from transcription: the owner does
    not narrate his intent by forwarding an mp3, and running whisper over a song
    is expensive noise on the stream.
    """
    saved: list[str] = []
    voice_paths: list[str] = []
    dest = MEDIA / str(update_id)
    for kind in ("voice", "audio", "video", "video_note", "document"):
        if kind in msg and "file_id" in msg[kind]:
            p = download_file(token, msg[kind]["file_id"], dest)
            if p:
                saved.append(str(p))
                if kind in ("voice", "video_note"):
                    voice_paths.append(str(p))
    if "photo" in msg and msg["photo"]:
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        p = download_file(token, largest["file_id"], dest)
        if p:
            saved.append(str(p))
    return saved, voice_paths


def _transcribe_paths(paths: list[str]) -> list[dict]:
    """Best-effort transcription of voice files. Import is lazy so a broken
    transcribe install (no model, no ffmpeg) does not stop the listener booting.

    Each result is {'path': str, 'text': str|None, 'language': str|None,
    'duration_s': float|None}. text=None means the file was captured but
    transcription failed — downstream should treat that as "voice, unknown
    content" rather than "no voice message".
    """
    if not paths:
        return []
    try:
        from transcribe import transcribe_or_none              # local module
    except ImportError as e:
        logging.warning("transcribe module missing: %s", e)
        return [{"path": p, "text": None, "language": None, "duration_s": None,
                 "error": "transcribe module missing"} for p in paths]

    out: list[dict] = []
    for p in paths:
        r = transcribe_or_none(Path(p), language="he")
        if r is None:
            out.append({"path": p, "text": None, "language": None,
                        "duration_s": None, "error": "transcribe unavailable"})
        else:
            out.append({"path": p, **r})
    return out


def summarize(msg: dict[str, Any], transcripts: list[dict] | None = None) -> str:
    """Human-readable summary for the record.

    If the message is a voice note that transcribed, the summary IS the transcript —
    so downstream ('Monitor', notify handlers, memory writers) see the words the
    owner said instead of the placeholder '[voice]'. Falls back to the placeholder
    when transcription is off or failed.
    """
    if "text" in msg:
        return msg["text"]
    if transcripts:
        speech = " ".join(t["text"] for t in transcripts if t.get("text"))
        if speech.strip():
            return speech.strip()
    for kind in ("voice", "audio", "video", "video_note", "photo", "document", "sticker", "location", "contact"):
        if kind in msg:
            return f"[{kind}]"
    return "[unknown]"


def process_update(token: str, owner_id: int, upd: dict[str, Any]) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat", {})
    sender = msg.get("from", {})
    chat_id = chat.get("id")

    if chat_id != owner_id:
        logging.info("dropping non-owner message from chat_id=%s sender=%s",
                     chat_id, sender.get("id"))
        return

    update_id = upd["update_id"]
    media, voice_paths = extract_media(token, msg, update_id)
    # Transcription runs synchronously here rather than in a queue: whisper.cpp on
    # M-series transcribes a typical 20-second voice note in under two seconds, and
    # keeping it in-band means the record on stream.jsonl already carries the words
    # by the time any downstream monitor tails it. Journalling it out of band would
    # split the "voice note arrived" event from the "transcript is available" event,
    # which every downstream would then have to re-join.
    transcripts = _transcribe_paths(voice_paths)

    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "channel": "telegram",
        "update_id": update_id,
        "message_id": msg.get("message_id"),
        "chat_id": chat_id,
        "from_id": sender.get("id"),
        "from_name": " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x),
        "text": msg.get("text"),
        "summary": summarize(msg, transcripts),
        "media_paths": media,
        "transcripts": transcripts,
        "raw": msg,
    }

    INBOX.mkdir(parents=True, exist_ok=True)
    with STREAM.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logging.info("inbox update_id=%s summary=%r", update_id, record["summary"][:120])


def main() -> int:
    for p in (INBOX, MEDIA, LOG_PATH.parent, HEARTBEAT.parent, OFFSET_PATH.parent):
        p.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("listener starting pid=%s", os.getpid())

    cfg = load_config()
    owner_id = int(cfg["owner"]["telegram_id"])
    token = load_token()

    # Sanity check the bot itself before entering the loop.
    me = tg_call(token, "getMe", timeout=15)
    if not me.get("ok"):
        logging.error("getMe failed: %s", me)
        return 2
    logging.info("bot @%s id=%s ready; owner=%s",
                 me["result"].get("username"), me["result"].get("id"), owner_id)

    offset = load_offset()
    backoff = BACKOFF_MIN

    hb_stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(hb_stop,), daemon=True)
    hb_thread.start()

    while True:
        try:
            resp = tg_call(
                token,
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message", "edited_message"])},
                timeout=POLL_TIMEOUT + 10,
            )
            if not resp.get("ok"):
                logging.error("getUpdates not ok: %s", resp)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            backoff = BACKOFF_MIN
            for upd in resp.get("result", []):
                try:
                    process_update(token, owner_id, upd)
                except Exception as e:
                    logging.exception("process_update failed: %s", e)
                offset = max(offset, upd["update_id"] + 1)
                save_offset(offset)
        except KeyboardInterrupt:
            logging.info("listener stopping (SIGINT)")
            return 0
        except (TimeoutError, urllib.error.URLError) as e:
            # URLError is only benign when it wraps a timeout; a DNS or refused-connection
            # URLError is a real fault and must stay loud.
            if isinstance(e, urllib.error.URLError) and not isinstance(
                    getattr(e, "reason", None), TimeoutError):
                logging.exception("poll cycle failed: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            # A long-poll that returns no update inside the timeout window is the normal
            # quiet case, not a fault: ~a dozen a day, self-recovering every time. Logged
            # at ERROR with a full traceback it drowned the real signal -- every ERROR grep
            # (healthcheck, session monitors) fired on it, so the alerts got ignored, which
            # is how the genuinely broken things stayed invisible. WARNING, one line, no
            # traceback. Anything that is NOT a timeout still gets the loud path below.
            logging.warning("poll cycle timed out (benign, retrying): %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        except Exception as e:
            logging.exception("poll cycle failed: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


if __name__ == "__main__":
    sys.exit(main())

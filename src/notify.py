#!/usr/bin/env python3
"""Send a text message from the assistant to the owner on Telegram.

Usage:
    notify.py "message text"            text, plus a voice note if voice is set up and the text is short
    notify.py --no-voice "message"      text only
    echo "message text" | notify.py

Voice needs edge-tts (in ~/.assistant/venv) and ffmpeg. Without them it sends text only.
Set "voice" in config.json (default he-IL-HilaNeural).

Reads the owner id from ~/.assistant/config.json and the bot token from
~/.assistant/secrets/telegram_bot_token. Prints the Telegram message id.
Long messages are split at 4000 characters.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path.home() / ".assistant"
EDGE_TTS = str(ROOT / "venv" / "bin" / "edge-tts")
VOICE_MAX_CHARS = 500  # longer texts are text-only: a long voice note is a chore to listen to


def send_voice(token: str, owner: int, text: str, voice: str) -> None:
    """Render text to an OGG/Opus voice note and send it. Silent no-op if the tools are missing."""
    if not (os.path.exists(EDGE_TTS) and shutil.which("ffmpeg")):
        return
    tmp = Path(tempfile.mkdtemp(prefix="assistant_tts_"))
    mp3, ogg = tmp / "v.mp3", tmp / "v.ogg"
    r = subprocess.run([EDGE_TTS, "--voice", voice, "--text", text, "--write-media", str(mp3)],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        return
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", str(ogg)],
                   check=True, timeout=60)
    boundary = "----assistant" + os.urandom(8).hex()
    body = b""
    for name, value in (("chat_id", str(owner).encode()),):
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value + b"\r\n"
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="voice"; filename="v.ogg"\r\n'
             "Content-Type: audio/ogg\r\n\r\n").encode() + ogg.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendVoice", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=60).read()


def send(text: str, voice_ok: bool = True) -> list[int]:
    cfg = json.loads((ROOT / "config.json").read_text())
    token = (ROOT / "secrets" / "telegram_bot_token").read_text().strip()
    owner = int(cfg["owner"]["telegram_id"])
    ids = []
    for i in range(0, len(text), 4000):
        data = urllib.parse.urlencode({"chat_id": owner, "text": text[i : i + 4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            ids.append(json.load(r)["result"]["message_id"])
    if voice_ok and len(text) <= VOICE_MAX_CHARS:
        try:
            send_voice(token, owner, text, cfg.get("voice", "he-IL-HilaNeural"))
        except Exception as e:  # a failed voice note must never lose the text
            print(f"(voice skipped: {e})", file=sys.stderr)
    return ids


if __name__ == "__main__":
    args = sys.argv[1:]
    voice_ok = "--no-voice" not in args
    args = [a for a in args if a != "--no-voice"]
    body = " ".join(args) if args else sys.stdin.read()
    if not body.strip():
        sys.exit("nothing to send")
    print(send(body.strip(), voice_ok))

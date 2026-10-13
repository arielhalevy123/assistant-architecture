#!/usr/bin/env python3
"""Send a text message from the assistant to the owner on Telegram.

Usage:
    notify.py "message text"
    echo "message text" | notify.py

Reads the owner id from ~/.assistant/config.json and the bot token from
~/.assistant/secrets/telegram_bot_token. Prints the Telegram message id.
Long messages are split at 4000 characters.
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path.home() / ".assistant"


def send(text: str) -> list[int]:
    cfg = json.loads((ROOT / "config.json").read_text())
    token = (ROOT / "secrets" / "telegram_bot_token").read_text().strip()
    owner = int(cfg["owner"]["telegram_id"])
    ids = []
    for i in range(0, len(text), 4000):
        data = urllib.parse.urlencode({"chat_id": owner, "text": text[i : i + 4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            ids.append(json.load(r)["result"]["message_id"])
    return ids


if __name__ == "__main__":
    body = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    if not body.strip():
        sys.exit("nothing to send")
    print(send(body.strip()))

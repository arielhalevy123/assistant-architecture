#!/usr/bin/env python3
"""Monitor-side filter for the assistant's inbound stream.

Reads stream.jsonl lines on stdin, prints the ones worth surfacing.
Suppresses only @newsletter broadcasts: they are one-way channels that never
carry anything addressed to the owner personally. Group chats are NOT suppressed:
they often carry live coordination.

Unparseable lines are always passed through: a filter that silently eats
malformed input hides exactly the failures worth seeing.

Usage:  tail -n0 -F ~/.assistant/inbox/stream.jsonl | monitor_filter.py
"""
import json, sys

for line in sys.stdin:
    line = line.rstrip('\n')
    if not line.strip():
        continue
    try:
        e = json.loads(line)
    except Exception:
        print(line, flush=True)
        continue
    if str(e.get('chat_id') or '').endswith('@newsletter'):
        continue
    print(line, flush=True)

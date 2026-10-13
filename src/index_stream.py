#!/usr/bin/env python3
"""Full-text search over everything the assistant has ever seen.

Why this and not a vector database
----------------------------------
the owner asked (2026-09-06) why the assistant's memory lives in flat files rather than in
embeddings. The honest answer is that the flat file is the *record* and must stay
that way: `stream.jsonl` is append-only, greppable, backup-able and repairable by
hand, and `verify_owner_message.py` can use it as evidence precisely because it is
a log and not a model. An embedding index cannot be audited by eye, and its
failure mode is silent — a bad retrieval returns something plausible rather than
an error, which is the exact failure that produced fabricated records here once
already.

So this is an *index*, never a store. It is derived state: delete the database and
rebuild it from the stream, and nothing is lost. Semantic search can be layered on
later using the same rule — the stream stays the source of truth, and anything
built over it stays disposable.

SQLite FTS5 gets most of the value for none of the risk: substring and prefix
matching, boolean queries, phrase queries, ranked results, and it handles Hebrew
because the unicode61 tokenizer is Unicode-aware.

Usage
-----
    index_stream.py --rebuild              # build from scratch
    index_stream.py --update               # append only what is new (fast, idempotent)
    index_stream.py "salesforce OR nvidia"  # search
    index_stream.py --channel whatsapp --since 2026-09-01 "wedding"
    index_stream.py --stats
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STATE_ROOT = Path.home() / ".assistant"
STREAM = STATE_ROOT / "inbox" / "stream.jsonl"
DB = STATE_ROOT / "db" / "stream_index.sqlite3"


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
        -- The FTS table holds only what is searchable. Metadata used for
        -- filtering lives in a plain table so it can be indexed and compared
        -- numerically; FTS5 columns are all text and would make range queries
        -- on time awkward and slow.
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            body,
            who,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        CREATE TABLE IF NOT EXISTS messages (
            rowid    INTEGER PRIMARY KEY,   -- matches messages_fts rowid
            ts       REAL    NOT NULL,
            iso      TEXT    NOT NULL,
            channel  TEXT    NOT NULL,
            who      TEXT,
            from_me  INTEGER DEFAULT 0,
            msg_key  TEXT UNIQUE            -- dedupe key, see msg_key()
        );

        CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);
        CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, ts);

        -- Where the last --update stopped, so re-running is cheap and idempotent.
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    con.commit()


def msg_key(e: dict) -> str:
    """Stable identity for a stream line, so --update never double-inserts.

    Channels number things differently, so fall back through the identifiers each
    one actually provides, and finally to timestamp+text.
    """
    ch = e.get("channel", "?")
    for field in ("update_id", "message_id", "uid", "id"):
        if e.get(field) is not None:
            return f"{ch}:{field}:{e[field]}"
    return f"{ch}:ts:{e.get('ts')}:{hash((e.get('text') or '')[:200]) & 0xFFFFFFFF}"


def extract(e: dict) -> tuple[str, str] | None:
    """(searchable_body, who) for one stream line, or None to skip it."""
    ch = e.get("channel")
    if not ch:
        return None

    if ch in ("email", "gmail"):
        who = e.get("from") or "?"
        # The summary is usually just the subject repeated, which made every
        # snippet show the same line twice. Keep order, drop exact repeats.
        seen: set[str] = set()
        parts = []
        for piece in (e.get("subject") or "",
                      e.get("snippet") or e.get("summary") or "",
                      (e.get("body") or "")[:4000]):
            piece = piece.strip()
            if piece and piece not in seen:
                seen.add(piece)
                parts.append(piece)
        body = " ".join(parts)
    else:
        who = e.get("from_name") or e.get("from_username") or str(e.get("from_id") or "?")
        body = e.get("text") or e.get("summary") or ""
        # Voice notes are only searchable once transcribed; fold those in.
        for t in e.get("transcripts") or []:
            if isinstance(t, str):
                body += " " + t
            elif isinstance(t, dict):
                body += " " + (t.get("text") or "")
        if not body.strip():
            body = f"[{e.get('type') or 'media'}]"

    body = " ".join(body.split())
    return (body[:8000], str(who)[:120])


def index_lines(con: sqlite3.Connection, lines, *, quiet=False) -> tuple[int, int]:
    added = skipped = 0
    cur = con.cursor()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except Exception:
            skipped += 1
            continue
        got = extract(e)
        if not got:
            skipped += 1
            continue
        body, who = got
        key = msg_key(e)
        try:
            cur.execute("INSERT INTO messages_fts (body, who) VALUES (?, ?)", (body, who))
            cur.execute(
                "INSERT INTO messages (rowid, ts, iso, channel, who, from_me, msg_key) "
                "VALUES (?,?,?,?,?,?,?)",
                (cur.lastrowid, e.get("ts") or 0, e.get("iso") or "",
                 e.get("channel"), who, 1 if e.get("from_me") else 0, key),
            )
            added += 1
        except sqlite3.IntegrityError:
            # Already indexed. Roll back the orphaned FTS row so the two tables
            # cannot drift apart.
            cur.execute("DELETE FROM messages_fts WHERE rowid = ?", (cur.lastrowid,))
            skipped += 1
    con.commit()
    if not quiet:
        print(f"indexed {added}, skipped {skipped}")
    return added, skipped


def rebuild(con: sqlite3.Connection) -> None:
    con.executescript("DROP TABLE IF EXISTS messages_fts; DROP TABLE IF EXISTS messages;")
    con.commit()
    ensure_schema(con)
    t0 = time.time()
    with STREAM.open(encoding="utf-8", errors="replace") as f:
        index_lines(con, f)
    con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('last_size', ?)",
                (str(STREAM.stat().st_size),))
    con.commit()
    print(f"rebuilt in {time.time()-t0:.1f}s -> {DB}")


def update(con: sqlite3.Connection) -> None:
    """Index only bytes appended since last run.

    Falls back to a full pass if the file shrank — that means it was rotated or
    truncated, and resuming from a stale offset would silently skip everything.
    """
    ensure_schema(con)
    row = con.execute("SELECT v FROM meta WHERE k='last_size'").fetchone()
    last = int(row["v"]) if row else 0
    size = STREAM.stat().st_size
    if size < last:
        print("stream shrank (rotated?) — doing a full rebuild")
        rebuild(con)
        return
    if size == last:
        print("no new lines")
        return
    with STREAM.open("rb") as f:
        f.seek(last)
        chunk = f.read().decode("utf-8", "replace")
    index_lines(con, chunk.splitlines())
    con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('last_size', ?)", (str(size),))
    con.commit()


def search(con: sqlite3.Connection, query: str, *, channel=None, since=None,
           limit=25, include_own=True) -> list[sqlite3.Row]:
    sql = ["SELECT m.iso, m.channel, m.who, m.from_me, "
           "       snippet(messages_fts, 0, '»', '«', '…', 18) AS snip "
           "FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid "
           "WHERE messages_fts MATCH ?"]
    args: list = [query]
    if channel:
        sql.append("AND m.channel = ?")
        args.append(channel)
    if since:
        sql.append("AND m.ts >= ?")
        args.append(since)
    if not include_own:
        sql.append("AND m.from_me = 0")
    sql.append("ORDER BY rank, m.ts DESC LIMIT ?")
    args.append(limit)
    return con.execute(" ".join(sql), args).fetchall()


def parse_since(s: str) -> float:
    """Accept 2026-09-01, or shorthand like 7d / 12h."""
    if s.endswith("d") and s[:-1].isdigit():
        return (datetime.now() - timedelta(days=int(s[:-1]))).timestamp()
    if s.endswith("h") and s[:-1].isdigit():
        return (datetime.now() - timedelta(hours=int(s[:-1]))).timestamp()
    return datetime.fromisoformat(s).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="*", help="FTS5 query, e.g. 'salesforce OR nvidia'")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--channel")
    ap.add_argument("--since", help="2026-09-01, or 7d / 12h")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--no-own", action="store_true", help="exclude the owner's own messages")
    args = ap.parse_args()

    con = connect()
    ensure_schema(con)

    if args.rebuild:
        rebuild(con); return 0
    if args.update:
        update(con); return 0
    if args.stats:
        n = con.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        print(f"indexed messages: {n}")
        print(f"db size         : {DB.stat().st_size/1e6:.1f} MB" if DB.exists() else "no db")
        for r in con.execute("SELECT channel, COUNT(*) c, MIN(iso) a, MAX(iso) b "
                             "FROM messages GROUP BY channel ORDER BY c DESC"):
            print(f"  {r['channel']:10} {r['c']:6}   {r['a'][:10]} .. {r['b'][:10]}")
        return 0

    if not args.query:
        ap.print_help(); return 2

    try:
        rows = search(con, " ".join(args.query), channel=args.channel,
                      since=parse_since(args.since) if args.since else None,
                      limit=args.limit, include_own=not args.no_own)
    except sqlite3.OperationalError as e:
        print(f"query error: {e}\n"
              "FTS5 syntax: bare words are AND; use OR, NOT, \"exact phrase\", pre*",
              file=sys.stderr)
        return 1

    if not rows:
        print("no matches"); return 0
    for r in rows:
        mark = "→" if r["from_me"] else "←"
        print(f"{r['iso'][:16]} {mark} [{r['channel']:8}] {(r['who'] or '?')[:24]:26} {r['snip']}")
    print(f"\n{len(rows)} result(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Index Claude Code conversations into the assistant's knowledge graph.

Hook mode (PreCompact / SessionEnd): reads the hook JSON on stdin, extracts the
conversation text added since the last run, redacts secrets, and starts a
detached worker. Worker mode runs a headless Claude that writes entities,
relations and observations through the MCP memory server.

Usage:
    memory_index.py                         # hook mode, JSON on stdin
    memory_index.py --file notes.txt        # index a text file now (backfill)
    memory_index.py --worker <excerpt> <transcript> <offset>
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GRAPH_DIR = Path.home() / ".assistant" / "graph"
STATE = GRAPH_DIR / "index_state.json"
LOCK = GRAPH_DIR / "index.lock"
LOGS = GRAPH_DIR / "logs"
MCP_CONFIG = GRAPH_DIR / "mcp.json"
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
MIN_CHARS = 400
MAX_CHARS = 120_000  # newest text wins when a session produced more than this

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),          # telegram bot token
    re.compile(r"\b(?:ghp|gho|github_pat|xox[abp])_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),                     # long opaque strings
]

PROMPT = """You maintain the owner's personal knowledge graph through the memory MCP tools.
Below is an excerpt of a conversation between the owner and their assistant.
Update the graph so a future session can pick up any topic from it.

Steps:
1. Call read_graph (or search_nodes for the main topics) to see what exists. Reuse existing entity names exactly; never create near-duplicates.
2. Create or update entities for durable things: people, projects, studies/courses, jobs, companies, decisions, preferences and standing rules, deadlines, files/repos/paths that matter.
   entityType examples: person, project, course, company, decision, rule, event, artifact.
3. Add observations as short self-contained facts, each starting with the date in DD.MM.YYYY form when known (e.g. "07.10.2026: decision made, next step is Y"). Include status, next steps, file paths, numbers, links.
4. When a fact replaces an older one, delete the outdated observation and add the new one.
5. Create relations in active voice (e.g. Owner -works_on-> Project X, Project X -has_repo-> project-x-repo).
6. Skip chit-chat, routine noise that led nowhere, and transient tool noise.
7. NEVER store secrets: API keys, tokens, passwords, app passwords, card or ID numbers, even partially. Text marked [REDACTED] stays out.
Do not output anything except a one-line summary of what you changed.

EXCERPT:
"""


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "index.log", "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_offset(transcript: str, offset: int) -> None:
    state = load_state()
    state[transcript] = offset
    STATE.write_text(json.dumps(state, indent=1))


def message_text(entry: dict) -> str:
    msg = entry.get("message") or {}
    role = msg.get("role")
    content = msg.get("content")
    if role not in ("user", "assistant") or entry.get("isMeta"):
        return ""
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    out = []
    for p in parts:
        if "<task-notification>" in p:
            # monitor events: keep only the owner's own Telegram messages
            out += re.findall(r"TG [^\n<]*", p)
            continue
        p = re.sub(r"<system-reminder>.*?</system-reminder>", "", p, flags=re.S)
        if p.strip():
            out.append(p.strip())
    if not out:
        return ""
    who = "Owner" if role == "user" else "Assistant"
    return f"{who}: " + "\n".join(out)


def extract(transcript: str, offset: int) -> tuple[str, int]:
    lines = []
    with open(transcript, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = offset + len(data)
    for raw in data.splitlines():
        try:
            t = message_text(json.loads(raw))
        except Exception:
            continue
        if t:
            lines.append(t)
    return "\n\n".join(lines), end


def run_claude(text: str) -> bool:
    env = dict(os.environ, ASSISTANT_MEMORY_INDEXER="1")
    cmd = [CLAUDE, "-p", "--model", "sonnet",
           "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
           "--allowedTools", "mcp__memory__*",
           "--disallowedTools", "Bash,Edit,Write,WebFetch,WebSearch"]
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        r = subprocess.run(cmd, input=PROMPT + text, text=True, capture_output=True,
                           cwd=str(GRAPH_DIR), env=env, timeout=1800)
    log(f"claude rc={r.returncode} out={r.stdout.strip()[:300]!r} err={r.stderr.strip()[:300]!r}")
    return r.returncode == 0


def worker(excerpt_path: str, transcript: str, offset: int) -> None:
    text = Path(excerpt_path).read_text()
    ok = run_claude(text)
    if ok and transcript:
        save_offset(transcript, offset)
    if ok:
        Path(excerpt_path).unlink(missing_ok=True)


def spawn(text: str, transcript: str, offset: int) -> None:
    excerpt = GRAPH_DIR / f"pending_{int(time.time()*1000)}.txt"
    excerpt.write_text(text)
    os.chmod(excerpt, 0o600)
    subprocess.Popen([sys.executable, __file__, "--worker", str(excerpt), transcript, str(offset)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(LOGS / "worker.err", "a"), start_new_session=True)


def main() -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--file":
        ok = run_claude(redact(Path(sys.argv[2]).read_text())[-MAX_CHARS:])
        sys.exit(0 if ok else 1)
    if os.environ.get("ASSISTANT_MEMORY_INDEXER"):
        return  # the indexer's own headless session must not index itself
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return
    transcript = hook.get("transcript_path") or ""
    if not transcript or not os.path.exists(transcript):
        return
    offset = load_state().get(transcript, 0)
    text, end = extract(transcript, offset)
    if len(text) < MIN_CHARS:
        save_offset(transcript, end)
        return
    log(f"{hook.get('hook_event_name')} session={hook.get('session_id')} chars={len(text)}")
    spawn(redact(text)[-MAX_CHARS:], transcript, end)


if __name__ == "__main__":
    main()

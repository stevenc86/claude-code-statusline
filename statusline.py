#!/usr/bin/env python3
"""Claude Code statusline.

Layout (lines printed top → bottom):
    L1  🤖 model · ⚡ effort · ⎇ branch · 🧠 ctx · 📊 cache · ⏱ session · ⌛ last
    L2  💰 block/wk/mo · 🔥 burn →wk · ✓ todos · 🔧 tools · 🌐 bg · 📝 last skill
    L3  👥 N · ├ agent A · ├ agent B · └ agent C       (only if agents > 0)
    L4  5h ████████  ##%  (reset countdown)
    L5  7d ███       ##%  (reset countdown)

Performance:
- Rendering reads three JSON files (the cost/ctx cache, the plan-usage cache,
  and settings.json) and is ~20 ms warm. Both caches are refreshed by detached
  subprocesses, so the render path never blocks.
- The transcript and git index are re-scanned only when their mtime changes.
- Refreshes are single-flight: spawning a refresh worker is gated by an on-disk
  lock, so an active session can never pile up redundant refresh subprocesses.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# concurrent.futures / tempfile / timedelta are imported lazily inside the
# refresh path only; keeping them off the hot render path saves ~7 ms/render.

__version__ = "1.0.0"

# ---------- visual config ----------
BAR_WIDTH = 30
FILL_CHAR = "#"
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[38;2;255;100;100m"
GREEN = "\033[38;2;152;216;170m"

# Vivid Macaron palette
GREEN_RGB = (152, 216, 170)  # pistachio #98D8AA
AMBER_RGB = (255, 217, 162)  # mango #FFD9A2
RED_RGB = (255, 158, 178)    # raspberry #FF9EB2

# Warning thresholds
CTX_WARN_PCT = 80
BURN_PROJECTION_WARN_HRS = 24
BAR_DANGER_PCT = 90

# ---------- paths / TTL ----------
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
USAGE_CACHE = CLAUDE_DIR / "plugins" / "oh-my-claudecode" / ".usage-cache-anthropic.json"
OMC_HUD = CLAUDE_DIR / "hud" / "omc-hud.mjs"
LOCAL_CACHE = CLAUDE_DIR / ".statusline-cache.json"
REFRESH_LOCK = CLAUDE_DIR / ".statusline-refresh.lock"
USAGE_LOCK = CLAUDE_DIR / ".statusline-usage.lock"
CACHE_STALE_SECS = 60
BG_LOOKBACK_SECS = 30 * 60  # how far back to count bg Bash tool_uses
# Lock TTLs: a lock older than this is treated as abandoned (worker died) and
# reclaimed. REFRESH must exceed the slowest ccusage timeout (30 s) so a healthy
# worker is never pre-empted; USAGE only debounces the node HUD spawn.
REFRESH_LOCK_TTL = 45
USAGE_LOCK_TTL = 15

# Tree characters for agent display
TREE_BRANCH = "├"
TREE_LAST = "└"


# ---------- helpers: color / format ----------
def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rgb_at(pct):
    pct = max(0.0, min(100.0, pct))
    if pct < 50:
        return _mix(GREEN_RGB, AMBER_RGB, pct / 50)
    return _mix(AMBER_RGB, RED_RGB, (pct - 50) / 50)


def ansi(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


def render_bar(pct, width=BAR_WIDTH):
    fill = max(0, min(width, int(round(pct * width / 100))))
    parts = []
    for i in range(width):
        if i < fill:
            pos = (i / max(width - 1, 1)) * 100
            r, g, b = rgb_at(pos)
            parts.append(f"{ansi(r, g, b)}{FILL_CHAR}")
        else:
            parts.append(" ")
    parts.append(RESET)
    return "".join(parts)


def color_pct(pct):
    p = int(round(pct))
    if pct >= 100:
        r, g, b = 255, 0, 0
    else:
        r, g, b = rgb_at(pct)
    style = BOLD if pct >= BAR_DANGER_PCT else ""
    return f"{style}{ansi(r, g, b)}{p}%{RESET}"


def fmt_remaining(seconds):
    seconds = max(0, int(seconds))
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h{mins % 60:02d}m"
    return f"{hours // 24}d{hours % 24:02d}h"


def fmt_duration_short(seconds):
    """Short duration: 45s / 2m / 1h32m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h{mins % 60:02d}m"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def seconds_until(iso_ts):
    if not iso_ts:
        return 0
    try:
        target = parse_iso(iso_ts)
        return max(0, int((target - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        return 0


def to_epoch(iso_ts):
    if not iso_ts:
        return None
    try:
        return parse_iso(iso_ts).timestamp()
    except Exception:
        return None


def extract_cwd(data):
    """Resolve the working dir from statusline input, tolerating a null workspace."""
    ws = data.get("workspace")
    if not isinstance(ws, dict):
        ws = {}
    return data.get("cwd") or ws.get("current_dir", "") or ""


# ---------- effort (settings.json) ----------
def get_effort():
    try:
        with open(CLAUDE_DIR / "settings.json") as f:
            return str(json.load(f).get("effortLevel", "-"))
    except Exception:
        return "-"


# ---------- single-pass transcript scanner ----------
_CMD_RE = re.compile(
    r"<command-(?:name|message)>([a-zA-Z][a-zA-Z0-9_:.\-]{1,40})</command-(?:name|message)>"
)
_TASK_DONE_RE = re.compile(r"<task-id>([a-zA-Z0-9]+)</task-id>")


def scan_transcript(transcript_path):
    """One file pass; returns all derived metrics.

    Empty/missing path returns sane defaults so callers don't crash.
    """
    out = {
        "last_slash": "-",
        "agents_running": 0,
        "agent_details": [],            # list of {name, start_unix}
        "session_start_unix": None,
        "last_user_unix": None,
        "last_assistant_end_unix": None,
        "last_latency_secs": None,
        "tool_count": 0,
        "cache_hit_pct": None,
        "todos_pending": 0,
        "todos_total": 0,
        "bg_count": 0,
    }
    if not transcript_path or not os.path.exists(transcript_path):
        return out

    task_started: dict[str, dict] = {}   # tool_use_id -> {name, start_unix}
    task_finished: set[str] = set()
    todos_created = 0
    todos_closed = 0
    cache_read_total = 0
    cache_create_total = 0
    input_token_total = 0
    bg_completed_in_msgs: set[str] = set()
    bg_recent_count = 0
    now_unix = time.time()

    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Cheap rejection — skip lines that can't contain anything we want
                if ('"role"' not in line
                        and '"type"' not in line
                        and 'command-' not in line):
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue

                ts_unix = to_epoch(entry.get("timestamp"))
                if ts_unix and out["session_start_unix"] is None:
                    out["session_start_unix"] = ts_unix

                etype = entry.get("type")
                msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                role = msg.get("role")
                content = msg.get("content") if isinstance(msg, dict) else None

                # Track conversational turn timing
                if etype == "user" and ts_unix:
                    out["last_user_unix"] = ts_unix
                if etype == "assistant" and ts_unix:
                    out["last_assistant_end_unix"] = ts_unix

                # Slash command (only from user text blocks; avoid bash strings)
                if etype == "user" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            for m in _CMD_RE.findall(c.get("text", "")):
                                out["last_slash"] = m

                # Aggregate usage (from assistant entries with .message.usage)
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    cache_read_total += int(usage.get("cache_read_input_tokens", 0) or 0)
                    cache_create_total += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    input_token_total += int(usage.get("input_tokens", 0) or 0)

                # Tool calls + agent detection + bg detection
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "tool_use":
                            out["tool_count"] += 1
                            name = c.get("name")
                            tid = c.get("id")
                            inp = c.get("input") if isinstance(c.get("input"), dict) else {}

                            if name == "Task" and tid:
                                sub = inp.get("subagent_type") or inp.get("description") or "agent"
                                task_started[tid] = {
                                    "name": str(sub)[:14],
                                    "start_unix": ts_unix or now_unix,
                                }
                            elif name == "TaskCreate":
                                todos_created += 1
                            elif name == "TaskUpdate":
                                status = inp.get("status")
                                if status in ("completed", "deleted"):
                                    todos_closed += 1
                            elif name == "Bash" and inp.get("run_in_background"):
                                if ts_unix and (now_unix - ts_unix) <= BG_LOOKBACK_SECS:
                                    bg_recent_count += 1
                        elif ctype == "tool_result":
                            tid = c.get("tool_use_id")
                            if tid and tid in task_started:
                                task_finished.add(tid)
                            # Look for <task-id>X</task-id> patterns in result text
                            txt = c.get("content")
                            if isinstance(txt, str):
                                for m in _TASK_DONE_RE.findall(txt):
                                    bg_completed_in_msgs.add(m)
                            elif isinstance(txt, list):
                                for it in txt:
                                    if isinstance(it, dict) and isinstance(it.get("text"), str):
                                        for m in _TASK_DONE_RE.findall(it["text"]):
                                            bg_completed_in_msgs.add(m)

                # System messages may contain task-notifications too
                if etype == "system" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and isinstance(c.get("text"), str):
                            for m in _TASK_DONE_RE.findall(c["text"]):
                                bg_completed_in_msgs.add(m)
    except Exception:
        pass

    # Agent breakdown — only those without matching results
    running = []
    for tid, info in task_started.items():
        if tid not in task_finished:
            running.append(info)
    out["agents_running"] = len(running)
    out["agent_details"] = running

    # Last response latency
    if out["last_user_unix"] and out["last_assistant_end_unix"]:
        diff = out["last_assistant_end_unix"] - out["last_user_unix"]
        if diff > 0:
            out["last_latency_secs"] = int(diff)

    # Cache hit rate over the whole session
    denom = cache_read_total + cache_create_total + input_token_total
    if denom > 0:
        out["cache_hit_pct"] = round(cache_read_total / denom * 100, 1)

    # Todos
    out["todos_total"] = todos_created
    out["todos_pending"] = max(0, todos_created - todos_closed)

    # bg count: rough — recent bg starts that we haven't seen a task-completion msg for.
    out["bg_count"] = max(0, bg_recent_count - len(bg_completed_in_msgs))
    return out


# ---------- git ----------
def get_git_status(cwd):
    """Branch + ahead/behind + uncommitted line diff. Empty dict on failure."""
    info = {"branch": None, "ahead": 0, "behind": 0, "added": 0, "removed": 0}
    if not cwd:
        return info
    try:
        head = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if head.returncode != 0:
            return info
        info["branch"] = head.stdout.strip()

        # Ahead/behind vs upstream
        ab = subprocess.run(
            ["git", "-C", cwd, "rev-list", "--left-right", "--count", "@{u}...HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if ab.returncode == 0:
            parts = ab.stdout.strip().split()
            if len(parts) == 2:
                info["behind"] = int(parts[0])
                info["ahead"] = int(parts[1])

        # Uncommitted line diff (vs HEAD; includes staged + unstaged but not untracked)
        ns = subprocess.run(
            ["git", "-C", cwd, "diff", "HEAD", "--numstat"],
            capture_output=True, text=True, timeout=5,
        )
        if ns.returncode == 0:
            for ln in ns.stdout.splitlines():
                cols = ln.split("\t")
                if len(cols) >= 2:
                    a, r = cols[0], cols[1]
                    if a.isdigit():
                        info["added"] += int(a)
                    if r.isdigit():
                        info["removed"] += int(r)
    except Exception:
        pass
    return info


def git_index_mtime(cwd):
    try:
        return os.path.getmtime(os.path.join(cwd, ".git", "index"))
    except Exception:
        return None


def transcript_mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except Exception:
        return None


# ---------- Anthropic plan-usage cache (maintained by OMC HUD) ----------
def get_plan_usage():
    info = {
        "five_pct": None, "five_reset": None,
        "wk_pct": None, "wk_reset": None,
        "cache_age": None,
    }
    try:
        cache = json.loads(USAGE_CACHE.read_text())
        data = cache.get("data") or {}
        info["five_pct"] = data.get("fiveHourPercent")
        info["five_reset"] = data.get("fiveHourResetsAt")
        info["wk_pct"] = data.get("weeklyPercent")
        info["wk_reset"] = data.get("weeklyResetsAt")
        ts_ms = cache.get("timestamp")
        if ts_ms:
            info["cache_age"] = (time.time() * 1000 - ts_ms) / 1000
    except Exception:
        pass
    return info


def refresh_usage_cache_async(input_json):
    if not OMC_HUD.exists():
        return
    try:
        p = subprocess.Popen(
            ["node", str(OMC_HUD)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if p.stdin:
            p.stdin.write(json.dumps(input_json).encode())
            p.stdin.close()
    except Exception:
        pass


# ---------- local cache ----------
def read_local_cache():
    try:
        return json.loads(LOCAL_CACHE.read_text())
    except Exception:
        return {}


def write_local_cache_atomic(data):
    import tempfile
    LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".statusline-cache.", suffix=".tmp", dir=str(LOCAL_CACHE.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, LOCAL_CACHE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# ---------- spawn locks (single-flight refresh) ----------
def try_acquire_lock(lock_path, ttl):
    """Atomically claim the right to spawn a background worker.

    Returns True if the caller may spawn. Uses an exclusive-create lock file; a
    lock older than *ttl* seconds is treated as abandoned (its worker died) and
    reclaimed. Fails open only when the lock directory itself is unusable, so a
    broken lock can never permanently disable refreshes.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except Exception:
            return False
        if age < ttl:
            return False
        # Stale lock: bump its mtime so concurrent renders still back off, then
        # let this caller reclaim it.
        try:
            os.utime(lock_path, None)
        except Exception:
            return False
        return True
    except Exception:
        return False


def release_lock(lock_path):
    try:
        os.unlink(lock_path)
    except Exception:
        pass


# ---------- ccusage callers (refresh only) ----------
def _ccusage_blocks():
    out = subprocess.run(
        ["ccusage", "blocks", "--active", "--json"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    blocks = (json.loads(out).get("blocks") or [])
    if not blocks:
        return {"block_cost": None, "burn": None}
    b = blocks[0]
    burn = b.get("burnRate") or {}
    return {
        "block_cost": float(b.get("costUSD", 0)),
        "burn": float(burn.get("costPerHour", 0)),
    }


def _ccusage_monthly():
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    out = subprocess.run(
        ["ccusage", "monthly", "--json"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    months = json.loads(out).get("monthly") or []
    for e in months:
        if e.get("period") == period:
            return {"monthly_cost": float(e.get("totalCost", 0))}
    # Timezone boundary or format drift: fall back to the most recent month
    # rather than reporting $0.
    if months:
        latest = max(months, key=lambda e: e.get("period", ""))
        return {"monthly_cost": float(latest.get("totalCost", 0))}
    return {"monthly_cost": 0.0}


def _ccusage_plan_week(reset_iso):
    from datetime import timedelta
    if not reset_iso:
        return {"plan_week_cost": 0.0, "plan_week_reset": None}
    reset_at = parse_iso(reset_iso)
    period_start = reset_at - timedelta(days=7)
    since = period_start.strftime("%Y%m%d")
    out = subprocess.run(
        ["ccusage", "daily", "--since", since, "--json"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    entries = json.loads(out).get("daily") or []
    total = sum(float(e.get("totalCost", 0)) for e in entries)
    return {"plan_week_cost": total, "plan_week_reset": reset_iso}


_CTX_RE = re.compile(r"🧠\s+([\d,]+)\s*\((\d+(?:\.\d+)?)\s*%\)")
_CTX_PCT_RE = re.compile(r"🧠\s+(\d+(?:\.\d+)?)\s*%")


def _ccusage_ctx(input_json):
    raw = json.dumps(input_json)
    out = subprocess.run(
        ["ccusage", "statusline"],
        input=raw, capture_output=True, text=True, timeout=10,
    ).stdout
    m = _CTX_RE.search(out)
    if m:
        return {"ctx_tokens": int(m.group(1).replace(",", "")), "ctx_pct": float(m.group(2))}
    m = _CTX_PCT_RE.search(out)
    if m:
        return {"ctx_tokens": None, "ctx_pct": float(m.group(1))}
    return {"ctx_tokens": None, "ctx_pct": None}


# ---------- refresh worker ----------
def refresh_local_cache(input_json):
    """Parallel: 4 ccusage calls + transcript scan + git status. Atomic write.

    Always releases REFRESH_LOCK on exit so the next mtime change can refresh.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        prev = read_local_cache()
        plan = get_plan_usage()
        reset_iso = plan.get("wk_reset")
        transcript = input_json.get("transcript_path", "")
        cwd = extract_cwd(input_json)

        tasks = {
            "blocks": _ccusage_blocks,
            "monthly": _ccusage_monthly,
            "plan_week": lambda: _ccusage_plan_week(reset_iso),
            "ctx": lambda: _ccusage_ctx(input_json),
            "transcript": lambda: scan_transcript(transcript),
            "git": lambda: get_git_status(cwd),
        }

        merged = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            futs = {ex.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futs):
                tag = futs[fut]
                try:
                    res = fut.result()
                except Exception:
                    continue
                if tag == "transcript":
                    # Namespace transcript fields under tx_ for cache clarity
                    for k, v in res.items():
                        merged[f"tx_{k}"] = v
                elif tag == "git":
                    for k, v in res.items():
                        merged[f"git_{k}"] = v
                else:
                    merged.update(res)

        output = dict(prev)
        output.update(merged)
        output["timestamp"] = time.time()
        output["transcript_mtime"] = transcript_mtime(transcript)
        output["git_index_mtime"] = git_index_mtime(cwd) if cwd else None
        write_local_cache_atomic(output)
    except Exception:
        pass
    finally:
        release_lock(REFRESH_LOCK)


def refresh_local_cache_async(input_json):
    """Spawn the detached refresh worker. Returns True if the spawn started."""
    try:
        p = subprocess.Popen(
            [sys.executable or "python3", os.path.abspath(__file__), "--refresh"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if p.stdin:
            p.stdin.write(json.dumps(input_json).encode())
            p.stdin.close()
        return True
    except Exception:
        return False


# ---------- render ----------
SEP = f" {DIM}·{RESET} "


def _fmt_ctx_field(ctx_tokens, ctx_pct):
    if ctx_tokens is not None and ctx_pct is not None:
        body = f"{fmt_tokens(ctx_tokens)} ({int(round(ctx_pct))}%)"
    elif ctx_pct is not None:
        body = f"{int(round(ctx_pct))}%"
    else:
        return "🧠 -"
    if ctx_pct is not None and ctx_pct >= CTX_WARN_PCT:
        return f"{RED}⚠ 🧠 {body}{RESET}"
    return f"🧠 {body}"


def _fmt_git_field(cache):
    branch = cache.get("git_branch")
    if not branch:
        return None
    added = cache.get("git_added", 0) or 0
    removed = cache.get("git_removed", 0) or 0
    ahead = cache.get("git_ahead", 0) or 0
    behind = cache.get("git_behind", 0) or 0
    parts = [f"⎇ {branch}"]
    if added or removed:
        parts.append(f"+{added} -{removed}")
    if ahead:
        parts.append(f"↑{ahead}")
    if behind:
        parts.append(f"↓{behind}")
    return " ".join(parts)


def _fmt_burn_field(burn, wk_pct, plan_week_cost):
    if burn is None:
        return "🔥 $-/h"
    burn_str = f"🔥 ${burn:.2f}/h"
    # Burn-rate projection: how many hours until weekly quota is full
    if (
        wk_pct is not None and burn and burn > 0
        and plan_week_cost is not None and plan_week_cost > 0
        and 0 < wk_pct < 100
    ):
        plan_total = plan_week_cost / (wk_pct / 100)
        remaining_budget = plan_total - plan_week_cost
        hrs_to_full = remaining_budget / burn
        # Only show projection when it's meaningful (< 7 days)
        if 0 < hrs_to_full < 7 * 24:
            tag = fmt_remaining(int(hrs_to_full * 3600))
            if hrs_to_full < BURN_PROJECTION_WARN_HRS:
                burn_str += f" {RED}→wk {tag}{RESET}"
            else:
                burn_str += f" {DIM}→wk {tag}{RESET}"
    return burn_str


def render(data, cache, plan):
    model = data.get("model", {}).get("display_name", "Claude")
    effort = get_effort()

    # Transcript-derived (from cache)
    last_slash = cache.get("tx_last_slash", "-")
    slash_field = f"/{last_slash}" if last_slash != "-" else "-"
    agents_running = cache.get("tx_agents_running", 0) or 0
    agent_details = cache.get("tx_agent_details", []) or []
    tool_count = cache.get("tx_tool_count", 0) or 0
    cache_hit = cache.get("tx_cache_hit_pct")
    todos_pending = cache.get("tx_todos_pending", 0) or 0
    todos_total = cache.get("tx_todos_total", 0) or 0
    bg_count = cache.get("tx_bg_count", 0) or 0
    session_start = cache.get("tx_session_start_unix")
    last_latency = cache.get("tx_last_latency_secs")

    # Live-computed
    session_dur = fmt_duration_short(time.time() - session_start) if session_start else "-"
    latency_str = fmt_duration_short(last_latency) if last_latency else "-"

    # Cost / context (from cache)
    ctx_tokens = cache.get("ctx_tokens")
    ctx_pct = cache.get("ctx_pct")
    block_cost = cache.get("block_cost")
    burn = cache.get("burn")
    plan_week_cost = cache.get("plan_week_cost")
    monthly_cost = cache.get("monthly_cost")

    block_str = f"${block_cost:.2f}" if block_cost is not None else "$-"
    wk_str = f"${plan_week_cost:.2f}" if plan_week_cost is not None else "$-"
    mo_str = f"${monthly_cost:.2f}" if monthly_cost is not None else "$-"

    # --- Line 1: identity + state ---
    l1_fields = [
        f"🤖 {model}",
        f"⚡ {effort}",
    ]
    git_field = _fmt_git_field(cache)
    if git_field:
        l1_fields.append(git_field)
    l1_fields.append(_fmt_ctx_field(ctx_tokens, ctx_pct))
    if cache_hit is not None:
        l1_fields.append(f"📊 {int(round(cache_hit))}% cache")
    if session_start:
        l1_fields.append(f"⏱ {session_dur}")
    if last_latency:
        l1_fields.append(f"⌛ {latency_str}")
    line1 = SEP.join(l1_fields)

    # --- Line 2: cost + activity ---
    l2_fields = [
        f"💰 {block_str} / {wk_str} / {mo_str}",
        _fmt_burn_field(burn, plan.get("wk_pct"), plan_week_cost),
    ]
    if todos_total > 0:
        l2_fields.append(f"✓ {todos_pending}/{todos_total} todo")
    if tool_count > 0:
        l2_fields.append(f"🔧 {tool_count}")
    if bg_count > 0:
        l2_fields.append(f"🌐 {bg_count} bg")
    l2_fields.append(f"📝 {slash_field}")
    line2 = SEP.join(l2_fields)

    # --- Optional Line 3: agent tree ---
    line3 = None
    if agents_running > 0 and agent_details:
        nodes = []
        for i, a in enumerate(agent_details):
            char = TREE_LAST if i == len(agent_details) - 1 else TREE_BRANCH
            dur = fmt_duration_short(time.time() - a.get("start_unix", time.time()))
            nodes.append(f"{char} {a.get('name', 'agent')} {DIM}{dur}{RESET}")
        line3 = f"👥 {agents_running}  " + "  ".join(nodes)

    # --- Bars ---
    five_pct = plan["five_pct"] if plan["five_pct"] is not None else 0.0
    wk_pct = plan["wk_pct"] if plan["wk_pct"] is not None else 0.0
    five_remain = seconds_until(plan["five_reset"])
    wk_remain = seconds_until(plan["wk_reset"])
    bars = [
        f"{GREEN}5h{RESET}  {render_bar(five_pct)}  {color_pct(five_pct)}  {DIM}({fmt_remaining(five_remain)}){RESET}",
        f"{GREEN}7d{RESET}  {render_bar(wk_pct)}  {color_pct(wk_pct)}  {DIM}({fmt_remaining(wk_remain)}){RESET}",
    ]

    lines = [line1, line2]
    if line3:
        lines.append(line3)
    lines.extend(bars)
    return "\n".join(lines)


def needs_refresh(data, cache, plan):
    # Time-based
    cache_ts = cache.get("timestamp")
    if cache_ts is None or (time.time() - cache_ts) > CACHE_STALE_SECS:
        return True
    # mtime-based: transcript / git index changed since last refresh
    transcript = data.get("transcript_path", "")
    cwd = extract_cwd(data)
    if transcript_mtime(transcript) != cache.get("transcript_mtime"):
        return True
    if cwd and git_index_mtime(cwd) != cache.get("git_index_mtime"):
        return True
    return False


def main():
    if "--version" in sys.argv[1:]:
        # Handle before reading stdin so it never blocks when run interactively.
        print(f"claude-code-statusline {__version__}")
        return 0

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    if "--refresh" in sys.argv[1:]:
        refresh_local_cache(data)
        return 0

    cache = read_local_cache()
    plan = get_plan_usage()
    try:
        sys.stdout.write(render(data, cache, plan) + "\n")
    except Exception:
        # A statusline must never emit a traceback; degrade to a minimal line.
        try:
            model = (data.get("model") or {}).get("display_name", "Claude")
        except Exception:
            model = "Claude"
        sys.stdout.write(f"🤖 {model}\n")

    # Fire-and-forget refreshes, each gated by a single-flight lock so an active
    # session never piles up redundant workers.
    try:
        if needs_refresh(data, cache, plan) and try_acquire_lock(REFRESH_LOCK, REFRESH_LOCK_TTL):
            if not refresh_local_cache_async(data):
                release_lock(REFRESH_LOCK)
        if (OMC_HUD.exists()
                and (plan["cache_age"] is None or plan["cache_age"] > CACHE_STALE_SECS)
                and try_acquire_lock(USAGE_LOCK, USAGE_LOCK_TTL)):
            # The node HUD worker can't release our lock; it expires via TTL.
            refresh_usage_cache_async(data)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A custom statusline for Claude Code: a single Python script (`statusline.py`) that Claude Code runs on every render, feeding it a JSON blob on stdin. It prints 5–7 lines of truecolor (24-bit ANSI) status — model, effort, git, PR, context, prompt cache, cost, burn rate, sub-agents, a gradient divider, and 5h/7d plus per-model (e.g. Fable) plan-quota bars.

## Commands

```bash
# Tests (stdlib unittest — no pytest, no deps)
python3 -m unittest discover -s tests -v
python3 tests/test_statusline.py            # run the file directly

# Byte-compile check (what CI runs first)
python3 -m py_compile statusline.py

# Lint the installer (CI runs both)
bash -n install.sh
shellcheck install.sh

# Manual smoke test — feed it a statusline input blob
echo '{"model":{"display_name":"Claude Opus 4.7"},"transcript_path":"","cwd":"."}' | python3 statusline.py

# Version (must not block on stdin)
python3 statusline.py --version

# Install / update (copies to ~/.claude/, patches settings.json)
./install.sh
```

There is no build step and no package manifest — `statusline.py` is the deliverable, copied verbatim into `~/.claude/`.

## Hard constraints (do not break these)

- **Zero third-party dependencies.** `statusline.py` and the test suite use the Python stdlib only. No `requirements.txt`, no `pyproject.toml`. Do not add imports outside the stdlib.
- **Python 3.9+.** CI runs the matrix 3.9–3.13. `from __future__ import annotations` lets signatures use newer typing syntax, but anything evaluated at runtime must work on 3.9.
- **The render path must never emit a traceback.** `main()` wraps `render()` in a try/except that degrades to a one-line `🤖 <model>` fallback. A statusline that crashes breaks the user's terminal UI. Keep every render-path field defensive (`or 0`, `.get(...)`, broad `except Exception: pass`).
- **macOS / Linux only.** Uses POSIX process APIs (`start_new_session`, `os.open` exclusive-create locks) and a Bash installer. Windows is explicitly unsupported.
- Bump `__version__` in `statusline.py` when shipping a user-visible change; `--version` reads it.

## Architecture — the render/refresh split

This is the core design and the thing to understand before changing anything. There are **two execution modes in one file**, selected by argv:

1. **Render mode** (default, runs on every Claude Code redraw, ~20 ms): reads three JSON files — the local cache, the OMC plan-usage cache, and `settings.json` — and prints. It does **no** subprocess calls, no transcript scan, no git. All it does beyond reading caches is *decide whether a refresh is due* and, if so, fire-and-forget a detached worker. The render must stay cheap; expensive imports (`concurrent.futures`, `tempfile`, `timedelta`) are deliberately kept out of the top-level import block and imported lazily inside the refresh path only.

2. **Refresh mode** (`--refresh`, detached subprocess, 5–10 s): the script re-invokes *itself* via `refresh_local_cache_async`. The worker runs 3 `ccusage` calls (cost/burn only) + a transcript scan + `git status` **in parallel** (`ThreadPoolExecutor`), merges them, and atomically writes the local cache. The render path picks up the new values on the next redraw. Context is *not* refreshed here — it comes live from the statusline input's `context_window` field on every render (see `read_context_window`), so it's per-session, matches `/context`, and is correct on the 1M beta. Effort likewise prefers the live `effort.level` from stdin.

So the slow work never blocks the visible statusline — it always shows the last cached values and refreshes behind the scenes.

### Caches (both in `~/.claude/`, or `$CLAUDE_CONFIG_DIR`)

| File | Owner | Contents |
|------|-------|----------|
| `.statusline-cache.json` | this script's refresh worker | Account-wide ccusage cost/burn at the top level; per-session transcript-derived + git fields and refresh mtimes under `sessions[<session_key>]` (see below) |
| `plugins/oh-my-claudecode/.usage-cache-anthropic.json` | OMC HUD (external `node` script) | 5h / 7d / **per-model weekly** plan-quota % + resets. `render()` prefers the live stdin `rate_limits` for the 5h/7d *display* (real-time), but this cache is the **only** source of per-model weekly sub-limits — stdin exposes `five_hour`/`seven_day`/`spend_limit` only. OMC 5.x writes them as `data.scopedWeeklyBuckets` (`[{id, label, percent, resetsAt, isActive}]`, built from the usage API's `limits[]` `kind: "weekly_scoped"` rows — this is where **Fable** lives) and only fills the legacy `sonnetWeeklyPercent` / `opusWeeklyPercent` when the API reports those families. Also the 5h/7d fallback. So the HUD is still spawned (staleness-gated) whenever it's installed. Refreshed by spawning `hud/omc-hud.mjs`. Optional — without it the per-model bars are hidden and 5h/7d fall back to 0% when stdin lacks them. |

The local cache is per-session: `{<ACCOUNT_KEYS>…, "sessions": {<session_key>: {tx_*, git_*, timestamp, transcript_mtime, git_index_mtime}}}`. ccusage fields are account-wide and top-level; transcript-scan fields (`tx_*`) and git fields (`git_*`) belong to the session that refreshed them (`session_key` = stdin `session_id`, else `transcript_path`). `main()` flattens the current session's slice with `session_view()` before calling `render()` / `needs_refresh()`, which read the flat keys directly — so renaming a producer key means updating the reader too, and a **new account-wide field must be added to `ACCOUNT_KEYS`** or the next refresh drops it (the output is rebuilt, not merged, so stale legacy keys don't linger). Session entries idle > `SESSION_CACHE_TTL` (24 h) are pruned, max `SESSION_CACHE_MAX` (30). The refresh lock is still global, so only one worker ever writes the file.

Several fields are read **live from the statusline stdin** rather than any cache — they're per-session, real-time, and can't bleed across sessions: `context_window` → `read_context_window` (ctx tokens/%, /context-accurate, 1M-correct), `rate_limits` → `read_rate_limits` (5h/7d bars), `effort.level` → `get_effort`, `prompt_cache` → `read_prompt_cache` (📊 hit rate — the transcript estimate is only a fallback — plus the ❄ go-cold wall-clock time and miss count), `pr` → `_fmt_pr_field`, and `cost`/`version`/`thinking`/`output_style`/`exceeds_200k_tokens`/`fast_mode` read directly in `render()`. Times that matter while idle (cache expiry) are shown as wall-clock times, not countdowns: the statusline only re-renders on events unless the user sets `statusLine.refreshInterval`. Prefer stdin over ccusage/OMC/settings whenever Claude Code provides the number.

### Single-flight refresh locking

An actively streaming session changes the transcript mtime constantly, which would otherwise spawn a refresh subprocess on every redraw. `try_acquire_lock` / `release_lock` (`REFRESH_LOCK`, `USAGE_LOCK`) gate worker spawns with on-disk exclusive-create lock files. A lock older than its TTL is treated as abandoned (worker died) and reclaimed. `REFRESH_LOCK_TTL` (45 s) must stay **above** the slowest ccusage timeout (30 s) so a healthy worker is never pre-empted. The refresh worker always `release_lock`s in a `finally`.

### `needs_refresh` triggers

Any of: cache older than `CACHE_STALE_SECS` (60 s); transcript file mtime changed; `.git/index` mtime changed — all compared against the **current session's own** cache entry, so another session's refresh never makes this one look stale. mtime comparison is what makes refresh incremental.

### Other notable pieces

- **`scan_transcript`** — a single forward pass over the transcript JSONL deriving *all* transcript metrics at once (slash command, running agents, tool count, cache-hit %, todos, bg tasks, session start, latency). It cheaply rejects lines before `json.loads`. Add new transcript-derived fields here, not in a second pass. Gotchas: only `user`/`assistant`/`system` entries are scanned — current builds also log `api-request-*` debug records that replay earlier messages and would double-count; the sub-agent tool is `Agent` (formerly `Task`, both accepted); background agents return an `async_launched` ack as their tool_result and finish only when a `<task-notification>` carrying their `<tool-use-id>` arrives — those blocks can sit in a user string, a queued-command attachment or a queue-operation record, so they're harvested from the raw line (`_collect_notifications`).
- **Color model** — bar fill is colored per-character by *position* on a green→amber→pink gradient (`rgb_at`); the percentage text is colored by *value* (red ≥ 100%). See `render_bar` / `color_pct`.

## Testing conventions

- Framework is **stdlib `unittest`**, not pytest. Mirror the existing style in `tests/test_statusline.py`.
- The suite sets `CLAUDE_CONFIG_DIR` to a scratch tempdir **before importing the module** — all cache/lock paths resolve from it at import time, so the real `~/.claude` is never touched. Preserve that ordering.
- `ccusage` subprocess calls are tested by monkeypatching `sl.subprocess.run` with a `FakeRun` stub. No real subprocess or network in tests.

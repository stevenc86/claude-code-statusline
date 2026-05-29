# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A custom statusline for Claude Code: a single Python script (`statusline.py`) that Claude Code runs on every render, feeding it a JSON blob on stdin. It prints 4–5 lines of truecolor (24-bit ANSI) status — model, effort, git, context, cost, burn rate, sub-agents, and 5h/7d plan-quota bars.

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

2. **Refresh mode** (`--refresh`, detached subprocess, 5–10 s): the script re-invokes *itself* via `refresh_local_cache_async`. The worker runs 4 `ccusage` calls + a transcript scan + `git status` **in parallel** (`ThreadPoolExecutor`), merges them, and atomically writes the local cache. The render path picks up the new values on the next redraw.

So the slow work never blocks the visible statusline — it always shows the last cached values and refreshes behind the scenes.

### Caches (both in `~/.claude/`, or `$CLAUDE_CONFIG_DIR`)

| File | Owner | Contents |
|------|-------|----------|
| `.statusline-cache.json` | this script's refresh worker | cost/burn/context/git/transcript-derived fields |
| `plugins/oh-my-claudecode/.usage-cache-anthropic.json` | OMC HUD (external `node` script) | 5h / 7d plan-quota %. Refreshed by spawning `hud/omc-hud.mjs`. Optional — bars show 0% without it. |

In the cache, transcript-scan fields are namespaced `tx_*` and git fields `git_*` (see `refresh_local_cache`); other ccusage fields are top-level. `render()` reads these keys directly, so renaming a producer key means updating the reader too.

### Single-flight refresh locking

An actively streaming session changes the transcript mtime constantly, which would otherwise spawn a refresh subprocess on every redraw. `try_acquire_lock` / `release_lock` (`REFRESH_LOCK`, `USAGE_LOCK`) gate worker spawns with on-disk exclusive-create lock files. A lock older than its TTL is treated as abandoned (worker died) and reclaimed. `REFRESH_LOCK_TTL` (45 s) must stay **above** the slowest ccusage timeout (30 s) so a healthy worker is never pre-empted. The refresh worker always `release_lock`s in a `finally`.

### `needs_refresh` triggers

Any of: cache older than `CACHE_STALE_SECS` (60 s); transcript file mtime changed; `.git/index` mtime changed. mtime comparison is what makes refresh incremental.

### Other notable pieces

- **`scan_transcript`** — a single forward pass over the transcript JSONL deriving *all* transcript metrics at once (slash command, running agents, tool count, cache-hit %, todos, bg tasks, session start, latency). It cheaply rejects lines before `json.loads`. Add new transcript-derived fields here, not in a second pass.
- **Color model** — bar fill is colored per-character by *position* on a green→amber→pink gradient (`rgb_at`); the percentage text is colored by *value* (red ≥ 100%). See `render_bar` / `color_pct`.

## Testing conventions

- Framework is **stdlib `unittest`**, not pytest. Mirror the existing style in `tests/test_statusline.py`.
- The suite sets `CLAUDE_CONFIG_DIR` to a scratch tempdir **before importing the module** — all cache/lock paths resolve from it at import time, so the real `~/.claude` is never touched. Preserve that ordering.
- `ccusage` subprocess calls are tested by monkeypatching `sl.subprocess.run` with a `FakeRun` stub. No real subprocess or network in tests.

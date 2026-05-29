# Claude Code Statusline

A custom statusline for [Claude Code](https://claude.ai/code) with truecolor gradient bars and a macaron-inspired pastel palette.

```
🤖 Claude Opus 4.7 · ⚡ xhigh · ⎇ master +42 -10 · 🧠 290k (29%) · 📊 89% cache · ⏱ 21h17m · ⌛ 18s
💰 $38.47 / $322.41 / $1641.98 · 🔥 $33.26/h →wk 18h · ✓ 4/13 todo · 🔧 149 · 🌐 2 bg · 📝 /admin-login
👥 3 · ├ architect 2m · ├ explore 45s · └ executor 1m       ← only when agents are running
5h  ████████                        26%  (3h42m)
7d  █                                4%  (4d08h)
```

> Bars are rendered with per-character truecolor RGB (24-bit ANSI). Each `#` is colored by its position on a green → amber → pink gradient.

## Features

**Always shown (Line 1 — identity + state):**
- 🤖 model · ⚡ effort · ⎇ git branch & diff lines · 🧠 context tokens (with ⚠ at ≥ 80%) · 📊 cache hit rate · ⏱ session duration · ⌛ last response latency

**Always shown (Line 2 — cost + activity):**
- 💰 block / plan-week / month cost · 🔥 burn rate with `→wk` projection · ✓ todo progress · 🔧 tool call count · 🌐 background tasks · 📝 last slash command

**Conditional (Line 3 — sub-agents):**
- 👥 agent count + tree of running sub-agents with elapsed time

**Plan bars (Line 4–5):**
- Real Anthropic `/api/oauth/usage` quota for 5h and 7d windows
- Per-character truecolor gradient (pistachio → mango → raspberry)

**Performance:**
- ~20 ms render. All expensive work (4 ccusage calls + transcript scan + git status) runs in parallel in a detached, single-flight refresh worker; the main path never blocks.
- Incremental refresh: re-scans transcript / git only when their mtime changes.

**Other:**
- Pure Python — no Node.js runtime, no compiled binary
- Threshold warnings: red color for context ≥ 80%, burn projection < 24 h, bar value ≥ 90 %

## Field Reference

### Line 1 — identity + state

| Icon | Field | Meaning | Source | Hidden when |
|------|-------|---------|--------|------------|
| 🤖 | model | Current Claude model display name | `model.display_name` from statusline input | never |
| ⚡ | effort | Reasoning effort level (`low` / `medium` / `high` / `xhigh`) | `effortLevel` in `~/.claude/settings.json` | never (`-` if unset) |
| ⎇ | branch | Current git branch, uncommitted line diff, ahead/behind counts | `git rev-parse`, `git rev-list`, `git diff HEAD --numstat` | not in a git repo |
| 🧠 | context | Tokens used in current conversation + percent of model window | `ccusage statusline` parsing | ccusage unavailable |
| ⚠ 🧠 | (warning) | Context icon turns red and adds ⚠ prefix | when context ≥ 80% | context < 80% |
| 📊 | cache hit | Cache-read tokens ÷ total input tokens (session-wide) | aggregated `message.usage` in transcript | no usage data yet |
| ⏱ | session | Wall-clock time since the first message in this transcript | transcript first `timestamp` | no session start found |
| ⌛ | latency | Time between the last user message and last assistant response | transcript timestamps | no completed turn yet |

### Line 2 — cost + activity

| Icon | Field | Meaning | Source | Hidden when |
|------|-------|---------|--------|------------|
| 💰 | cost | `$block / $plan-week / $month` — three time windows | `ccusage blocks/daily/monthly` | always shown (`$-` if missing) |
| 🔥 | burn | Cost-per-hour for current 5h block | `ccusage blocks --active` `burnRate.costPerHour` | always shown |
| →wk | projection | Hours until current burn rate fills the weekly plan quota | computed from burn + plan-week % | projection > 7 days or wk_pct ≤ 0 |
| →wk (red) | (warning) | Projection turns red when imminent | when projection < 24 h | projection ≥ 24 h |
| ✓ | todos | `pending/total` — based on `TaskCreate` / `TaskUpdate` tool calls | transcript tool_use scan | no `TaskCreate` in transcript |
| 🔧 | tools | Total `tool_use` calls in this session | transcript tool_use count | count = 0 |
| 🌐 | bg | Background-mode Bash calls started recently and not yet completed | Bash tool_uses with `run_in_background=true` minus task-completion markers | count = 0 |
| 📝 | last slash | The most recently invoked slash command | `<command-name>` / `<command-message>` tags in user-role text | always shown (`-` if none) |

### Line 3 — sub-agents (conditional)

| Icon | Field | Meaning |
|------|-------|---------|
| 👥 | count | Number of `Task` tool_use entries with no matching tool_result yet |
| ├ / └ | tree | Tree branches for each running agent — last entry uses └ |
| agent name | name | `subagent_type` from the Task tool input |
| duration | elapsed | Time since that agent's `Task` tool_use started |

Entire line is omitted when no agents are running.

### Line 4–5 — plan bars

| Icon | Field | Meaning | Source |
|------|-------|---------|--------|
| 5h | bar | Current 5-hour plan quota usage with reset countdown | Anthropic `/api/oauth/usage` cache via OMC HUD |
| 7d | bar | Current 7-day plan quota usage with reset countdown | same |
| (bold %) | (warning) | Percentage rendered in bold when ≥ 90% | applied at render time |

### Color palette (per-character bar gradient)

| Position in bar | Color | Hex |
|-----------------|-------|-----|
| 0% | pistachio | `#98D8AA` |
| 50% | mango | `#FFD9A2` |
| 100% | raspberry | `#FF9EB2` |

Each `#` in the bar is colored by its **position**, not by the overall fill value. The displayed percentage text uses the **value** itself for color (green at low, red at high, solid red at ≥ 100%).

## Performance

| State | Time | Notes |
|-------|------|-------|
| Warm cache (typical) | **~20 ms** | reads two JSON caches + `settings.json`, renders, fires-and-forgets refresh if stale |
| Cold start (first ever run) | ~20 ms | same render cost; cost / git / transcript-derived fields show `-` until the background refresh writes the cache |
| Background refresh | 5–10 s | 4 ccusage calls + transcript scan + git status run in parallel via `ThreadPoolExecutor`; never blocks render |

Refresh triggers (any of):
- Cache age > 60 s
- Transcript file mtime changed
- `.git/index` mtime changed

Refreshes are **single-flight**: spawning a refresh worker is gated by an on-disk lock (`~/.claude/.statusline-refresh.lock`), so an actively streaming session—where the transcript mtime changes constantly—never piles up redundant refresh subprocesses. At most one refresh runs at a time; an abandoned lock is reclaimed after 45 s.

Caches:
- `~/.claude/.statusline-cache.json` — cost / burn / context / git / transcript-derived fields (this script maintains)
- `~/.claude/plugins/oh-my-claudecode/.usage-cache-anthropic.json` — 5h / 7d plan quota (OMC HUD maintains, 60 s TTL)

## Requirements

**Platforms:** macOS and Linux. Windows is not supported — the script uses POSIX process APIs and a Bash installer.

| Dependency | Required for | How to install |
|------------|--------------|----------------|
| Python 3.9+ | Everything | Usually pre-installed |
| [ccusage](https://github.com/ryoppippi/ccusage) | Cost tracking, context % | `npm i -g ccusage` (the installer offers this) |
| [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) plugin (OMC HUD) | 5h / 7d real plan usage bar | `claude plugin install oh-my-claudecode@omc` (the installer offers this, opt-in) |

You don't have to install these by hand — `./install.sh` offers to set up `ccusage`
automatically and can optionally install the `oh-my-claudecode` plugin for you.

Without OMC HUD the 5h / 7d bars will display 0% until the usage cache exists. Cost tracking still works because that comes from ccusage.

## Install

```bash
git clone https://github.com/<your-username>/claude-code-statusline.git ~/code/claude-code-statusline
cd ~/code/claude-code-statusline
./install.sh
```

The installer:

1. Verifies Python 3
2. Offers to install `ccusage` if missing (and Node.js via Homebrew on macOS, if needed)
3. Optionally installs the `oh-my-claudecode` plugin for the 5h/7d bars — opt-in, default no (uses the `claude` CLI)
4. Copies `statusline.py` to `~/.claude/` (backing up the old copy only if it changed)
5. Patches `~/.claude/settings.json`'s `statusLine` field (backing it up only if it changed)

The installer never installs anything without asking, and skips every prompt when run non-interactively (e.g. piped).

Restart Claude Code (or open a new session) and you're done.

## Update

The statusline is a single file copied into `~/.claude/`, so updating is just
"pull the latest, re-run the installer":

```bash
cd ~/code/claude-code-statusline   # wherever you cloned it
git pull
./install.sh
```

`install.sh` is safe to re-run: it only overwrites `~/.claude/statusline.py`
when the file actually changed, backing up the old copy to a timestamped `.bak`
first, and leaves `settings.json` untouched if the `statusLine` field is already
correct. Restart Claude Code (or open a new session) to pick up the change.

Check which version is installed:

```bash
python3 ~/.claude/statusline.py --version
```

Any backups it does create (`~/.claude/statusline.py.bak.*`) are safe to delete
once the new version works.

## Customize

Open `~/.claude/statusline.py`. Common knobs near the top:

```python
BAR_WIDTH = 30                       # bar width in characters
FILL_CHAR = "#"                      # bar fill character

# Vivid Macaron palette (default)
GREEN_RGB = (152, 216, 170)          # 0% — pistachio
AMBER_RGB = (255, 217, 162)          # 50% — mango
RED_RGB   = (255, 158, 178)          # 100% — raspberry
```

### Alternative palettes

| Style | Green | Amber | Red |
|-------|-------|-------|-----|
| **Vivid Macaron** (default) | `(152, 216, 170)` | `(255, 217, 162)` | `(255, 158, 178)` |
| Classic Macaron | `(164, 224, 169)` | `(255, 234, 167)` | `(255, 173, 173)` |
| Soft Pastel | `(181, 234, 215)` | `(255, 218, 193)` | `(255, 209, 220)` |
| Tailwind 400 | `(74, 222, 128)` | `(251, 191, 36)` | `(248, 113, 113)` |
| Tailwind 500 (saturated) | `(34, 197, 94)` | `(245, 158, 11)` | `(239, 68, 68)` |

## How the bars work

- **5h bar**: Anthropic plan's current 5-hour rate-limit quota (e.g. Pro/Max session limit). Reset time also from Anthropic API.
- **7d bar**: Anthropic plan's current weekly rate-limit quota. Reset is plan-specific (commonly Monday morning UTC).
- **Bar fill %**: how much of your plan quota you've consumed
- **Bar color (per character)**: position in the bar — green at the start, red at the end, regardless of fill
- **Percentage text color**: based on the percentage value — green at low, red at high (and overflow)

## Cost windows

| Field | Window | Source |
|-------|--------|--------|
| `$X / · · ·` (1st) | Current 5h ccusage billing block | `ccusage blocks --active --json` |
| `· · · / $X / · · ·` (2nd) | Plan-week (aligned with 7d bar reset) | `ccusage daily --since <plan_week_start>` |
| `· · · · · · / $X` (3rd) | Current calendar month | `ccusage monthly --json` |

Costs are token-estimated by ccusage at public Anthropic API prices, regardless of your actual subscription.

## Restore

```bash
ls ~/.claude/settings.json.bak.*    # find latest backup
cp ~/.claude/settings.json.bak.<timestamp> ~/.claude/settings.json
rm -f ~/.claude/.statusline-cache.json   # safe to remove anytime
```

## Known Limitations

- Plan-week cost slightly overestimates: ccusage groups by full calendar day, so the partial day at the week boundary is included in full
- Slash command detection scans transcript regex matches — only counts commands recorded with `<command-name>` or `<command-message>` tags
- Agent count uses Task tool_use start/end counting from transcript — may be off if tool_use_result entries are missing or out of order

## License

MIT — see [LICENSE](LICENSE)

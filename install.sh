#!/usr/bin/env bash
# Claude Code Statusline installer
# Run from cloned repo dir: ./install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
TARGET="$CLAUDE_DIR/statusline.py"
SETTINGS="$CLAUDE_DIR/settings.json"

color_blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }
color_green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
color_yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
color_red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }

color_blue "▸ Claude Code Statusline installer"
echo

# 1. Check Python 3
if ! command -v python3 >/dev/null 2>&1; then
  color_red "✗ python3 not found — please install Python 3.9+"
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
color_green "✓ Python $PY_VER"

# 2. Check / install ccusage (an npm package; powers cost + context %)
if command -v ccusage >/dev/null 2>&1; then
  color_green "✓ ccusage $(ccusage --version 2>/dev/null | head -1 || echo installed)"
else
  color_yellow "⚠ ccusage not installed (powers cost + context %)"
  # ccusage needs Node/npm — offer to install Node via Homebrew on macOS.
  if ! command -v npm >/dev/null 2>&1; then
    if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1 && [ -t 0 ]; then
      read -r -p "Node.js not found. Install it via Homebrew? [y/N] " node_ans || node_ans="n"
      if [[ "${node_ans:-n}" =~ ^[Yy]$ ]]; then
        brew install node || color_yellow "  → Homebrew install failed; install Node.js manually"
      fi
    fi
  fi
  if command -v npm >/dev/null 2>&1; then
    if [ -t 0 ]; then
      read -r -p "Install ccusage via npm? [Y/n] " ans || ans="n"
    else
      ans="n"  # non-interactive (e.g. piped): never auto-install global packages
    fi
    if [[ -z "${ans:-}" || "${ans:-}" =~ ^[Yy]$ ]]; then
      if npm install -g ccusage; then
        color_green "✓ ccusage installed"
      else
        color_yellow "  → ccusage install failed; run 'npm i -g ccusage' manually"
      fi
    else
      color_yellow "  → run 'npm i -g ccusage' to enable cost / ctx fields"
    fi
  else
    color_red "✗ Node.js/npm not found — install Node.js, then run: npm i -g ccusage"
    color_yellow "  → continuing without ccusage; cost / ctx fields will show '-'"
  fi
fi

# 3. OMC HUD (optional — only powers the 5h/7d plan-usage bars)
HUD="$CLAUDE_DIR/hud/omc-hud.mjs"
if [[ -f "$HUD" ]]; then
  color_green "✓ OMC HUD detected — 5h/7d bars will show real plan usage"
elif command -v claude >/dev/null 2>&1; then
  color_yellow "⚠ OMC HUD not installed (optional — powers only the 5h/7d plan bars)"
  color_yellow "  it ships inside the larger oh-my-claudecode plugin"
  if [ -t 0 ]; then
    read -r -p "Install the oh-my-claudecode plugin now? [y/N] " hud_ans || hud_ans="n"
  else
    hud_ans="n"  # non-interactive: don't pull in a large plugin unprompted
  fi
  if [[ "${hud_ans:-n}" =~ ^[Yy]$ ]]; then
    claude plugin marketplace add "https://github.com/Yeachan-Heo/oh-my-claudecode.git" 2>/dev/null || true
    if claude plugin install oh-my-claudecode@omc; then
      color_green "✓ oh-my-claudecode installed — restart Claude Code to activate the HUD"
    else
      color_yellow "  → install failed; in Claude Code run: /plugin install oh-my-claudecode@omc"
    fi
  else
    color_yellow "  → skipped; 5h/7d bars will show 0% until it's installed"
  fi
else
  color_yellow "⚠ OMC HUD not installed — 5h/7d bars will show 0% until cache exists"
  color_yellow "  install it in Claude Code: /plugin install oh-my-claudecode@omc"
fi

# 4. Install statusline.py — back up the old copy only when it actually changes
mkdir -p "$CLAUDE_DIR"
if [[ -f "$TARGET" ]] && cmp -s "$SCRIPT_DIR/statusline.py" "$TARGET"; then
  color_green "✓ statusline.py already up to date"
else
  if [[ -f "$TARGET" ]]; then
    cp "$TARGET" "$TARGET.bak.$(date +%Y%m%d_%H%M%S)"
    color_blue "  backed up existing statusline.py"
  fi
  cp "$SCRIPT_DIR/statusline.py" "$TARGET"
  chmod +x "$TARGET"
  color_green "✓ installed $TARGET"
fi

# 5. Patch settings.json's statusLine field — back up only when it changes
python3 - "$SETTINGS" <<'PYEOF'
import json, os, shutil, sys, time
path = sys.argv[1]
desired = {"type": "command", "command": "python3 $HOME/.claude/statusline.py"}
data = {}
if os.path.exists(path):
    with open(path) as f:
        try:
            data = json.load(f)
        except Exception:
            print("  settings.json invalid JSON, creating new")
if data.get("statusLine") == desired:
    print("✓ settings.json statusLine already set")
else:
    if os.path.exists(path):
        shutil.copy(path, f"{path}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
        print("  backed up existing settings.json")
    data["statusLine"] = desired
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print("✓ patched settings.json statusLine")
PYEOF

echo
color_green "Done. Restart Claude Code (or open a new session) to see the new statusline."
echo
color_blue "Customize colors in $TARGET (search for GREEN_RGB / AMBER_RGB / RED_RGB)."

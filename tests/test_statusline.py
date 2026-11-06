"""Unit tests for statusline.py.

Zero third-party dependencies — runs on a stock Python 3.9+ interpreter:

    python3 -m unittest discover -s tests          # from the repo root
    python3 tests/test_statusline.py               # directly

Every test runs against a throwaway CLAUDE_CONFIG_DIR so the suite never reads
or writes the developer's real ~/.claude state.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

# Point the module at a scratch config dir *before* importing it: CLAUDE_DIR and
# all cache/lock paths are resolved at import time from CLAUDE_CONFIG_DIR.
_SCRATCH = tempfile.mkdtemp(prefix="sl-tests.")
os.environ["CLAUDE_CONFIG_DIR"] = _SCRATCH

_MODULE_PATH = Path(__file__).resolve().parent.parent / "statusline.py"
_spec = importlib.util.spec_from_file_location("statusline", _MODULE_PATH)
sl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl)


def tearDownModule():
    """Remove the throwaway config dir once the whole suite has finished."""
    shutil.rmtree(_SCRATCH, ignore_errors=True)


def _strip_ansi(s):
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


class FakeRun:
    """Stand-in for subprocess.run's CompletedProcess (only .stdout is read)."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class FormattingTests(unittest.TestCase):
    def test_fmt_remaining(self):
        self.assertEqual(sl.fmt_remaining(0), "0m")
        self.assertEqual(sl.fmt_remaining(59), "0m")
        self.assertEqual(sl.fmt_remaining(60), "1m")
        self.assertEqual(sl.fmt_remaining(3600), "1h00m")
        self.assertEqual(sl.fmt_remaining(3600 + 5 * 60), "1h05m")
        self.assertEqual(sl.fmt_remaining(25 * 3600), "1d01h")
        self.assertEqual(sl.fmt_remaining(-100), "0m")  # clamps negatives

    def test_fmt_duration_short(self):
        self.assertEqual(sl.fmt_duration_short(45), "45s")
        self.assertEqual(sl.fmt_duration_short(60), "1m")
        self.assertEqual(sl.fmt_duration_short(92 * 60), "1h32m")

    def test_fmt_tokens(self):
        self.assertEqual(sl.fmt_tokens(999), "999")
        self.assertEqual(sl.fmt_tokens(1000), "1k")
        self.assertEqual(sl.fmt_tokens(290_000), "290k")
        self.assertEqual(sl.fmt_tokens(1_500_000), "1.5M")

    def test_rgb_at_boundaries(self):
        self.assertEqual(sl.rgb_at(0), sl.GREEN_RGB)
        self.assertEqual(sl.rgb_at(50), sl.AMBER_RGB)
        self.assertEqual(sl.rgb_at(100), sl.RED_RGB)
        self.assertEqual(sl.rgb_at(200), sl.RED_RGB)  # clamps above 100
        self.assertEqual(sl.rgb_at(-10), sl.GREEN_RGB)  # clamps below 0

    def test_render_bar_fill_proportional(self):
        # Count visible fill characters, ignoring ANSI escapes / spaces.
        def fill_count(pct):
            return sl.render_bar(pct, width=10).count(sl.FILL_CHAR)

        self.assertEqual(fill_count(0), 0)
        self.assertEqual(fill_count(100), 10)
        self.assertEqual(fill_count(50), 5)
        self.assertEqual(fill_count(200), 10)  # never overflows width


class TimeParsingTests(unittest.TestCase):
    def test_parse_iso_handles_z(self):
        dt = sl.parse_iso("2026-05-29T00:00:00Z")
        self.assertEqual(dt.year, 2026)
        self.assertIsNotNone(dt.tzinfo)

    def test_to_epoch(self):
        self.assertIsNone(sl.to_epoch(None))
        self.assertIsNone(sl.to_epoch(""))
        self.assertIsNone(sl.to_epoch("not-a-date"))
        self.assertIsInstance(sl.to_epoch("2026-05-29T00:00:00Z"), float)

    def test_seconds_until(self):
        self.assertEqual(sl.seconds_until(None), 0)
        self.assertEqual(sl.seconds_until("garbage"), 0)
        self.assertEqual(sl.seconds_until("2000-01-01T00:00:00Z"), 0)  # past clamps to 0

    def test_seconds_until_accepts_epoch_seconds(self):
        # stdin rate_limits.resets_at is a Unix epoch in seconds.
        self.assertEqual(sl.seconds_until(0), 0)            # epoch past clamps to 0
        future = time.time() + 3600
        self.assertGreater(sl.seconds_until(future), 3500)


class ExtractCwdTests(unittest.TestCase):
    def test_workspace_none_does_not_crash(self):
        self.assertEqual(sl.extract_cwd({"workspace": None}), "")

    def test_workspace_dict(self):
        self.assertEqual(sl.extract_cwd({"workspace": {"current_dir": "/x"}}), "/x")

    def test_cwd_takes_precedence(self):
        self.assertEqual(sl.extract_cwd({"cwd": "/a", "workspace": {"current_dir": "/b"}}), "/a")

    def test_empty(self):
        self.assertEqual(sl.extract_cwd({}), "")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.lock = Path(_SCRATCH) / "test.lock"
        sl.release_lock(self.lock)

    def tearDown(self):
        sl.release_lock(self.lock)

    def test_acquire_then_backoff(self):
        self.assertTrue(sl.try_acquire_lock(self.lock, 45))
        self.assertFalse(sl.try_acquire_lock(self.lock, 45))  # held within TTL

    def test_stale_lock_reclaimed(self):
        self.assertTrue(sl.try_acquire_lock(self.lock, 45))
        os.utime(self.lock, (time.time() - 100, time.time() - 100))  # age it past TTL
        self.assertTrue(sl.try_acquire_lock(self.lock, 45))  # reclaimed
        self.assertFalse(sl.try_acquire_lock(self.lock, 45))  # fresh again

    def test_release_allows_reacquire(self):
        self.assertTrue(sl.try_acquire_lock(self.lock, 45))
        sl.release_lock(self.lock)
        self.assertFalse(self.lock.exists())
        self.assertTrue(sl.try_acquire_lock(self.lock, 45))


class ScanTranscriptTests(unittest.TestCase):
    def _write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl", dir=_SCRATCH)
        with os.fdopen(fd, "w") as f:
            for obj in lines:
                f.write(json.dumps(obj) + "\n")
        return path

    def test_empty_path_returns_defaults(self):
        out = sl.scan_transcript("")
        self.assertEqual(out["tool_count"], 0)
        self.assertEqual(out["agents_running"], 0)
        self.assertEqual(out["last_slash"], "-")

    def test_full_scan(self):
        # Recent timestamps: an unfinished agent older than AGENT_LOOKBACK_SECS is
        # (correctly) treated as dead, so a fixed historical date would drop it.
        path = self._write([
            {"type": "user", "timestamp": self._ts(65),
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "<command-name>admin-login</command-name>"}]}},
            {"type": "assistant", "timestamp": self._ts(60),
             "message": {"role": "assistant",
                         "usage": {"input_tokens": 100, "cache_read_input_tokens": 900},
                         "content": [
                             {"type": "tool_use", "id": "t1", "name": "Task",
                              "input": {"subagent_type": "explore"}},
                             {"type": "tool_use", "id": "c1", "name": "TaskCreate", "input": {}},
                         ]}},
        ])
        out = sl.scan_transcript(path)
        self.assertEqual(out["last_slash"], "admin-login")
        self.assertEqual(out["agents_running"], 1)         # Task with no result yet
        self.assertEqual(out["agent_details"][0]["name"], "explore")
        self.assertEqual(out["tool_count"], 2)
        self.assertEqual(out["cache_hit_pct"], 90.0)        # 900 / (900+100)
        self.assertEqual(out["todos_total"], 1)
        self.assertEqual(out["todos_pending"], 1)

    def test_agent_completion_clears_running(self):
        path = self._write([
            {"type": "assistant", "timestamp": "2026-05-29T00:00:00Z",
             "message": {"role": "assistant",
                         "content": [{"type": "tool_use", "id": "t1", "name": "Task",
                                      "input": {"subagent_type": "explore"}}]}},
            {"type": "user", "timestamp": "2026-05-29T00:00:10Z",
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}},
        ])
        out = sl.scan_transcript(path)
        self.assertEqual(out["agents_running"], 0)

    # --- current builds: sub-agent tool is "Agent", often launched async ---

    @staticmethod
    def _ts(ago_secs=0):
        t = time.time() - ago_secs
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

    def _agent_use(self, tid="toolu_A1", ago=60, **inp):
        return {"type": "assistant", "timestamp": self._ts(ago),
                "message": {"role": "assistant",
                            "content": [{"type": "tool_use", "id": tid, "name": "Agent",
                                         "input": {"description": "Review the diff",
                                                   "subagent_type": "code-reviewer", **inp}}]}}

    def _async_ack(self, tid="toolu_A1"):
        return {"type": "user", "timestamp": self._ts(55),
                "message": {"role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": tid,
                                         "content": [{"type": "text",
                                                      "text": "Async agent launched successfully."}]}]},
                "toolUseResult": {"isAsync": True, "status": "async_launched",
                                  "agentId": "a1b2c3"}}

    @staticmethod
    def _notification(tid="toolu_A1", task_id="a1b2c3", status="completed"):
        return (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tid}</tool-use-id>\n<status>{status}</status>\n"
                f"<summary>done</summary>\n</task-notification>")

    def test_agent_tool_counts_as_running_agent(self):
        path = self._write([self._agent_use()])
        out = sl.scan_transcript(path)
        self.assertEqual(out["agents_running"], 1)
        self.assertEqual(out["agent_details"][0]["name"], "code-reviewer")

    def test_agent_name_preferred_over_subagent_type(self):
        path = self._write([self._agent_use(name="selfreview")])
        self.assertEqual(sl.scan_transcript(path)["agent_details"][0]["name"], "selfreview")

    def test_async_agent_stays_running_after_launch_ack(self):
        # The tool_result of a background launch is only an ack, not completion.
        path = self._write([self._agent_use(), self._async_ack()])
        self.assertEqual(sl.scan_transcript(path)["agents_running"], 1)

    def test_async_agent_cleared_by_user_string_notification(self):
        path = self._write([
            self._agent_use(), self._async_ack(),
            {"type": "user", "timestamp": self._ts(5),
             "message": {"role": "user", "content": self._notification()}},
        ])
        self.assertEqual(sl.scan_transcript(path)["agents_running"], 0)

    def test_async_agent_cleared_by_queued_attachment_notification(self):
        # Delivered mid-turn, the notification lands in an attachment record.
        path = self._write([
            self._agent_use(), self._async_ack(),
            {"type": "attachment", "timestamp": self._ts(5),
             "attachment": {"type": "queued_command", "prompt": self._notification()}},
        ])
        self.assertEqual(sl.scan_transcript(path)["agents_running"], 0)

    def test_sync_agent_result_clears_running(self):
        path = self._write([
            self._agent_use(),
            {"type": "user", "timestamp": self._ts(30),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "tool_use_id": "toolu_A1",
                                      "content": "review done"}]}},
        ])
        self.assertEqual(sl.scan_transcript(path)["agents_running"], 0)

    def test_ancient_unfinished_agent_is_dropped(self):
        ago = sl.AGENT_LOOKBACK_SECS + 600
        path = self._write([self._agent_use(ago=ago)])
        self.assertEqual(sl.scan_transcript(path)["agents_running"], 0)

    def test_debug_request_records_are_not_double_counted(self):
        # `api-request-blob` replays earlier messages, tool_use blocks included.
        use = self._agent_use()
        blob = {"type": "api-request-blob", "timestamp": self._ts(1),
                "message": {"role": "user", "content": use["message"]["content"]}}
        path = self._write([use, blob])
        out = sl.scan_transcript(path)
        self.assertEqual(out["tool_count"], 1)

    def _bg_bash(self, tid="toolu_B1"):
        return {"type": "assistant", "timestamp": self._ts(60),
                "message": {"role": "assistant",
                            "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                         "input": {"command": "sleep 99",
                                                   "run_in_background": True}}]}}

    def test_bg_bash_running_until_notified(self):
        path = self._write([self._bg_bash()])
        self.assertEqual(sl.scan_transcript(path)["bg_count"], 1)

    def test_bg_bash_cleared_by_tool_use_id_notification(self):
        path = self._write([
            self._bg_bash(),
            {"type": "user", "timestamp": self._ts(5),
             "message": {"role": "user",
                         "content": self._notification(tid="toolu_B1", task_id="b9")}},
        ])
        self.assertEqual(sl.scan_transcript(path)["bg_count"], 0)

    def test_agent_notification_does_not_clear_bg_bash(self):
        # A completed *agent* must not be subtracted from the running bg Bash count.
        path = self._write([
            self._bg_bash(), self._agent_use(), self._async_ack(),
            {"type": "user", "timestamp": self._ts(5),
             "message": {"role": "user", "content": self._notification()}},
        ])
        out = sl.scan_transcript(path)
        self.assertEqual(out["bg_count"], 1)
        self.assertEqual(out["agents_running"], 0)


class CcusageParsingTests(unittest.TestCase):
    def setUp(self):
        self._orig_run = sl.subprocess.run

    def tearDown(self):
        sl.subprocess.run = self._orig_run

    def test_monthly_exact_match(self):
        from datetime import datetime, timezone
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        sl.subprocess.run = lambda *a, **k: FakeRun(
            json.dumps({"monthly": [{"period": period, "totalCost": 42.5}]}))
        self.assertEqual(sl._ccusage_monthly()["monthly_cost"], 42.5)

    def test_monthly_falls_back_to_latest(self):
        sl.subprocess.run = lambda *a, **k: FakeRun(
            json.dumps({"monthly": [{"period": "2099-01", "totalCost": 5},
                                    {"period": "2099-02", "totalCost": 9}]}))
        self.assertEqual(sl._ccusage_monthly()["monthly_cost"], 9.0)

    def test_monthly_empty(self):
        sl.subprocess.run = lambda *a, **k: FakeRun(json.dumps({"monthly": []}))
        self.assertEqual(sl._ccusage_monthly()["monthly_cost"], 0.0)

    def test_plan_week_sums_daily(self):
        sl.subprocess.run = lambda *a, **k: FakeRun(
            json.dumps({"daily": [{"totalCost": 1.5}, {"totalCost": 2.5}]}))
        out = sl._ccusage_plan_week("2026-05-26T00:00:00Z")
        self.assertEqual(out["plan_week_cost"], 4.0)
        self.assertEqual(out["plan_week_reset"], "2026-05-26T00:00:00Z")

    def test_plan_week_no_reset(self):
        out = sl._ccusage_plan_week(None)
        self.assertEqual(out["plan_week_cost"], 0.0)
        self.assertIsNone(out["plan_week_reset"])


class RenderTests(unittest.TestCase):
    def _plan(self):
        return {"five_pct": 26.0, "five_reset": None, "wk_pct": 4.0,
                "wk_reset": None, "cache_age": 1}

    def test_minimal_render_is_string_with_bars(self):
        out = sl.render({"model": {"display_name": "Claude Opus 4.7"}}, {}, self._plan())
        self.assertIsInstance(out, str)
        self.assertIn("🤖 Claude Opus 4.7", out)
        self.assertIn("5h", out)
        self.assertIn("7d", out)

    def test_rich_render_includes_optional_fields(self):
        cache = {
            "tx_last_slash": "admin-login", "tx_agents_running": 2,
            "tx_agent_details": [{"name": "architect", "start_unix": time.time() - 120},
                                 {"name": "explore", "start_unix": time.time() - 45}],
            "tx_tool_count": 149, "tx_cache_hit_pct": 89.0,
            "tx_todos_pending": 4, "tx_todos_total": 13, "tx_bg_count": 2,
            "ctx_tokens": 290000, "ctx_pct": 29.0, "block_cost": 38.47, "burn": 33.26,
            "plan_week_cost": 322.41, "monthly_cost": 1641.98,
            "git_branch": "master", "git_added": 42, "git_removed": 10,
            "git_ahead": 1, "git_behind": 0,
        }
        out = sl.render({"model": {"display_name": "X"}}, cache, self._plan())
        self.assertIn("👥 2", out)
        self.assertIn("architect", out)
        self.assertIn("🔧 149", out)
        self.assertIn("🌐 2 bg", out)
        self.assertIn("✓ 4/13 todo", out)
        self.assertIn("⎇ master +42 -10 ↑1", out)

    def test_agent_line_omitted_when_idle(self):
        out = sl.render({"model": {"display_name": "X"}}, {}, self._plan())
        self.assertNotIn("👥", out)

    def test_live_context_window_preferred_over_cache(self):
        # Live `context_window` from stdin wins over the (possibly cross-session)
        # cached ccusage value — and stays correct on the 1M beta.
        data = {
            "model": {"display_name": "X"},
            "context_window": {
                "total_input_tokens": 82215, "total_output_tokens": 743,
                "context_window_size": 1000000, "used_percentage": 8,
                "remaining_percentage": 92,
            },
        }
        cache = {"ctx_tokens": 109596, "ctx_pct": 42.0}  # stale / other session
        out = sl.render(data, cache, self._plan())
        self.assertIn("🧠 82k (8%)", out)
        self.assertNotIn("42%", out)

    def test_live_session_fields_and_flags_render(self):
        data = {
            "model": {"display_name": "Claude Opus 4.8"},
            "version": "2.1.158",
            "effort": {"level": "high"},
            "thinking": {"enabled": True},
            "output_style": {"name": "Explanatory"},
            "cost": {"total_cost_usd": 2.86, "total_lines_added": 42,
                     "total_lines_removed": 7},
        }
        out = sl.render(data, {}, self._plan())
        self.assertIn("v2.1.158", out)     # version after model
        self.assertIn("⚡ high", out)
        self.assertIn("💭", out)            # thinking indicator
        self.assertIn("💬 $2.86", out)      # session cost
        self.assertIn("✎ +42 -7", out)     # session edits
        self.assertIn("🎨 Explanatory", out)

    def test_default_output_style_hidden(self):
        data = {"model": {"display_name": "X"}, "output_style": {"name": "default"}}
        self.assertNotIn("🎨", sl.render(data, {}, self._plan()))

    def test_live_rate_limits_drive_bars(self):
        # plan (from OMC cache) is empty; stdin rate_limits should fill the bars.
        plan = {"five_pct": 26.0, "wk_pct": 4.0, "five_reset": None,
                "wk_reset": None, "cache_age": 1}
        out = sl.render({"model": {"display_name": "X"}}, {}, plan)
        self.assertIn("5h", out)
        self.assertIn("7d", out)

    def test_sonnet_bar_shown_when_present(self):
        plan = {**self._plan(), "sonnet_pct": 10.0, "sonnet_reset": None}
        out = sl.render({"model": {"display_name": "X"}}, {}, plan)
        self.assertIn("Son", out)
        self.assertEqual(out.count("\n") + 1, 6)  # 2 status lines + divider + 3 bars

    def test_sonnet_bar_omitted_when_absent(self):
        out = sl.render({"model": {"display_name": "X"}}, {}, self._plan())
        self.assertNotIn("Son", out)
        self.assertEqual(out.count("\n") + 1, 5)  # 2 status lines + divider + 2 bars

    def test_scoped_fable_bar_shown_and_aligned(self):
        plan = {**self._plan(),
                "scoped": [{"label": "Fable", "pct": 49.0, "reset": None}]}
        out = sl.render({"model": {"display_name": "X"}}, {}, plan)
        rows = [_strip_ansi(r) for r in out.split("\n")]
        bar_rows = [r for r in rows if r.startswith(("5h", "7d", "Fable"))]
        self.assertEqual(len(bar_rows), 3)
        self.assertIn("49%", bar_rows[-1])
        # Labels are padded to the longest one, so every bar track (first fill
        # char) starts at the same column: label width + 2-space gap.
        self.assertEqual({r.index(sl.FILL_CHAR) for r in bar_rows}, {len("Fable") + 2})
        self.assertEqual(out.count("\n") + 1, 6)  # 2 status lines + divider + 3 bars

    def test_opus_bar_shown_when_present(self):
        plan = {**self._plan(), "opus_pct": 12.0, "opus_reset": None}
        self.assertIn("Opus", sl.render({"model": {"display_name": "X"}}, {}, plan))

    def test_divider_separates_text_from_bars(self):
        out = sl.render({"model": {"display_name": "X"}}, {}, self._plan())
        rows = out.split("\n")
        divider_idx = next(i for i, r in enumerate(rows) if "─" in r)
        first_bar_idx = next(i for i, r in enumerate(rows) if "5h" in r)
        # The rule sits between the status text and the first bar.
        self.assertLess(divider_idx, first_bar_idx)
        self.assertNotIn("5h", rows[divider_idx])
        self.assertIn("◆", rows[divider_idx])  # centered ornament

    def test_no_context_window_shows_placeholder_not_stale_cache(self):
        # Context is live-only now. A stale cache value must NOT leak in (that was
        # the cross-session bleed bug); absent context_window renders "🧠 -".
        cache = {"ctx_tokens": 290000, "ctx_pct": 29.0}
        out = sl.render({"model": {"display_name": "X"}}, cache, self._plan())
        self.assertIn("🧠 -", out)
        self.assertNotIn("290k", out)
        self.assertNotIn("29%", out)


class LiveStdinFieldTests(unittest.TestCase):
    """prompt_cache / pr / fast_mode, read live from the statusline stdin."""

    _PLAN = {"five_pct": 1.0, "five_reset": None, "wk_pct": 1.0, "wk_reset": None,
             "cache_age": 1}

    def _render(self, cache=None, **data):
        rows = sl.render({"model": {"display_name": "X"}, **data}, cache or {}, self._PLAN)
        return _strip_ansi(rows.split("\n")[0])  # Line 1

    @staticmethod
    def _pc(**over):
        pc = {"warm": True, "caching_observed": True, "ttl": "1h",
              "expires_at": int(time.time()) + 1800, "requests": 12, "misses": 0,
              "hit_ratio": 0.982}
        pc.update(over)
        return pc

    def test_read_prompt_cache_absent_or_malformed(self):
        self.assertIsNone(sl.read_prompt_cache({}))
        self.assertIsNone(sl.read_prompt_cache({"prompt_cache": "nope"}))
        pc = sl.read_prompt_cache({"prompt_cache": {"hit_ratio": "x", "expires_at": True,
                                                    "misses": "?"}})
        self.assertEqual(pc, {"hit_pct": None, "warm": False, "expires_at": None,
                              "observed": False, "misses": 0})

    def test_live_hit_ratio_overrides_transcript_estimate(self):
        line = self._render(cache={"tx_cache_hit_pct": 50.0}, prompt_cache=self._pc())
        self.assertIn("📊 98% cache", line)
        self.assertNotIn("50%", line)

    def test_falls_back_to_transcript_estimate(self):
        self.assertIn("📊 50% cache", self._render(cache={"tx_cache_hit_pct": 50.0}))

    def test_warm_cache_shows_wall_clock_expiry(self):
        pc = self._pc()
        line = self._render(prompt_cache=pc)
        self.assertIn("❄" + sl.fmt_clock(pc["expires_at"]), line)
        self.assertNotIn("cold", line)

    def test_cold_cache_and_misses(self):
        line = self._render(prompt_cache=self._pc(warm=False, expires_at=None, misses=2))
        self.assertIn("❄ cold", line)
        self.assertIn("✗2", line)

    def test_no_cache_field_without_any_hit_rate(self):
        line = self._render(prompt_cache=self._pc(hit_ratio=None, caching_observed=False))
        self.assertNotIn("📊", line)

    def test_pr_badge(self):
        line = self._render(pr={"number": 42, "url": "https://x/pull/42",
                                "review_state": "approved"})
        self.assertIn("🔀 #42 ✓", line)

    def test_gitlab_mr_badge_and_draft(self):
        line = self._render(pr={"number": 7, "kind": "mr", "review_state": "draft"})
        self.assertIn("🔀 !7 draft", line)

    def test_pr_absent_or_malformed(self):
        self.assertNotIn("🔀", self._render())
        self.assertNotIn("🔀", self._render(pr={"number": None}))
        self.assertNotIn("🔀", self._render(pr="nope"))

    def test_fast_mode_flag(self):
        self.assertIn("🚀 fast", self._render(fast_mode=True))
        self.assertNotIn("🚀", self._render(fast_mode=False))
        self.assertNotIn("🚀", self._render())


class ReadContextWindowTests(unittest.TestCase):
    def test_missing_field_returns_none(self):
        self.assertEqual(sl.read_context_window({}), (None, None, None))

    def test_full_object_parsed(self):
        cw = {"context_window": {"total_input_tokens": 82215,
                                 "used_percentage": 8, "context_window_size": 1000000}}
        self.assertEqual(sl.read_context_window(cw), (82215, 8.0, 1000000))

    def test_null_usage_after_compact_is_safe(self):
        # current_usage/totals are null right after /compact or before first call.
        cw = {"context_window": {"total_input_tokens": None,
                                 "used_percentage": None, "context_window_size": 200000}}
        self.assertEqual(sl.read_context_window(cw), (None, None, 200000))

    def test_non_dict_does_not_crash(self):
        self.assertEqual(sl.read_context_window({"context_window": "nope"}), (None, None, None))


class EffortTests(unittest.TestCase):
    def test_stdin_effort_level_preferred(self):
        self.assertEqual(sl.get_effort({"effort": {"level": "xhigh"}}), "xhigh")

    def test_falls_back_to_settings_when_absent(self):
        # No `effort` on stdin -> read settings.json (scratch dir has none -> "-").
        self.assertEqual(sl.get_effort({}), "-")
        self.assertEqual(sl.get_effort(None), "-")

    def test_malformed_effort_does_not_crash(self):
        self.assertEqual(sl.get_effort({"effort": "nope"}), "-")


class RateLimitsTests(unittest.TestCase):
    def test_reads_both_windows(self):
        data = {"rate_limits": {
            "five_hour": {"used_percentage": 26.0, "resets_at": 1780000000},
            "seven_day": {"used_percentage": 4.0, "resets_at": 1780500000},
        }}
        rl = sl.read_rate_limits(data)
        self.assertEqual(rl["five_pct"], 26.0)
        self.assertEqual(rl["five_reset"], 1780000000)
        self.assertEqual(rl["wk_pct"], 4.0)
        self.assertEqual(rl["wk_reset"], 1780500000)

    def test_absent_returns_none(self):
        self.assertIsNone(sl.read_rate_limits({}))
        self.assertIsNone(sl.read_rate_limits({"rate_limits": "nope"}))
        self.assertIsNone(sl.read_rate_limits({"rate_limits": {}}))

    def test_partial_window_ok(self):
        rl = sl.read_rate_limits({"rate_limits": {"five_hour": {"used_percentage": 50}}})
        self.assertEqual(rl["five_pct"], 50.0)
        self.assertIsNone(rl["wk_pct"])


class PlanUsageTests(unittest.TestCase):
    def setUp(self):
        sl.USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            sl.USAGE_CACHE.unlink()
        except FileNotFoundError:
            pass

    def test_reads_sonnet_weekly_from_omc_cache(self):
        sl.USAGE_CACHE.write_text(json.dumps({
            "timestamp": int(time.time() * 1000),
            "data": {"fiveHourPercent": 7, "weeklyPercent": 59,
                     "sonnetWeeklyPercent": 10,
                     "sonnetWeeklyResetsAt": "2026-06-02T10:00:00Z"},
        }))
        info = sl.get_plan_usage()
        self.assertEqual(info["wk_pct"], 59)
        self.assertEqual(info["sonnet_pct"], 10)
        self.assertEqual(info["sonnet_reset"], "2026-06-02T10:00:00Z")

    def test_missing_sonnet_is_none(self):
        sl.USAGE_CACHE.write_text(json.dumps({"data": {"weeklyPercent": 59}}))
        self.assertIsNone(sl.get_plan_usage()["sonnet_pct"])
        self.assertEqual(sl.get_plan_usage()["scoped"], [])

    def test_reads_scoped_fable_bucket_from_omc_cache(self):
        # Shape written by OMC 5.7.x from the usage API's limits[] weekly_scoped rows.
        sl.USAGE_CACHE.write_text(json.dumps({
            "timestamp": int(time.time() * 1000),
            "data": {"fiveHourPercent": 14, "weeklyPercent": 32,
                     "scopedWeeklyBuckets": [
                         {"id": "fable", "label": "Fable", "percent": 49,
                          "resetsAt": "2026-11-10T10:00:00.446Z", "isActive": True}]},
        }))
        info = sl.get_plan_usage()
        self.assertEqual(info["scoped"], [
            {"label": "Fable", "pct": 49.0, "reset": "2026-11-10T10:00:00.446Z"}])
        self.assertIsNone(info["sonnet_pct"])

    def test_malformed_scoped_buckets_are_skipped(self):
        sl.USAGE_CACHE.write_text(json.dumps({"data": {"scopedWeeklyBuckets": [
            "nope", {"label": "NoPct"}, {"label": "", "percent": 5},
            {"id": "x", "label": "Extremely Long Name", "percent": "7"},
        ]}}))
        self.assertEqual(sl.get_plan_usage()["scoped"], [
            {"label": "Extrem", "pct": 7.0, "reset": None}])

    def test_scoped_buckets_not_a_list_is_safe(self):
        sl.USAGE_CACHE.write_text(json.dumps({"data": {"scopedWeeklyBuckets": {"a": 1}}}))
        self.assertEqual(sl.get_plan_usage()["scoped"], [])


class PerSessionCacheTests(unittest.TestCase):
    """Local cache: account-wide ccusage fields shared, tx_*/git_* per session."""

    def setUp(self):
        self._orig = {n: getattr(sl, n) for n in
                      ("_ccusage_blocks", "_ccusage_monthly", "_ccusage_plan_week")}
        sl._ccusage_blocks = lambda: {"block_cost": 3.0, "burn": 1.5}
        sl._ccusage_monthly = lambda: {"monthly_cost": 30.0}
        sl._ccusage_plan_week = lambda reset: {"plan_week_cost": 9.0, "plan_week_reset": reset}

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(sl, n, fn)
        for p in (sl.LOCAL_CACHE, sl.REFRESH_LOCK):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def _transcript(self, n_tools):
        fd, path = tempfile.mkstemp(suffix=".jsonl", dir=_SCRATCH)
        content = [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}
                   for i in range(n_tools)]
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-11-06T00:00:00Z",
                                "message": {"role": "assistant", "content": content}}) + "\n")
        return path

    def test_session_key_fallbacks(self):
        self.assertEqual(sl.session_key({"session_id": "abc", "transcript_path": "/t"}), "abc")
        self.assertEqual(sl.session_key({"transcript_path": "/t"}), "/t")
        self.assertEqual(sl.session_key({}), "_default")
        self.assertEqual(sl.session_key(None), "_default")

    def test_view_merges_account_fields_with_own_session_only(self):
        cache = {"block_cost": 3.0, "monthly_cost": 30.0,
                 "sessions": {"A": {"tx_tool_count": 5}, "B": {"tx_tool_count": 77}}}
        view = sl.session_view(cache, {"session_id": "A"})
        self.assertEqual(view, {"block_cost": 3.0, "monthly_cost": 30.0, "tx_tool_count": 5})

    def test_legacy_flat_cache_leaks_no_transcript_fields(self):
        legacy = {"block_cost": 3.0, "tx_tool_count": 985, "ctx_pct": 8.0,
                  "timestamp": time.time(), "transcript_mtime": 1.0}
        view = sl.session_view(legacy, {"session_id": "A"})
        self.assertEqual(view, {"block_cost": 3.0})
        self.assertTrue(sl.needs_refresh({"session_id": "A"}, view, {}))

    def test_other_sessions_refresh_does_not_make_this_one_stale(self):
        now = time.time()
        cache = {"sessions": {
            "A": {"timestamp": now, "transcript_mtime": None, "git_index_mtime": None},
            "B": {"timestamp": now - 3600}}}
        a = {"session_id": "A", "transcript_path": ""}
        self.assertFalse(sl.needs_refresh(a, sl.session_view(cache, a), {}))
        self.assertTrue(sl.needs_refresh({"session_id": "C"},
                                         sl.session_view(cache, {"session_id": "C"}), {}))

    def test_refresh_writes_own_session_and_keeps_others(self):
        now = time.time()
        sl.write_local_cache_atomic({
            "block_cost": 1.0, "tx_tool_count": 985, "ctx_pct": 8.0,   # legacy flat keys
            "sessions": {
                "A": {"tx_tool_count": 5, "timestamp": now - 10},
                "stale": {"tx_tool_count": 1, "timestamp": now - sl.SESSION_CACHE_TTL - 60},
            }})
        sl.try_acquire_lock(sl.REFRESH_LOCK, sl.REFRESH_LOCK_TTL)
        sl.refresh_local_cache({"session_id": "B", "transcript_path": self._transcript(2),
                                "cwd": ""})
        out = sl.read_local_cache()
        self.assertEqual(set(out["sessions"]), {"A", "B"})        # stale pruned
        self.assertEqual(out["sessions"]["A"]["tx_tool_count"], 5)  # untouched
        self.assertEqual(out["sessions"]["B"]["tx_tool_count"], 2)
        self.assertEqual(out["block_cost"], 3.0)                    # account-wide, refreshed
        self.assertEqual(out["monthly_cost"], 30.0)
        self.assertNotIn("tx_tool_count", out)                      # legacy keys dropped
        self.assertNotIn("ctx_pct", out)
        self.assertFalse(sl.REFRESH_LOCK.exists())                  # lock released

    def test_failed_task_keeps_previous_session_values(self):
        now = time.time()
        sl.write_local_cache_atomic({"sessions": {"A": {"tx_tool_count": 5,
                                                        "timestamp": now - 10}}})
        orig_scan = sl.scan_transcript
        sl.scan_transcript = lambda path: 1 / 0
        try:
            sl.refresh_local_cache({"session_id": "A", "transcript_path": "", "cwd": ""})
        finally:
            sl.scan_transcript = orig_scan
        entry = sl.read_local_cache()["sessions"]["A"]
        self.assertEqual(entry["tx_tool_count"], 5)
        self.assertGreater(entry["timestamp"], now - 10)

    def test_prune_caps_session_count(self):
        now = time.time()
        sessions = {f"s{i}": {"timestamp": now - i} for i in range(sl.SESSION_CACHE_MAX + 5)}
        sessions["junk"] = "not a dict"
        kept = sl._prune_sessions(sessions, now)
        self.assertEqual(len(kept), sl.SESSION_CACHE_MAX)
        self.assertIn("s0", kept)                                   # newest kept
        self.assertNotIn(f"s{sl.SESSION_CACHE_MAX + 4}", kept)      # oldest dropped


class NeedsRefreshTests(unittest.TestCase):
    def test_no_cache_timestamp_forces_refresh(self):
        self.assertTrue(sl.needs_refresh({}, {}, {}))

    def test_fresh_cache_no_change(self):
        cache = {"timestamp": time.time(), "transcript_mtime": None, "git_index_mtime": None}
        self.assertFalse(sl.needs_refresh({"transcript_path": ""}, cache, {}))

    def test_stale_timestamp(self):
        cache = {"timestamp": time.time() - 3600}
        self.assertTrue(sl.needs_refresh({"transcript_path": ""}, cache, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)

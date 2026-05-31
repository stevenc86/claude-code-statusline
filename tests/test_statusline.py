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
        path = self._write([
            {"type": "user", "timestamp": "2026-05-29T00:00:00Z",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "<command-name>admin-login</command-name>"}]}},
            {"type": "assistant", "timestamp": "2026-05-29T00:00:05Z",
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
        self.assertEqual(out.count("\n") + 1, 5)  # 2 status lines + 3 bars

    def test_sonnet_bar_omitted_when_absent(self):
        out = sl.render({"model": {"display_name": "X"}}, {}, self._plan())
        self.assertNotIn("Son", out)
        self.assertEqual(out.count("\n") + 1, 4)  # 2 status lines + 2 bars

    def test_no_context_window_shows_placeholder_not_stale_cache(self):
        # Context is live-only now. A stale cache value must NOT leak in (that was
        # the cross-session bleed bug); absent context_window renders "🧠 -".
        cache = {"ctx_tokens": 290000, "ctx_pct": 29.0}
        out = sl.render({"model": {"display_name": "X"}}, cache, self._plan())
        self.assertIn("🧠 -", out)
        self.assertNotIn("290k", out)
        self.assertNotIn("29%", out)


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

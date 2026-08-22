import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import herdr_model_lanes as quota

NOW = 1_700_000_000


def window(used: int, remaining: int, reset_delta: int | None) -> quota.QuotaWindow:
    resets_at = None if reset_delta is None else NOW + reset_delta
    return quota.QuotaWindow(used, remaining, resets_at)


CLAUDE_OK = quota.ClaudeUsage(window(9, 91, 5 * 86_400), None, None, NOW)
GROK_OK = quota.GrokUsage(window(70, 30, 3 * 86_400), NOW)
GLM_OK = quota.GlmUsage(window(50, 50, 2 * 3_600), NOW)
ANTIGRAVITY_OK = quota.AntigravityUsage(window(10, 90, 6 * 86_400), NOW)
KIMI_OK = quota.KimiUsage(window(20, 80, 4 * 86_400), NOW)
CURSOR_OK = quota.CursorUsage(window(40, 60, 12 * 86_400), NOW)


def fake_refresh(usage, error=None, stale=False, calls=None):
    def _run(force, cache_path, now, command):
        if calls is not None:
            calls.append(force)
        return usage, stale, error

    return _run


class UsageCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        # Every provider reader is stubbed: no network, Keychain, or live quota.
        self.calls: dict[str, list[bool]] = {}
        defaults = {
            "claude": CLAUDE_OK,
            "grok": GROK_OK,
            "glm": GLM_OK,
            "antigravity": ANTIGRAVITY_OK,
            "kimi": KIMI_OK,
            "cursor": CURSOR_OK,
        }
        self.refresh_mocks = {}
        for name, usage in defaults.items():
            self.calls[name] = []
            patcher = mock.patch.object(
                quota,
                f"_refresh_{name}",
                fake_refresh(usage, calls=self.calls[name]),
            )
            self.refresh_mocks[name] = patcher
        self.configured = {
            "grok": mock.patch.object(quota, "_grok_login_exists", return_value=True),
            "glm": mock.patch.object(
                quota.glm_usage, "key_available", return_value=True
            ),
            "antigravity": mock.patch.object(
                quota.antigravity_usage, "probe_available", return_value=True
            ),
            "kimi": mock.patch.object(
                quota.kimi_usage, "key_available", return_value=True
            ),
            "cursor": mock.patch.object(
                quota.cursor_usage, "key_available", return_value=True
            ),
        }
        for patcher in (*self.refresh_mocks.values(), *self.configured.values()):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stub_refresh(self, name, usage, error=None, stale=False) -> None:
        """Stack a replacement reader; cleanups unwind in reverse order."""
        patcher = mock.patch.object(
            quota,
            f"_refresh_{name}",
            fake_refresh(usage, error=error, stale=stale, calls=self.calls[name]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.refresh_mocks[name] = patcher

    def run_usage(self, argv):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = quota.ag_command(["usage", *argv], now=NOW)
        return code, stdout.getvalue()

    def test_plain_output_prints_one_row_per_provider(self) -> None:
        for name, usage in (
            ("claude", CLAUDE_OK),
            ("grok", GROK_OK),
            ("glm", GLM_OK),
            ("antigravity", ANTIGRAVITY_OK),
            ("kimi", KIMI_OK),
            ("cursor", CURSOR_OK),
        ):
            self.stub_refresh(name, usage)

        code, out = self.run_usage([])

        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line]
        self.assertEqual(len(lines), 6)
        self.assertIn("Cl 91% · 5d0h", out)
        self.assertIn("Gk 30% · 3d0h", out)
        self.assertIn("Gl 50% · 2h", out)
        self.assertIn("Ag 90% · 6d0h", out)
        self.assertIn("Km 80% · 4d0h", out)
        self.assertIn("Cu 60% · 12d0h", out)

    def test_json_output_is_stable_and_normalized(self) -> None:
        for name, usage in (
            ("claude", CLAUDE_OK),
            ("grok", GROK_OK),
            ("glm", GLM_OK),
            ("antigravity", ANTIGRAVITY_OK),
            ("kimi", KIMI_OK),
            ("cursor", CURSOR_OK),
        ):
            self.stub_refresh(name, usage)

        code, out = self.run_usage(["--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(
            set(payload["providers"]),
            {"claude", "grok", "glm", "antigravity", "kimi", "cursor"},
        )
        claude = payload["providers"]["claude"]
        self.assertEqual(claude["remaining_percent"], 91)
        self.assertEqual(claude["resets_at"], NOW + 5 * 86_400)
        self.assertFalse(claude["stale"])
        self.assertIsNone(claude["error"])
        self.assertNotIn("token", out.lower())

    def test_refresh_flag_bypasses_reader_caches(self) -> None:
        self.stub_refresh("grok", GROK_OK)

        self.run_usage(["--refresh"])
        self.assertEqual(self.calls["grok"], [True])

        self.run_usage([])
        self.assertEqual(self.calls["grok"], [True, False])

    def test_partial_failure_is_visible_and_exit_zero(self) -> None:
        self.stub_refresh("grok", None, error="Grok helper failed with rc=1")
        self.stub_refresh("claude", CLAUDE_OK)

        code, out = self.run_usage([])

        self.assertEqual(code, 0)
        self.assertIn("Gk n/a (Grok helper failed with rc=1)", out)
        self.assertIn("Cl 91%", out)

    def test_partial_failure_json_keeps_error_per_provider(self) -> None:
        self.stub_refresh("kimi", None, error="Kimi helper failed with rc=2")

        code, out = self.run_usage(["--json"])

        self.assertEqual(code, 0)
        kimi = json.loads(out)["providers"]["kimi"]
        self.assertIsNone(kimi["remaining_percent"])
        self.assertEqual(kimi["error"], "Kimi helper failed with rc=2")

    def test_total_failure_exits_nonzero(self) -> None:
        for name in self.refresh_mocks:
            self.stub_refresh(name, None, error=f"{name} unavailable")

        code, out = self.run_usage([])

        self.assertEqual(code, 1)
        self.assertIn("Cl n/a", out)

    def test_unconfigured_provider_is_skipped(self) -> None:
        patcher = mock.patch.object(quota, "_grok_login_exists", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        code, out = self.run_usage([])

        self.assertEqual(code, 0)
        self.assertNotIn("Gk", out)

    def test_invalid_flag_exits_nonzero(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = quota.ag_command(["usage", "--bogus"], now=NOW)

        self.assertEqual(code, 2)

    def test_usage_never_enters_lane_picker(self) -> None:
        with mock.patch.object(
            quota, "resolve_route", side_effect=AssertionError("picker reached")
        ):
            code, _ = self.run_usage([])

        self.assertEqual(code, 0)

    def test_main_dispatches_usage_flags_past_ag_parser(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = quota.main(["ag", "usage", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["providers"]["grok"]["remaining_percent"], 30)

    def test_main_usage_help_and_invalid_flag(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            help_code = quota.main(["ag", "usage", "--help"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            bad_code = quota.main(["ag", "usage", "--bogus"])

        self.assertEqual(help_code, 0)
        self.assertIn("--refresh", stdout.getvalue())
        self.assertEqual(bad_code, 2)

    def test_existing_ag_parser_still_offers_picker(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = quota.main(["ag", "--help"])

        self.assertEqual(code, 0)
        self.assertIn("--classified", stdout.getvalue())
        self.assertIn("ag usage", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

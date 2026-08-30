import json
import multiprocessing
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import herdr_model_lanes as quota

NOW = 1_700_000_000


def window(used: int, remaining: int, reset_delta: int | None) -> quota.QuotaWindow:
    resets_at = None if reset_delta is None else NOW + reset_delta
    return quota.QuotaWindow(used, remaining, resets_at)


class CodexParseTests(unittest.TestCase):
    def test_parses_weekly_primary_window(self) -> None:
        payload = {
            "rateLimits": {
                "primary": {
                    "usedPercent": 93,
                    "windowDurationMins": 10_080,
                    "resetsAt": 1_787_201_523,
                },
                "secondary": None,
                "planType": "pro",
            }
        }

        usage = quota.parse_codex_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.weekly.remaining_percent, 7)
        self.assertEqual(usage.weekly.resets_at, 1_787_201_523)
        self.assertEqual(usage.plan, "pro")

    def test_selects_weekly_window_by_duration_not_position(self) -> None:
        payload = {
            "rateLimits": {
                "primary": {
                    "usedPercent": 20,
                    "windowDurationMins": 300,
                    "resetsAt": NOW + 1_000,
                },
                "secondary": {
                    "usedPercent": 61,
                    "windowDurationMins": 10_080,
                    "resetsAt": NOW + 2_000,
                },
            }
        }

        usage = quota.parse_codex_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.weekly.remaining_percent, 39)
        self.assertEqual(usage.weekly.resets_at, NOW + 2_000)

    def test_rejects_non_weekly_or_invalid_percentage(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "weekly"):
            quota.parse_codex_usage(
                {
                    "rateLimits": {
                        "primary": {"usedPercent": 20, "windowDurationMins": 300}
                    }
                },
                fetched_at=NOW,
            )
        with self.assertRaisesRegex(quota.QuotaError, "invalid"):
            quota.parse_codex_usage(
                {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 110,
                            "windowDurationMins": 10_080,
                            "resetsAt": NOW,
                        }
                    }
                },
                fetched_at=NOW,
            )


class ClaudeParseTests(unittest.TestCase):
    def test_parses_live_claude_helper_output(self) -> None:
        payload = {
            "five_hour": {
                "utilization": 2.0,
                "resets_at": "2023-11-14T23:13:20Z",
            },
            "seven_day": {
                "utilization": 9.0,
                "resets_at": "2023-11-20T22:13:20Z",
            },
            "seven_day_sonnet": None,
            "extra_usage": {"is_enabled": False},
        }

        usage = quota.parse_claude_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.weekly.remaining_percent, 91)
        self.assertEqual(usage.session.remaining_percent, 98)
        self.assertIsNone(usage.sonnet)
        self.assertFalse(usage.source_stale)

    def test_preserves_helper_fallback_staleness_and_age(self) -> None:
        payload = {
            "five_hour": {"utilization": 20, "resets_at": None},
            "seven_day": {"utilization": 40, "resets_at": None},
            "seven_day_sonnet": None,
            "stale": True,
            "stale_age_seconds": 7_200,
        }

        usage = quota.parse_claude_usage(payload, fetched_at=NOW)

        self.assertTrue(usage.source_stale)
        self.assertEqual(usage.fetched_at, NOW - 7_200)
        self.assertIsNone(usage.weekly.resets_at)

    def test_rejects_live_weekly_window_without_reset(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "reset"):
            quota.parse_claude_usage(
                {"seven_day": {"utilization": 9, "resets_at": None}},
                fetched_at=NOW,
            )


class FormatTests(unittest.TestCase):
    def test_formats_both_weekly_allowances_and_warnings(self) -> None:
        codex = quota.CodexUsage(window(93, 7, 6 * 86_400 + 3 * 3_600), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(9, 91, 5 * 86_400 + 2 * 3_600),
            session=window(2, 98, 2 * 3_600),
            sonnet=None,
            fetched_at=NOW,
        )

        self.assertEqual(
            quota.format_quota(codex, claude, now=NOW),
            "Cx 7%!! · 6d3h | Cl 91% · 5d2h",
        )

    def test_surfaces_tighter_five_hour_and_sonnet_constraints(self) -> None:
        claude = quota.ClaudeUsage(
            weekly=window(50, 50, 6 * 86_400),
            session=window(85, 15, 2 * 3_600),
            sonnet=window(92, 8, 3 * 86_400),
            fetched_at=NOW,
        )

        self.assertEqual(
            quota.format_quota(None, claude, now=NOW),
            "Cx n/a | Cl 50% · 6d0h / 5h 15%! · 2h / S 8%!! · 3d0h",
        )

    def test_marks_sources_stale_and_keeps_unknown_independent(self) -> None:
        claude = quota.ClaudeUsage(
            weekly=window(9, 91, None),
            session=None,
            sonnet=None,
            fetched_at=NOW,
            source_stale=True,
        )

        self.assertEqual(
            quota.format_quota(None, claude, now=NOW),
            "Cx n/a | Cl 91%~",
        )

    def test_coerces_wrong_type_optional_segments_to_na(self) -> None:
        wrong = quota.GrokUsage(window(50, 50, 10_000), NOW)

        line = quota.format_quota(
            None,
            None,
            now=NOW,
            grok="not-a-usage",
            glm=wrong,
            antigravity=wrong,
            kimi=wrong,
            cursor=wrong,
        )

        self.assertEqual(
            line,
            "Cx n/a | Cl n/a | Gk n/a | Gl n/a | Ag n/a | Km n/a | Cu n/a",
        )


class CacheTests(unittest.TestCase):
    def test_round_trips_only_normalized_fields(self) -> None:
        codex = quota.CodexUsage(window(93, 7, 10_000), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(9, 91, 20_000),
            session=window(2, 98, 1_000),
            sonnet=None,
            fetched_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_path = root / quota.CODEX_CACHE_FILENAME
            claude_path = root / quota.CLAUDE_CACHE_FILENAME
            quota.save_codex_cache(codex_path, codex)
            quota.save_claude_cache(claude_path, claude)
            codex_raw = json.loads(codex_path.read_text())
            claude_raw = json.loads(claude_path.read_text())

            self.assertEqual(quota.load_codex_cache(codex_path), codex)
            self.assertEqual(quota.load_claude_cache(claude_path), claude)

        self.assertEqual(set(codex_raw), {"weekly", "fetched_at", "plan"})
        self.assertEqual(
            set(claude_raw),
            {"weekly", "session", "sonnet", "fetched_at", "source_stale"},
        )
        self.assertNotIn("access_token", json.dumps(claude_raw).lower())


class ClaudeQueryTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.subprocess.run")
    def test_invokes_bounded_helper_and_parses_only_json_output(
        self, run: mock.Mock
    ) -> None:
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "five_hour": None,
                    "seven_day": {
                        "utilization": 9,
                        "resets_at": "2023-11-20T22:13:20Z",
                    },
                    "seven_day_sonnet": None,
                }
            ),
            stderr="",
        )

        usage = quota.query_claude(
            now=NOW, claude_command=["/trusted/claude-max-usage"]
        )

        self.assertEqual(usage.weekly.remaining_percent, 91)
        self.assertEqual(run.call_args.args[0], ["/trusted/claude-max-usage"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @mock.patch("herdr_model_lanes.subprocess.run")
    def test_default_command_uses_bundled_helper_with_current_python(
        self, run: mock.Mock
    ) -> None:
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "five_hour": None,
                    "seven_day": {
                        "utilization": 9,
                        "resets_at": "2023-11-20T22:13:20Z",
                    },
                    "seven_day_sonnet": None,
                }
            ),
            stderr="",
        )

        quota.query_claude(now=NOW)

        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]).name, "claude_max_usage.py")
        self.assertEqual(Path(command[1]).parent, Path(quota.__file__).parent)
        self.assertFalse(run.call_args.kwargs.get("shell", False))


class SidebarTests(unittest.TestCase):
    def test_representative_two_rows(self) -> None:
        codex = quota.CodexUsage(window(25, 75, 2 * 86_400), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(9, 91, 5 * 3_600),
            session=window(50, 50, 3_600),
            sonnet=None,
            fetched_at=NOW,
        )
        grok = quota.GrokUsage(window(80, 20, 86_400), NOW)
        glm = quota.GlmUsage(window(60, 40, 4 * 3_600), NOW)
        kimi = quota.KimiUsage(window(30, 70, 10 * 3_600), NOW)

        core, other = quota.format_sidebar_rows(
            codex, claude, NOW, grok=grok, glm=glm, kimi=kimi
        )

        self.assertEqual(core, "Cx 75%·2d0h  Cl 91%·5h")
        self.assertEqual(other, "5h 50%·1h  Gk 20%·1d0h  +2")
        for row in (core, other):
            self.assertLessEqual(len(row), quota.SIDEBAR_MAX_WIDTH)

    def test_row_width_never_exceeds_36(self) -> None:
        codex = quota.CodexUsage(window(1, 99, 6 * 86_400), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(1, 99, 6 * 86_400),
            session=window(1, 99, 4 * 3_600),
            sonnet=window(1, 99, 6 * 86_400),
            fetched_at=NOW,
        )
        providers = (
            quota.GrokUsage(window(1, 99, 6 * 86_400), NOW),
            quota.GlmUsage(window(1, 99, 4 * 3_600), NOW),
            quota.AntigravityUsage(window(1, 99, 6 * 86_400), NOW),
            quota.KimiUsage(window(1, 99, 6 * 86_400), NOW),
            quota.CursorUsage(window(1, 99, 20 * 86_400), NOW),
        )

        core, other = quota.format_sidebar_rows(
            codex,
            claude,
            NOW,
            grok=providers[0],
            glm=providers[1],
            antigravity=providers[2],
            kimi=providers[3],
            cursor=providers[4],
        )

        self.assertLessEqual(len(core), 36)
        self.assertLessEqual(len(other), 36)

    def test_overflow_fits_whole_segments_then_plus_n(self) -> None:
        claude = quota.ClaudeUsage(
            weekly=window(1, 99, 6 * 86_400),
            session=window(1, 99, 4 * 3_600),
            sonnet=window(1, 99, 6 * 86_400),
            fetched_at=NOW,
        )
        grok = quota.GrokUsage(window(1, 99, 6 * 86_400), NOW)
        glm = quota.GlmUsage(window(1, 99, 4 * 3_600), NOW)
        antigravity = quota.AntigravityUsage(window(1, 99, 6 * 86_400), NOW)
        kimi = quota.KimiUsage(window(1, 99, 6 * 86_400), NOW)
        cursor = quota.CursorUsage(window(1, 99, 20 * 86_400), NOW)

        core, other = quota.format_sidebar_rows(
            None,
            claude,
            NOW,
            grok=grok,
            glm=glm,
            antigravity=antigravity,
            kimi=kimi,
            cursor=cursor,
        )

        self.assertEqual(core, "Cl 99%·6d0h")
        # The weekly-relative constraints are not tighter, so five optional
        # segments remain; only whole segments fit and the rest is +3.
        self.assertEqual(other, "Gk 99%·6d0h  Gl 99%·4h  +3")
        self.assertLessEqual(len(other), 36)
        # Only whole segments appear: no truncated labels.
        for part in other.split("  "):
            self.assertRegex(part, r"^(5h|S|Gk|Gl|Ag|Km|Cu|\+\d+)")

    def test_omitted_providers_do_not_appear(self) -> None:
        claude = quota.ClaudeUsage(
            weekly=window(10, 90, 86_400),
            session=None,
            sonnet=None,
            fetched_at=NOW,
        )

        core, other = quota.format_sidebar_rows(None, claude, NOW)

        self.assertEqual(core, "Cl 90%·1d0h")
        self.assertEqual(other, "")

    def test_warning_and_stale_marks_survive(self) -> None:
        codex = quota.CodexUsage(window(93, 7, 3_600), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(85, 15, 3_600),
            session=window(95, 5, 1_800),
            sonnet=None,
            fetched_at=NOW,
        )

        core, other = quota.format_sidebar_rows(
            codex, claude, NOW, codex_stale=True, claude_stale=True
        )

        self.assertEqual(core, "Cx 7%!!~·1h  Cl 15%!~·1h")
        self.assertEqual(other, "5h 5%!!·30m")

    def test_sonnet_appears_only_when_tighter(self) -> None:
        claude = quota.ClaudeUsage(
            weekly=window(10, 90, 86_400),
            session=None,
            sonnet=window(80, 20, 86_400),
            fetched_at=NOW,
        )
        loose = quota.ClaudeUsage(
            weekly=window(10, 90, 86_400),
            session=None,
            sonnet=window(10, 90, 86_400),
            fetched_at=NOW,
        )

        _, tight_row = quota.format_sidebar_rows(None, claude, NOW)
        _, loose_row = quota.format_sidebar_rows(None, loose, NOW)

        self.assertEqual(tight_row, "S 20%·1d0h")
        self.assertEqual(loose_row, "")

    def test_no_live_segments_yield_empty_rows(self) -> None:
        self.assertEqual(quota.format_sidebar_rows(None, None, NOW), ("", ""))

    def test_wrong_optional_types_are_omitted_not_na(self) -> None:
        core, other = quota.format_sidebar_rows(None, None, NOW, grok=object())

        self.assertEqual((core, other), ("", ""))


class PublicationTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.subprocess.run")
    def test_publishes_rows_only_on_focused_workspace(self, run: mock.Mock) -> None:
        run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "workspaces": [
                                {"workspace_id": "w1", "focused": False},
                                {"workspace_id": "w2", "focused": True},
                            ]
                        }
                    }
                ),
                stderr="",
            )
        ] + [mock.Mock(returncode=0, stdout="{}", stderr="") for _ in range(2)]

        quota.publish_to_focused_workspace("Cx 7%·1h", "5h 50%·1h", herdr_bin="herdr")

        calls = [item.args[0] for item in run.call_args_list]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ["herdr", "workspace", "list"])
        self.assertEqual(calls[1][:2], ["herdr", "workspace"])
        self.assertEqual(calls[1][3], "w1")
        self.assertEqual(
            calls[1],
            [
                "herdr",
                "workspace",
                "report-metadata",
                "w1",
                "--source",
                quota.PLUGIN_ID,
                "--ttl-ms",
                str(quota.TOKEN_TTL_MS),
                "--clear-token",
                "model_quota",
                "--clear-token",
                "model_quota_core",
                "--clear-token",
                "model_quota_other",
            ],
        )
        self.assertEqual(calls[2][3], "w2")
        self.assertEqual(
            calls[2],
            [
                "herdr",
                "workspace",
                "report-metadata",
                "w2",
                "--source",
                quota.PLUGIN_ID,
                "--ttl-ms",
                str(quota.TOKEN_TTL_MS),
                "--clear-token",
                "model_quota",
                "--token",
                "model_quota_core=Cx 7%·1h",
                "--token",
                "model_quota_other=5h 50%·1h",
            ],
        )

    @mock.patch("herdr_model_lanes.subprocess.run")
    def test_empty_rows_clear_their_tokens_on_focused_workspace(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "workspaces": [
                                {"workspace_id": "w1", "focused": True},
                            ]
                        }
                    }
                ),
                stderr="",
            ),
            mock.Mock(returncode=0, stdout="{}", stderr=""),
        ]

        quota.publish_to_focused_workspace("", "")

        calls = [item.args[0] for item in run.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ["herdr", "workspace", "list"])
        self.assertEqual(calls[1][3], "w1")
        self.assertEqual(
            calls[1][8:],
            [
                "--clear-token",
                "model_quota",
                "--clear-token",
                "model_quota_core",
                "--clear-token",
                "model_quota_other",
            ],
        )

    @mock.patch("herdr_model_lanes.subprocess.run")
    def test_clear_all_clears_every_token_on_every_workspace(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = [
            mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "result": {
                            "workspaces": [
                                {"workspace_id": "w1", "focused": True},
                                {"workspace_id": "w2", "focused": False},
                            ]
                        }
                    }
                ),
                stderr="",
            )
        ] + [mock.Mock(returncode=0, stdout="{}", stderr="") for _ in range(2)]

        quota.clear_all()

        calls = [item.args[0] for item in run.call_args_list]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ["herdr", "workspace", "list"])
        expected_clears = [
            "--clear-token",
            "model_quota",
            "--clear-token",
            "model_quota_core",
            "--clear-token",
            "model_quota_other",
        ]
        for call in calls[1:]:
            self.assertEqual(call[:2], ["herdr", "workspace"])
            self.assertEqual(call[6:], expected_clears)
        self.assertEqual([call[3] for call in calls[1:]], ["w1", "w2"])


class RefreshTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_claude")
    @mock.patch("herdr_model_lanes.query_codex")
    def test_refreshes_sources_on_independent_intervals(
        self,
        query_codex: mock.Mock,
        query_claude: mock.Mock,
        publish: mock.Mock,
    ) -> None:
        cached_codex = quota.CodexUsage(window(93, 7, 10_000), NOW - 100, "pro")
        cached_claude = quota.ClaudeUsage(
            weekly=window(10, 90, 20_000),
            session=None,
            sonnet=None,
            fetched_at=NOW - 2_000,
        )
        refreshed_claude = quota.ClaudeUsage(
            weekly=window(9, 91, 20_000),
            session=None,
            sonnet=None,
            fetched_at=NOW,
        )
        query_claude.return_value = refreshed_claude

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.save_codex_cache(root / quota.CODEX_CACHE_FILENAME, cached_codex)
            quota.save_claude_cache(root / quota.CLAUDE_CACHE_FILENAME, cached_claude)
            outcome = quota.refresh(state_dir=root, now=NOW)

        query_codex.assert_not_called()
        query_claude.assert_called_once()
        self.assertEqual(outcome.codex, cached_codex)
        self.assertEqual(outcome.claude, refreshed_claude)
        publish.assert_called_once_with(
            "Cx 7%!!·2h  Cl 91%·5h",
            "",
            herdr_bin="herdr",
        )

    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_claude")
    @mock.patch("herdr_model_lanes.query_codex")
    def test_failed_refresh_keeps_each_last_value_stale(
        self,
        query_codex: mock.Mock,
        query_claude: mock.Mock,
        publish: mock.Mock,
    ) -> None:
        codex = quota.CodexUsage(window(93, 7, 10_000), NOW - 1_000, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(9, 91, 20_000),
            session=None,
            sonnet=None,
            fetched_at=NOW - 2_000,
        )
        query_codex.side_effect = quota.QuotaError("Codex offline")
        query_claude.side_effect = quota.QuotaError("Claude offline")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.save_codex_cache(root / quota.CODEX_CACHE_FILENAME, codex)
            quota.save_claude_cache(root / quota.CLAUDE_CACHE_FILENAME, claude)
            outcome = quota.refresh(force=True, state_dir=root, now=NOW)

        self.assertTrue(outcome.codex_stale)
        self.assertTrue(outcome.claude_stale)
        self.assertEqual(len(outcome.errors), 2)
        publish.assert_called_once_with(
            "Cx 7%!!~·2h  Cl 91%~·5h",
            "",
            herdr_bin="herdr",
        )


def _lock_writer(state_dir: str, remaining: int) -> None:
    path = Path(state_dir) / quota.CODEX_CACHE_FILENAME
    usage = quota.CodexUsage(window(100 - remaining, remaining, 10_000), NOW, "pro")
    for _ in range(25):
        with quota._cache_lock(path):
            quota.save_codex_cache(path, usage)
            loaded = quota.load_codex_cache(path)
            if loaded is None:
                raise RuntimeError("corrupt cache")


class BackoffTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.query_claude")
    def test_stale_attempt_does_not_block_forced_refresh(
        self, query_claude: mock.Mock
    ) -> None:
        refreshed = quota.ClaudeUsage(
            weekly=window(9, 91, 20_000),
            session=None,
            sonnet=None,
            fetched_at=NOW,
        )
        query_claude.return_value = refreshed
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / quota.CLAUDE_CACHE_FILENAME
            quota._save_attempt(quota._attempt_path(cache_path), NOW)
            usage, stale, error = quota._refresh_claude(
                True, cache_path, NOW, ["/trusted/claude-max-usage"]
            )

        query_claude.assert_called_once()
        self.assertEqual(usage, refreshed)
        self.assertFalse(stale)
        self.assertIsNone(error)

    @mock.patch("herdr_model_lanes.query_claude")
    def test_failed_claude_attempt_is_backed_off_without_a_cache(
        self, query_claude: mock.Mock
    ) -> None:
        query_claude.side_effect = quota.QuotaError("rate limited")
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / quota.CLAUDE_CACHE_FILENAME
            first = quota._refresh_claude(
                False, cache_path, NOW, ["/trusted/claude-max-usage"]
            )
            second = quota._refresh_claude(
                False, cache_path, NOW + 10, ["/trusted/claude-max-usage"]
            )

        self.assertIsNone(first[0])
        self.assertIsNone(second[0])
        query_claude.assert_called_once()


class LockTests(unittest.TestCase):
    def test_concurrent_writers_do_not_corrupt_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            procs = [
                multiprocessing.Process(
                    target=_lock_writer, args=(directory, remaining)
                )
                for remaining in (10, 90)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=15)
            self.assertTrue(all(proc.exitcode == 0 for proc in procs))
            loaded = quota.load_codex_cache(
                Path(directory) / quota.CODEX_CACHE_FILENAME
            )
            self.assertIsNotNone(loaded)
            self.assertIn(loaded.weekly.remaining_percent, (10, 90))


class PublishResilienceTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.query_claude")
    def test_missing_herdr_does_not_abort_refresh(
        self, query_claude: mock.Mock
    ) -> None:
        query_claude.side_effect = quota.QuotaError("no claude")
        cached = quota.CodexUsage(window(20, 80, 10_000), NOW, "pro")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.save_codex_cache(root / quota.CODEX_CACHE_FILENAME, cached)
            outcome = quota.refresh(
                state_dir=root,
                now=NOW,
                herdr_bin="/no-such-herdr-binary",
            )

        self.assertEqual(outcome.codex, cached)


class ManifestTests(unittest.TestCase):
    def test_manifest_uses_model_quota_identity_and_local_commands(self) -> None:
        manifest = tomllib.loads(Path("herdr-plugin.toml").read_text())

        self.assertEqual(manifest["id"], "terry.herdr-model-lanes")
        self.assertEqual(manifest["version"], "3.8.0")
        self.assertEqual(manifest["min_herdr_version"], "0.8.0")
        self.assertEqual(manifest["platforms"], ["macos", "linux"])
        self.assertNotIn("build", manifest)
        action_ids = [item["id"] for item in manifest["actions"]]
        self.assertIn("route-high", action_ids)
        route_high = next(
            item for item in manifest["actions"] if item["id"] == "route-high"
        )
        self.assertEqual(
            route_high["command"],
            ["python3", "herdr_model_lanes.py", "route", "high", "--launch"],
        )
        self.assertTrue(
            all("on" in event and "event" not in event for event in manifest["events"])
        )
        commands = []
        for section in ("startup", "actions", "events"):
            commands.extend(item["command"] for item in manifest.get(section, []))
        for command in commands:
            self.assertEqual(command[0:2], ["python3", "herdr_model_lanes.py"])


if __name__ == "__main__":
    unittest.main()

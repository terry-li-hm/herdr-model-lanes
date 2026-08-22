import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import herdr_model_quota as quota

NOW = 1_700_000_000
WEEK = 604_800


def window(remaining: int, reset_delta: int | None) -> quota.QuotaWindow:
    resets_at = None if reset_delta is None else NOW + reset_delta
    return quota.QuotaWindow(100 - remaining, remaining, resets_at)


def medium_spec() -> quota.ClassSpec:
    return quota.load_class_spec("medium")


class ClassSpecTests(unittest.TestCase):
    def test_loads_medium_class_from_classes_toml(self) -> None:
        spec = medium_spec()

        self.assertEqual(spec.name, "medium")
        self.assertEqual(
            [lane.name for lane in spec.lanes],
            ["sol", "opus", "glm", "grok", "cursor"],
        )
        sol = spec.lanes[0]
        self.assertEqual(sol.kind, "pi")
        self.assertEqual(
            sol.args, ("--provider", "openai-codex", "--model", "gpt-5.6-sol")
        )
        self.assertEqual(sol.quota, "codex")
        self.assertTrue(sol.classified_ok)
        self.assertEqual(spec.lanes[1].quota, "grok")
        opus = spec.lanes[2]
        self.assertEqual(opus.kind, "claude")
        self.assertEqual(opus.args, ("--model", "claude-opus-5"))
        self.assertEqual(opus.quota, "claude")
        glm = spec.lanes[3]
        self.assertEqual(
            glm.args, ("--provider", "bigmodel-coding", "--model", "glm-5.3")
        )
        self.assertEqual(glm.quota, "glm")
        self.assertFalse(glm.classified_ok)
        cursor = spec.lanes[4]
        self.assertEqual(cursor.kind, "cursor")
        self.assertEqual(cursor.args, ())
        self.assertEqual(cursor.quota, "cursor")
        self.assertTrue(cursor.classified_ok)

    def test_loads_high_class_from_classes_toml(self) -> None:
        spec = quota.load_class_spec("high")

        self.assertEqual([lane.name for lane in spec.lanes], ["fable", "sol"])
        fable = spec.lanes[0]
        self.assertEqual(fable.kind, "claude")
        self.assertEqual(fable.args, ("--model", "claude-fable-5"))
        self.assertEqual(fable.quota, "claude")
        self.assertTrue(fable.classified_ok)
        self.assertEqual(spec.lanes[1].name, "sol")
        self.assertEqual(spec.lanes[1].quota, "codex")

    def test_unknown_class_is_rejected(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "unknown model class"):
            quota.load_class_spec("heavy")


class SelectionTests(unittest.TestCase):
    def test_both_healthy_first_lane_in_order_wins(self) -> None:
        spec = medium_spec()
        quotas = {"codex": window(100, WEEK), "grok": window(70, WEEK)}

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "sol")
        self.assertIn("pick: sol", lines[-1])

    def test_first_unhealthy_falls_through_to_second(self) -> None:
        spec = medium_spec()
        quotas = {"codex": window(15, WEEK), "grok": window(100, WEEK)}

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")

    def test_both_unhealthy_picks_highest_health(self) -> None:
        spec = medium_spec()
        # codex: 50% left with the full window remaining -> health 0.5.
        # grok: 30% left with half the window remaining -> health 0.6.
        quotas = {"codex": window(50, WEEK), "grok": window(30, WEEK // 2)}

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")

    def test_unavailable_quota_ranks_last(self) -> None:
        spec = medium_spec()
        quotas = {"codex": None, "grok": window(15, WEEK)}

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")
        self.assertIn("sol: n/a", lines[0])

    def test_all_unavailable_picks_first_lane_by_order(self) -> None:
        spec = medium_spec()

        lane, _ = quota.select_lane(spec, {"codex": None, "grok": None}, NOW)

        self.assertEqual(lane.name, "sol")

    def test_classified_filter_drops_lanes_not_classified_ok(self) -> None:
        spec = quota.ClassSpec(
            name="test",
            description="",
            lanes=(
                quota.LaneSpec("a", "pi", (), "codex", False),
                quota.LaneSpec("b", "pi", (), "grok", True),
            ),
        )

        lane, _ = quota.select_lane(
            spec,
            {"codex": window(90, WEEK), "grok": window(10, WEEK)},
            NOW,
            classified=True,
        )

        self.assertEqual(lane.name, "b")

    def test_surplus_branch_picks_lane_resetting_within_48h(self) -> None:
        spec = medium_spec()
        # grok: 60% left with one day of the window -> health 4.2 and an
        # imminent reset, while sol is merely healthy and first in order.
        quotas = {"codex": window(90, WEEK), "grok": window(60, 86_400)}

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")
        self.assertIn("surplus before reset", lines[-1])

    def test_highest_health_surplus_lane_wins(self) -> None:
        spec = medium_spec()
        # grok health 4.2 beats opus health 2.1; both reset within 48h.
        quotas = {
            "codex": window(100, WEEK),
            "grok": window(60, 86_400),
            "claude": window(30, 86_400),
        }

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")

    def test_surplus_ignored_when_reset_is_beyond_48h(self) -> None:
        spec = medium_spec()
        # grok: 80% left, 60h to reset -> health 2.24 but no imminent reset.
        quotas = {"codex": window(100, WEEK), "grok": window(80, 60 * 3_600)}

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "sol")
        self.assertIn("first healthy lane in order", lines[-1])

    def test_surplus_ignored_when_health_below_two(self) -> None:
        spec = medium_spec()
        # grok: 50% left, 48h to reset -> health 1.75, resets at the edge.
        quotas = {"codex": window(100, WEEK), "grok": window(50, 48 * 3_600)}

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "sol")

    def test_na_only_class_picks_first_lane_by_order(self) -> None:
        spec = medium_spec()

        lane, lines = quota.select_lane(spec, {"glm": None, "cursor": None}, NOW)

        self.assertEqual(lane.name, "sol")
        self.assertIn("no lane has quota data", lines[-1])

    def test_classified_drops_glm_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(100, 86_400),
            "cursor": window(100, WEEK),
        }

        lane, lines = quota.select_lane(spec, quotas, NOW, classified=True)

        self.assertEqual(lane.name, "cursor")
        self.assertFalse(any("glm:" in line for line in lines))

    def test_high_class_picks_fable_when_claude_healthy(self) -> None:
        spec = quota.load_class_spec("high")
        quotas = {
            "claude": window(100, WEEK),
            "codex": window(90, WEEK),
        }

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "fable")

    def test_high_class_picks_sol_when_claude_unhealthy(self) -> None:
        spec = quota.load_class_spec("high")
        quotas = {
            "claude": window(5, WEEK),
            "codex": window(100, WEEK),
        }

        lane, _ = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "sol")


class GrokWindowTests(unittest.TestCase):
    def test_parse_grok_usage_from_helper_shape(self) -> None:
        payload = {
            "weekly": {
                "used_percent": 58,
                "remaining_percent": 42,
                "resets_at": "2026-08-22T00:00:00Z",
            }
        }

        usage = quota.parse_grok_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.weekly.remaining_percent, 42)
        self.assertEqual(usage.weekly.resets_at, 1_787_356_800)

    def test_rejects_missing_weekly_window(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "weekly"):
            quota.parse_grok_usage({"weekly": None}, fetched_at=NOW)

    def test_formats_three_sources_including_gk(self) -> None:
        codex = quota.CodexUsage(window(75, 5 * 86_400 + 22 * 3_600), NOW, "pro")
        claude = quota.ClaudeUsage(
            weekly=window(9, 86_400 + 3_600),
            session=None,
            sonnet=None,
            fetched_at=NOW,
        )
        grok = quota.GrokUsage(window(62, 3 * 86_400 + 2 * 3_600), NOW)

        self.assertEqual(
            quota.format_quota(codex, claude, NOW, grok=grok),
            "Cx 75% · 5d22h | Cl 9%!! · 1d1h | Gk 62% · 3d2h",
        )

    def test_gk_na_when_helper_cannot_run(self) -> None:
        self.assertEqual(
            quota.format_quota(None, None, NOW, grok=None, grok_stale=True),
            "Cx n/a | Cl n/a | Gk n/a",
        )

    def test_grok_cache_round_trip(self) -> None:
        usage = quota.GrokUsage(window(58, 10_000), NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.GROK_CACHE_FILENAME
            quota.save_grok_cache(path, usage)

            self.assertEqual(quota.load_grok_cache(path), usage)
            self.assertEqual(
                set(json.loads(path.read_text())), {"weekly", "fetched_at"}
            )


class GrokRefreshTests(unittest.TestCase):
    @mock.patch("herdr_model_quota.query_grok")
    def test_grok_refreshes_on_thirty_minute_cadence(
        self, query_grok: mock.Mock
    ) -> None:
        cached = quota.GrokUsage(window(58, 10_000), NOW - 1_000)
        query_grok.return_value = quota.GrokUsage(window(58, 20_000), NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.GROK_CACHE_FILENAME
            quota.save_grok_cache(path, cached)
            within = quota._refresh_grok(False, path, NOW - 1_000 + 1_500, None)
            outside = quota._refresh_grok(False, path, NOW - 1_000 + 1_801, None)

        self.assertEqual(within[0], cached)
        self.assertEqual(outside[0].weekly.remaining_percent, 58)
        query_grok.assert_called_once()

    @mock.patch("herdr_model_quota.publish_to_focused_workspace")
    @mock.patch("herdr_model_quota.query_codex")
    @mock.patch("herdr_model_quota.query_grok")
    def test_refresh_includes_gk_segment_only_when_enabled(
        self, query_grok: mock.Mock, query_codex: mock.Mock, publish: mock.Mock
    ) -> None:
        query_grok.side_effect = quota.QuotaError("Grok offline")
        query_codex.side_effect = quota.QuotaError("Codex offline")

        with tempfile.TemporaryDirectory() as directory:
            outcome = quota.refresh(
                state_dir=Path(directory), now=NOW, include_grok=True
            )

        self.assertIsNone(outcome.grok)
        self.assertIn("Gk n/a", publish.call_args.args[0])


class RouteCommandTests(unittest.TestCase):
    @mock.patch("herdr_model_quota._run_herdr_command")
    @mock.patch("herdr_model_quota.refresh")
    def test_explain_prints_lanes_and_pick_and_shows_notification(
        self, refresh: mock.Mock, herdr_run: mock.Mock
    ) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=None,
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=None,
            grok_stale=False,
        )
        herdr_run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch("builtins.print") as printed:
            rc = quota.route_command(["medium", "--explain"])

        self.assertEqual(rc, 0)
        output = "\n".join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn("sol: n/a", output)
        self.assertIn("grok: n/a", output)
        self.assertIn("pick: sol", output)
        notification = herdr_run.call_args.args[1]
        self.assertEqual(
            notification,
            ["notification", "show", "Route: medium", "--body", mock.ANY],
        )

    @mock.patch("herdr_model_quota._run_herdr_command")
    @mock.patch("herdr_model_quota.refresh")
    def test_explain_scales_back_when_herdr_notification_fails(
        self, refresh: mock.Mock, herdr_run: mock.Mock
    ) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=None,
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=None,
            grok_stale=False,
        )
        herdr_run.side_effect = quota.QuotaError("herdr missing")

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            rc = quota.route_command(["medium", "--explain"])

        self.assertEqual(rc, 0)
        self.assertIn("notification skipped", stderr.getvalue())

    @mock.patch("herdr_model_quota._run_herdr_command")
    @mock.patch("herdr_model_quota.refresh")
    def test_argv_prints_quoted_command_without_notification(
        self, refresh: mock.Mock, herdr_run: mock.Mock
    ) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=quota.CodexUsage(window(70, WEEK), NOW, "pro"),
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=None,
            grok_stale=False,
        )

        with (
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = quota.route_command(["medium", "--argv"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            stdout.getvalue().strip(),
            "pi --provider openai-codex --model gpt-5.6-sol",
        )
        self.assertIn("pick: sol", stderr.getvalue())
        herdr_run.assert_not_called()

    def test_lane_command_maps_kinds_to_executables(self) -> None:
        cursor = quota.LaneSpec("cursor", "cursor", (), "cursor", True)
        unknown = quota.LaneSpec("odd", "zzz", ("--flag", "a b"), "zzz", True)

        self.assertEqual(quota.lane_command(cursor), ["cursor-agent"])
        self.assertEqual(quota.lane_command(unknown), ["zzz", "--flag", "a b"])

    @mock.patch("herdr_model_quota._run_herdr_command")
    @mock.patch("herdr_model_quota.refresh")
    def test_launch_creates_tab_prints_rationale_and_starts_agent(
        self, refresh: mock.Mock, herdr_run: mock.Mock
    ) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=None,
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=quota.GrokUsage(window(70, WEEK), NOW),
            grok_stale=False,
        )
        herdr_run.side_effect = [
            mock.Mock(  # pane get
                returncode=0,
                stdout=json.dumps({"result": {"pane": {"cwd": "/Users/terry/work"}}}),
                stderr="",
            ),
            mock.Mock(  # tab create
                returncode=0,
                stdout=json.dumps(
                    {"result": {"root_pane": {"pane_id": "p9"}, "tab": {"number": 7}}}
                ),
                stderr="",
            ),
            mock.Mock(returncode=0, stdout="{}", stderr=""),  # pane run
            mock.Mock(returncode=0, stdout="{}", stderr=""),  # agent start
        ]

        with mock.patch.dict(
            "os.environ",
            {
                "HERDR_WORKSPACE_ID": "w1",
                "HERDR_PANE_ID": "p1",
                "HOME": "/Users/terry",
            },
        ):
            rc = quota.route_command(["medium", "--launch"])

        self.assertEqual(rc, 0)
        argvs = [call.args[1] for call in herdr_run.call_args_list]
        self.assertEqual(argvs[0], ["pane", "get", "p1"])
        self.assertEqual(
            argvs[1],
            [
                "tab",
                "create",
                "--workspace",
                "w1",
                "--cwd",
                "/Users/terry/work",
                "--label",
                "medium: grok",
            ],
        )
        self.assertEqual(argvs[2][0:3], ["pane", "run", "p9"])
        self.assertTrue(argvs[2][3].startswith("printf"))
        self.assertEqual(argvs[3][0:2], ["agent", "start"])
        self.assertEqual(argvs[3][2], "medium-grok-7")
        self.assertEqual(argvs[3][3:7], ["--kind", "grok", "--pane", "p9"])
        self.assertEqual(argvs[3][7], "--")


if __name__ == "__main__":
    unittest.main()

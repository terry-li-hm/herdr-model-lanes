import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import herdr_model_lanes as quota

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
            ["sol", "opus", "glm", "grok", "agy", "kimi", "cursor"],
        )
        sol = spec.lanes[0]
        self.assertEqual(sol.kind, "pi")
        self.assertEqual(
            sol.args, ("--provider", "openai-codex", "--model", "gpt-5.6-sol")
        )
        self.assertEqual(sol.quota, "codex")
        self.assertTrue(sol.classified_ok)
        opus = spec.lanes[1]
        self.assertEqual(opus.kind, "claude")
        self.assertEqual(opus.args, ("--model", "claude-opus-5"))
        self.assertEqual(opus.quota, "claude")
        glm = spec.lanes[2]
        self.assertEqual(
            glm.args, ("--provider", "bigmodel-coding", "--model", "glm-5.3")
        )
        self.assertEqual(glm.quota, "glm")
        self.assertFalse(glm.classified_ok)
        self.assertEqual(spec.lanes[3].quota, "grok")
        agy = spec.lanes[4]
        self.assertEqual(agy.kind, "agy")
        self.assertEqual(agy.args, ())
        self.assertEqual(agy.quota, "antigravity")
        self.assertFalse(agy.classified_ok)
        kimi = spec.lanes[5]
        self.assertEqual(kimi.kind, "kimi")
        self.assertEqual(kimi.args, ())
        self.assertEqual(kimi.quota, "kimi")
        self.assertFalse(kimi.classified_ok)
        cursor = spec.lanes[6]
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
    @mock.patch("herdr_model_lanes.query_grok")
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

    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_codex")
    @mock.patch("herdr_model_lanes.query_grok")
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


class GlmTests(unittest.TestCase):
    def test_parse_glm_usage_from_helper_shape(self) -> None:
        payload = {
            "five_hour": {
                "used_percent": 38,
                "remaining_percent": 62,
                "resets_at": 1_787_356_800,
            }
        }

        usage = quota.parse_glm_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.five_hour.remaining_percent, 62)
        self.assertEqual(usage.five_hour.window_seconds, quota.GLM_WINDOW_SECONDS)

    def test_rejects_missing_five_hour_window(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "five_hour"):
            quota.parse_glm_usage({"five_hour": None}, fetched_at=NOW)

    def test_formats_gl_segment_after_gk(self) -> None:
        glm = quota.GlmUsage(
            quota.QuotaWindow(38, 62, NOW + 4 * 3_600, quota.GLM_WINDOW_SECONDS), NOW
        )

        self.assertEqual(
            quota.format_quota(None, None, NOW, grok=None, glm=glm),
            "Cx n/a | Cl n/a | Gk n/a | Gl 62% · 4h",
        )

    def test_gl_marks_low_remaining_and_stale(self) -> None:
        glm = quota.GlmUsage(
            quota.QuotaWindow(92, 8, NOW + 3_600, quota.GLM_WINDOW_SECONDS), NOW
        )

        self.assertEqual(
            quota.format_quota(None, None, NOW, glm=glm, glm_stale=True),
            "Cx n/a | Cl n/a | Gl 8%!!~ · 1h",
        )

    def test_gl_na_when_no_key(self) -> None:
        self.assertEqual(
            quota.format_quota(None, None, NOW, glm=None),
            "Cx n/a | Cl n/a | Gl n/a",
        )

    def test_glm_cache_round_trip(self) -> None:
        usage = quota.GlmUsage(
            quota.QuotaWindow(38, 62, NOW + 10_000, quota.GLM_WINDOW_SECONDS), NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.GLM_CACHE_FILENAME
            quota.save_glm_cache(path, usage)

            self.assertEqual(quota.load_glm_cache(path), usage)
            self.assertEqual(
                set(json.loads(path.read_text())), {"five_hour", "fetched_at"}
            )

    def test_route_picks_glm_when_it_is_the_only_healthy_lane(self) -> None:
        spec = medium_spec()
        # glm: 80% left with 4h of a five-hour window -> health 1.0+, while
        # the weekly lanes are drained and cursor has no reader.
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": quota.QuotaWindow(20, 80, NOW + 4 * 3_600, quota.GLM_WINDOW_SECONDS),
            "cursor": None,
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "glm")
        self.assertIn("pick: glm", lines[-1])

    def test_five_hour_window_judged_on_its_own_pace(self) -> None:
        # 80% left with 4h of a five-hour window is healthy (0.8 < 1) so it
        # falls to the least-unhealthy branch, while the same numbers on a
        # weekly window would rank far lower.
        weekly = window(80, WEEK)
        five_hour = quota.QuotaWindow(20, 80, NOW + 4 * 3_600, quota.GLM_WINDOW_SECONDS)

        self.assertGreater(
            quota._lane_health(five_hour, NOW), quota._lane_health(weekly, NOW)
        )

    @mock.patch("herdr_model_lanes.query_glm")
    def test_glm_refreshes_on_five_minute_cadence(self, query_glm: mock.Mock) -> None:
        cached = quota.GlmUsage(
            quota.QuotaWindow(38, 62, NOW + 10_000, quota.GLM_WINDOW_SECONDS),
            NOW - 1_000,
        )
        query_glm.return_value = quota.GlmUsage(
            quota.QuotaWindow(38, 62, NOW + 20_000, quota.GLM_WINDOW_SECONDS), NOW
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.GLM_CACHE_FILENAME
            quota.save_glm_cache(path, cached)
            within = quota._refresh_glm(False, path, NOW - 1_000 + 200, None)
            outside = quota._refresh_glm(False, path, NOW - 1_000 + 301, None)

        self.assertEqual(within[0], cached)
        self.assertEqual(outside[0].five_hour.remaining_percent, 62)
        query_glm.assert_called_once()

    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_codex")
    @mock.patch("herdr_model_lanes.query_glm")
    def test_refresh_includes_gl_segment_only_when_enabled(
        self, query_glm: mock.Mock, query_codex: mock.Mock, publish: mock.Mock
    ) -> None:
        query_glm.side_effect = quota.QuotaError("GLM offline")
        query_codex.side_effect = quota.QuotaError("Codex offline")

        with tempfile.TemporaryDirectory() as directory:
            outcome = quota.refresh(
                state_dir=Path(directory), now=NOW, include_glm=True
            )

        self.assertIsNone(outcome.glm)
        self.assertIn("Gl n/a", publish.call_args.args[0])


class AntigravityTests(unittest.TestCase):
    def test_parse_antigravity_usage_from_helper_shape(self) -> None:
        payload = {
            "gemini": {
                "used_percent": 0,
                "remaining_percent": 100,
                "resets_at": "2026-08-29T10:10:41Z",
                "window_seconds": WEEK,
            }
        }

        usage = quota.parse_antigravity_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.gemini.remaining_percent, 100)
        self.assertEqual(usage.gemini.window_seconds, WEEK)

    def test_rejects_missing_gemini_window(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "gemini"):
            quota.parse_antigravity_usage({"gemini": None}, fetched_at=NOW)

    def test_formats_ag_segment_after_gl(self) -> None:
        antigravity = quota.AntigravityUsage(window(100, WEEK), NOW)

        self.assertEqual(
            quota.format_quota(
                None, None, NOW, grok=None, glm=None, antigravity=antigravity
            ),
            "Cx n/a | Cl n/a | Gk n/a | Gl n/a | Ag 100% · 7d0h",
        )

    def test_ag_na_when_app_closed(self) -> None:
        self.assertEqual(
            quota.format_quota(None, None, NOW, antigravity=None),
            "Cx n/a | Cl n/a | Ag n/a",
        )

    def test_antigravity_cache_round_trip(self) -> None:
        usage = quota.AntigravityUsage(window(42, WEEK), NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.ANTIGRAVITY_CACHE_FILENAME
            quota.save_antigravity_cache(path, usage)

            loaded = quota.load_antigravity_cache(path)
            self.assertEqual(loaded, usage)
            self.assertEqual(
                set(json.loads(path.read_text())), {"gemini", "fetched_at"}
            )

    def test_route_picks_agy_when_it_is_the_only_healthy_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(5, WEEK),
            "antigravity": window(100, WEEK),
            "cursor": None,
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "agy")
        self.assertIn("pick: agy", lines[-1])

    def test_classified_drops_agy_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(100, 86_400),
            "antigravity": window(100, WEEK),
            "cursor": window(100, WEEK),
        }

        lane, lines = quota.select_lane(spec, quotas, NOW, classified=True)

        self.assertEqual(lane.name, "cursor")
        self.assertFalse(any("agy:" in line for line in lines))

    @mock.patch("herdr_model_lanes.query_antigravity")
    def test_antigravity_refreshes_on_thirty_minute_cadence(
        self, query_antigravity: mock.Mock
    ) -> None:
        cached = quota.AntigravityUsage(window(100, WEEK), NOW - 1_000)
        query_antigravity.return_value = quota.AntigravityUsage(window(90, WEEK), NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.ANTIGRAVITY_CACHE_FILENAME
            quota.save_antigravity_cache(path, cached)
            within = quota._refresh_antigravity(False, path, NOW - 1_000 + 1_500, None)
            outside = quota._refresh_antigravity(False, path, NOW - 1_000 + 1_801, None)

        self.assertEqual(within[0], cached)
        self.assertEqual(outside[0].gemini.remaining_percent, 90)
        query_antigravity.assert_called_once()

    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_codex")
    @mock.patch("herdr_model_lanes.query_antigravity")
    def test_refresh_includes_ag_segment_only_when_enabled(
        self,
        query_antigravity: mock.Mock,
        query_codex: mock.Mock,
        publish: mock.Mock,
    ) -> None:
        query_antigravity.side_effect = quota.QuotaError("Antigravity offline")
        query_codex.side_effect = quota.QuotaError("Codex offline")

        with tempfile.TemporaryDirectory() as directory:
            outcome = quota.refresh(
                state_dir=Path(directory), now=NOW, include_antigravity=True
            )

        self.assertIsNone(outcome.antigravity)
        self.assertIn("Ag n/a", publish.call_args.args[0])


class KimiTests(unittest.TestCase):
    def test_parse_kimi_usage_from_helper_shape(self) -> None:
        payload = {
            "coding": {
                "used_percent": 0,
                "remaining_percent": 100,
                "resets_at": "2026-08-28T02:01:19Z",
                "window_seconds": WEEK,
            }
        }

        usage = quota.parse_kimi_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.coding.remaining_percent, 100)
        self.assertEqual(usage.coding.window_seconds, WEEK)

    def test_rejects_missing_coding_window(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "coding"):
            quota.parse_kimi_usage({"coding": None}, fetched_at=NOW)

    def test_formats_km_segment_after_ag(self) -> None:
        kimi = quota.KimiUsage(window(100, WEEK), NOW)

        self.assertEqual(
            quota.format_quota(
                None,
                None,
                NOW,
                grok=None,
                glm=None,
                antigravity=None,
                kimi=kimi,
            ),
            "Cx n/a | Cl n/a | Gk n/a | Gl n/a | Ag n/a | Km 100% · 7d0h",
        )

    def test_km_na_when_no_key(self) -> None:
        self.assertEqual(
            quota.format_quota(None, None, NOW, kimi=None),
            "Cx n/a | Cl n/a | Km n/a",
        )

    def test_kimi_cache_round_trip(self) -> None:
        usage = quota.KimiUsage(window(70, WEEK), NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.KIMI_CACHE_FILENAME
            quota.save_kimi_cache(path, usage)

            self.assertEqual(quota.load_kimi_cache(path), usage)
            self.assertEqual(
                set(json.loads(path.read_text())), {"coding", "fetched_at"}
            )

    def test_route_picks_kimi_when_it_is_the_only_healthy_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(5, WEEK),
            "antigravity": window(5, WEEK),
            "kimi": window(100, WEEK),
            "cursor": None,
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "kimi")
        self.assertIn("pick: kimi", lines[-1])

    def test_classified_drops_kimi_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(5, WEEK),
            "antigravity": window(5, WEEK),
            "kimi": window(100, WEEK),
            "cursor": window(100, WEEK),
        }

        lane, lines = quota.select_lane(spec, quotas, NOW, classified=True)

        self.assertEqual(lane.name, "cursor")
        self.assertFalse(any("kimi:" in line for line in lines))

    @mock.patch("herdr_model_lanes.query_kimi")
    def test_kimi_refreshes_on_five_minute_cadence(self, query_kimi: mock.Mock) -> None:
        cached = quota.KimiUsage(window(100, WEEK), NOW - 1_000)
        query_kimi.return_value = quota.KimiUsage(window(90, WEEK), NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.KIMI_CACHE_FILENAME
            quota.save_kimi_cache(path, cached)
            within = quota._refresh_kimi(False, path, NOW - 1_000 + 200, None)
            outside = quota._refresh_kimi(False, path, NOW - 1_000 + 301, None)

        self.assertEqual(within[0], cached)
        self.assertEqual(outside[0].coding.remaining_percent, 90)
        query_kimi.assert_called_once()

    @mock.patch("herdr_model_lanes.publish_to_focused_workspace")
    @mock.patch("herdr_model_lanes.query_codex")
    @mock.patch("herdr_model_lanes.query_kimi")
    def test_refresh_includes_km_segment_only_when_enabled(
        self, query_kimi: mock.Mock, query_codex: mock.Mock, publish: mock.Mock
    ) -> None:
        query_kimi.side_effect = quota.QuotaError("Kimi offline")
        query_codex.side_effect = quota.QuotaError("Codex offline")

        with tempfile.TemporaryDirectory() as directory:
            outcome = quota.refresh(
                state_dir=Path(directory), now=NOW, include_kimi=True
            )

        self.assertIsNone(outcome.kimi)
        self.assertIn("Km n/a", publish.call_args.args[0])


class CursorTests(unittest.TestCase):
    def test_parse_cursor_usage_from_helper_shape(self) -> None:
        payload = {
            "monthly": {
                "used_percent": 1,
                "remaining_percent": 99,
                "resets_at": "2026-09-19T06:05:05Z",
                "window_seconds": 2_678_400,
            }
        }

        usage = quota.parse_cursor_usage(payload, fetched_at=NOW)

        self.assertEqual(usage.monthly.remaining_percent, 99)
        self.assertEqual(usage.monthly.window_seconds, 2_678_400)

    def test_rejects_missing_monthly_window(self) -> None:
        with self.assertRaisesRegex(quota.QuotaError, "monthly"):
            quota.parse_cursor_usage({"monthly": None}, fetched_at=NOW)

    def test_formats_cu_segment_after_km(self) -> None:
        cursor = quota.CursorUsage(window(99, WEEK), NOW)

        self.assertEqual(
            quota.format_quota(
                None,
                None,
                NOW,
                grok=None,
                glm=None,
                antigravity=None,
                kimi=None,
                cursor=cursor,
            ),
            "Cx n/a | Cl n/a | Gk n/a | Gl n/a | Ag n/a | Km n/a | Cu 99% · 7d0h",
        )

    def test_cu_na_when_app_signed_out(self) -> None:
        self.assertEqual(
            quota.format_quota(None, None, NOW, cursor=None),
            "Cx n/a | Cl n/a | Cu n/a",
        )

    def test_cursor_cache_round_trip(self) -> None:
        usage = quota.CursorUsage(window(99, WEEK), NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.CURSOR_CACHE_FILENAME
            quota.save_cursor_cache(path, usage)

            self.assertEqual(quota.load_cursor_cache(path), usage)
            self.assertEqual(
                set(json.loads(path.read_text())), {"monthly", "fetched_at"}
            )

    def test_route_picks_cursor_when_it_is_the_only_healthy_lane(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(5, WEEK),
            "grok": window(5, WEEK),
            "claude": window(5, WEEK),
            "glm": window(5, WEEK),
            "antigravity": window(5, WEEK),
            "kimi": window(5, WEEK),
            "cursor": window(99, WEEK),
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "cursor")
        self.assertIn("pick: cursor", lines[-1])

    @mock.patch("herdr_model_lanes.query_cursor")
    def test_cursor_refreshes_on_thirty_minute_cadence(
        self, query_cursor: mock.Mock
    ) -> None:
        cached = quota.CursorUsage(window(99, WEEK), NOW - 1_000)
        query_cursor.return_value = quota.CursorUsage(window(90, WEEK), NOW)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / quota.CURSOR_CACHE_FILENAME
            quota.save_cursor_cache(path, cached)
            within = quota._refresh_cursor(False, path, NOW - 1_000 + 1_500, None)
            outside = quota._refresh_cursor(False, path, NOW - 1_000 + 1_801, None)

        self.assertEqual(within[0], cached)
        self.assertEqual(outside[0].monthly.remaining_percent, 90)
        query_cursor.assert_called_once()


class RouteCommandTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes._run_herdr_command")
    @mock.patch("herdr_model_lanes.refresh")
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

    @mock.patch("herdr_model_lanes._run_herdr_command")
    @mock.patch("herdr_model_lanes.refresh")
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

    @mock.patch("herdr_model_lanes._run_herdr_command")
    @mock.patch("herdr_model_lanes.refresh")
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
            rc = quota.route_command(["medium", "--argv"], now=NOW)

        self.assertEqual(rc, 0)
        self.assertEqual(
            stdout.getvalue().strip(),
            "pi --provider openai-codex --model gpt-5.6-sol",
        )
        self.assertIn("pick: sol", stderr.getvalue())
        herdr_run.assert_not_called()

    def test_lane_command_maps_kinds_to_executables(self) -> None:
        cursor = quota.LaneSpec("cursor", "cursor", (), "cursor", True)
        agy = quota.LaneSpec("agy", "agy", (), "antigravity", False)
        unknown = quota.LaneSpec("odd", "zzz", ("--flag", "a b"), "zzz", True)

        self.assertEqual(quota.lane_command(cursor), ["cursor-agent"])
        self.assertEqual(quota.lane_command(agy), ["agy"])
        kimi = quota.LaneSpec("kimi", "kimi", (), "kimi", False)
        self.assertEqual(quota.lane_command(kimi), ["kimi"])
        self.assertEqual(quota.lane_command(unknown), ["zzz", "--flag", "a b"])

    @mock.patch("herdr_model_lanes._run_herdr_command")
    @mock.patch("herdr_model_lanes.refresh")
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
            rc = quota.route_command(["medium", "--launch"], now=NOW)

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


class StateDirTests(unittest.TestCase):
    def test_state_dir_env_fallback(self) -> None:
        with mock.patch.dict(
            "os.environ", {"MODEL_LANES_STATE_DIR": "/tmp/ml"}, clear=True
        ):
            self.assertEqual(quota.state_dir_from_env(), Path("/tmp/ml"))
        with mock.patch.dict(
            "os.environ",
            {"HERDR_PLUGIN_STATE_DIR": "/tmp/hd", "MODEL_LANES_STATE_DIR": "/tmp/ml"},
            clear=True,
        ):
            self.assertEqual(quota.state_dir_from_env(), Path("/tmp/hd"))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(quota.state_dir_from_env())


class ShortWindowSurplusTests(unittest.TestCase):
    def test_short_window_never_counts_as_surplus(self) -> None:
        spec = quota.load_class_spec("medium")
        short = quota.QuotaWindow(1, 99, NOW + 3600, window_seconds=5 * 3600)
        weekly_ok = quota.QuotaWindow(25, 75, NOW + 4 * 86400)
        quotas = {
            "codex": weekly_ok,
            "claude": None,
            "grok": None,
            "glm": short,
            "cursor": None,
        }
        lane, rationale = quota.select_lane(spec, quotas, NOW)
        self.assertEqual(lane.name, "sol")
        self.assertIn("first healthy lane in order", rationale[-1])


class ExpiredWindowTests(unittest.TestCase):
    def test_past_reset_is_unavailable_not_infinitely_healthy(self) -> None:
        spec = quota.load_class_spec("high")
        quotas = {
            "claude": quota.QuotaWindow(10, 90, NOW - 60),
            "codex": window(50, WEEK),
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "sol")
        self.assertTrue(any(line.startswith("fable: n/a") for line in lines))
        self.assertNotIn("surplus before reset", lines[-1])

    def test_expired_claude_does_not_beat_healthy_grok(self) -> None:
        spec = medium_spec()
        quotas = {
            "codex": window(50, WEEK),
            "claude": quota.QuotaWindow(10, 90, NOW - 3_600),
            "grok": window(80, WEEK),
            "glm": None,
            "cursor": None,
        }

        lane, lines = quota.select_lane(spec, quotas, NOW)

        self.assertEqual(lane.name, "grok")
        self.assertTrue(any(line.startswith("opus: n/a") for line in lines))


class CountdownTests(unittest.TestCase):
    def test_sub_hour_reset_uses_minutes(self) -> None:
        glm = quota.GlmUsage(
            quota.QuotaWindow(1, 99, NOW + 30 * 60, quota.GLM_WINDOW_SECONDS), NOW
        )

        self.assertEqual(
            quota.format_quota(None, None, NOW, glm=glm),
            "Cx n/a | Cl n/a | Gl 99% · 30m",
        )
        self.assertEqual(quota._countdown(NOW + 30 * 60, NOW), "30m")
        self.assertEqual(quota._countdown(NOW + 2 * 3_600, NOW), "2h")


class ClassifiedOverrideTests(unittest.TestCase):
    @mock.patch("herdr_model_lanes.refresh")
    def test_classified_lane_override_rejects_glm(self, refresh: mock.Mock) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=None,
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=None,
            grok_stale=False,
        )

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            rc = quota.route_command(
                ["medium", "--classified", "--argv", "--lane", "glm"]
            )

        self.assertEqual(rc, 2)
        self.assertIn("classified-ok", stderr.getvalue())


class StandaloneRouteTests(unittest.TestCase):
    def test_argv_survives_missing_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.save_codex_cache(
                root / quota.CODEX_CACHE_FILENAME,
                quota.CodexUsage(window(80, WEEK), NOW, "pro"),
            )
            with (
                mock.patch.dict(
                    "os.environ",
                    {"HERDR_PLUGIN_STATE_DIR": str(root)},
                    clear=True,
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
                mock.patch("sys.stderr", new_callable=io.StringIO),
                mock.patch("herdr_model_lanes.query_claude") as query_claude,
                mock.patch("herdr_model_lanes.query_grok") as query_grok,
                mock.patch("herdr_model_lanes.query_glm") as query_glm,
            ):
                query_claude.side_effect = quota.QuotaError("no claude")
                query_grok.side_effect = quota.QuotaError("no grok")
                query_glm.side_effect = quota.QuotaError("no glm")
                rc = quota.route_command(
                    ["medium", "--argv"],
                    herdr_bin="/no-such-herdr-binary",
                    now=NOW,
                )

        self.assertEqual(rc, 0)
        self.assertIn("pi --provider openai-codex", stdout.getvalue())


class SelectLanePropertyTests(unittest.TestCase):
    def test_seeded_windows_obey_selection_invariants(self) -> None:
        spec = medium_spec()
        names = {lane.name for lane in spec.lanes}
        rng = random.Random(20260822)
        for _ in range(200):
            quotas: dict[str, quota.QuotaWindow | None] = {}
            for key, window_seconds in (
                ("codex", WEEK),
                ("claude", WEEK),
                ("grok", WEEK),
                ("glm", quota.GLM_WINDOW_SECONDS),
                ("antigravity", WEEK),
                ("kimi", WEEK),
                ("cursor", 31 * 86_400),
            ):
                roll = rng.randrange(5)
                if roll == 0:
                    quotas[key] = None
                elif roll == 1:
                    remaining = rng.choice([0, 20, 50, 80, 100])
                    quotas[key] = quota.QuotaWindow(
                        100 - remaining,
                        remaining,
                        NOW - rng.randint(1, 10_000),
                        window_seconds,
                    )
                else:
                    remaining = rng.choice([0, 5, 10, 19, 20, 21, 50, 80, 99, 100])
                    quotas[key] = quota.QuotaWindow(
                        100 - remaining,
                        remaining,
                        NOW + rng.randint(1, WEEK),
                        window_seconds,
                    )
            classified = bool(rng.getrandbits(1))
            lane, lines = quota.select_lane(spec, quotas, NOW, classified=classified)
            self.assertIn(lane.name, names)
            if classified:
                self.assertTrue(lane.classified_ok)
            eligible = [
                item for item in spec.lanes if not classified or item.classified_ok
            ]
            if all(
                quota._lane_health(quotas.get(item.quota), NOW) is None
                for item in eligible
            ):
                self.assertEqual(lane.name, eligible[0].name)
            candidates = []
            for item in eligible:
                window = quotas.get(item.quota)
                score = quota._lane_health(window, NOW)
                if (
                    window is not None
                    and score is not None
                    and score >= 1
                    and max(0, min(100, window.remaining_percent)) >= 20
                ):
                    candidates.append(item)
            if candidates:
                picked_window = quotas.get(lane.quota)
                self.assertIsNotNone(picked_window)
                assert picked_window is not None
                self.assertGreaterEqual(
                    max(0, min(100, picked_window.remaining_percent)), 20
                )
            if "surplus before reset" in lines[-1]:
                picked_window = quotas[lane.quota]
                self.assertGreater(
                    picked_window.window_seconds, quota.SURPLUS_RESET_WINDOW_SECS
                )


class ArgparseCliTests(unittest.TestCase):
    def test_ag_help_documents_picker_and_examples(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = quota.ag_command(["--help"])

        self.assertEqual(rc, 0)
        help_text = stdout.getvalue()
        self.assertIn("Enter", help_text)
        self.assertIn("ag high", help_text)
        self.assertIn("--classified", help_text)

    def test_top_level_help_lists_subcommands(self) -> None:
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = quota.main(["--help"])

        self.assertEqual(rc, 0)
        self.assertIn("ag", stdout.getvalue())
        self.assertIn("route", stdout.getvalue())

    def test_unknown_ag_flag_is_usage_error(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            rc = quota.ag_command(["--bogus"])

        self.assertEqual(rc, 2)

    @mock.patch("herdr_model_lanes.refresh")
    def test_ag_yes_execs_the_suggested_lane(self, refresh: mock.Mock) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=quota.CodexUsage(window(80, WEEK), NOW, "pro"),
            claude=None,
            codex_stale=False,
            claude_stale=False,
        )
        executed: list[list[str]] = []

        def exec_fn(file: str, args: list[str]) -> None:
            executed.append([file, *args[1:]])

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            rc = quota.ag_command(["medium", "-y"], now=NOW, exec_fn=exec_fn)

        self.assertEqual(rc, 0)
        self.assertEqual(
            executed, [["pi", "--provider", "openai-codex", "--model", "gpt-5.6-sol"]]
        )
        self.assertIn("ag: starting sol", stderr.getvalue())

    @mock.patch("herdr_model_lanes.refresh")
    def test_ag_number_overrides_the_pick(self, refresh: mock.Mock) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=quota.CodexUsage(window(100, WEEK), NOW, "pro"),
            claude=None,
            codex_stale=False,
            claude_stale=False,
            grok=quota.GrokUsage(window(90, WEEK), NOW),
        )
        executed: list[str] = []

        rc = quota.ag_command(
            ["medium"],
            now=NOW,
            exec_fn=lambda file, args: executed.append(file),
            choice_fn=lambda _timeout: "4",
        )

        self.assertEqual(rc, 0)
        self.assertEqual(executed, ["grok"])

    @mock.patch("herdr_model_lanes.refresh")
    def test_ag_quit_does_not_exec(self, refresh: mock.Mock) -> None:
        refresh.return_value = quota.RefreshOutcome(
            codex=None,
            claude=None,
            codex_stale=False,
            claude_stale=False,
        )
        executed: list[str] = []

        rc = quota.ag_command(
            ["medium"],
            now=NOW,
            exec_fn=lambda file, args: executed.append(file),
            choice_fn=lambda _timeout: "q",
        )

        self.assertEqual(rc, 0)
        self.assertEqual(executed, [])

    def test_picker_lines_mark_the_suggestion(self) -> None:
        names, rows = quota.picker_lines(
            [
                "sol: 80% left, resets in 4d, health 1.09",
                "grok: n/a (grok quota unavailable)",
                "pick: sol (first healthy lane in order)",
            ],
            "sol",
        )

        self.assertEqual(names, ["sol", "grok"])
        self.assertTrue(rows[0].startswith("* 1) sol:"))
        self.assertTrue(rows[1].startswith("  2) grok:"))

"""Tests for the Antigravity CLI status-line quota collector."""

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest import mock

import antigravity_statusline as collector

SYNTHETIC_SESSION = "agy-secret-session"
SYNTHETIC_TRANSCRIPT = "/home/user/.antigravity/secret.jsonl"


def setUpModule() -> None:
    """Isolate the suite from the launching session's width and colour."""
    patcher = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop("COLUMNS", None)


def _bucket(
    remaining_fraction: object,
    *,
    reset_in_secs: int = 17_640,
    reset_time: str | None = None,
    **extra: object,
) -> dict:
    """Documented bucket shape: remaining fraction plus one reset source."""
    value: dict = {"remaining_fraction": remaining_fraction}
    if reset_time is not None:
        value["reset_time"] = reset_time
    else:
        value["reset_in_seconds"] = reset_in_secs
    value.update(extra)
    return value


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload(quota: object | None = None, **extra: object) -> dict:
    document = {
        "session_id": SYNTHETIC_SESSION,
        "transcript_path": SYNTHETIC_TRANSCRIPT,
        "cwd": "/home/user/secret-project",
        "agent_state": "working",
        "model": {"id": "gemini-4-neon", "display_name": "Gemini 4 Neon (XHigh)"},
        "terminal_width": 120,
        "context_window": {"current_usage": {"input_tokens": 61_000}},
        "task_count": 1,
    }
    if quota is not None:
        document["quota"] = quota
    document.update(extra)
    return document


def _live_quota() -> dict:
    return {
        "gemini-5h": _bucket(0.99),
        "gemini-weekly": _bucket(0.86, reset_in_secs=514_800),
        "3p-weekly": _bucket(0.78, reset_in_secs=514_800),
    }


class _Stdio:
    """Patch stdin/stdout/stderr with in-memory buffers."""

    def __init__(self, stdin_bytes: bytes) -> None:
        self.stdin = SimpleNamespace(buffer=io.BytesIO(stdin_bytes))
        self.out = io.TextIOWrapper(io.BytesIO())
        self.err = io.TextIOWrapper(io.BytesIO())

    def __enter__(self) -> Self:
        self._patchers = [
            mock.patch("sys.stdin", self.stdin),
            mock.patch("sys.stdout", self.out),
            mock.patch("sys.stderr", self.err),
        ]
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *_args) -> None:
        for patcher in self._patchers:
            patcher.stop()

    @property
    def stdout_text(self) -> str:
        self.out.flush()
        return self.out.buffer.getvalue().decode("utf-8")

    @property
    def stderr_text(self) -> str:
        self.err.flush()
        return self.err.buffer.getvalue().decode("utf-8")


class StatePathPrecedenceTests(unittest.TestCase):
    def _env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in ("HERDR_PLUGIN_STATE_DIR", "MODEL_LANES_STATE_DIR", "XDG_STATE_HOME")
        }
        env.update({k: v for k, v in overrides.items() if v is not None})
        return env

    def test_herdr_plugin_state_dir_wins(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR="/state/a",
                MODEL_LANES_STATE_DIR="/state/b",
                XDG_STATE_HOME="/state/c",
            ),
        ):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path("/state/a/antigravity-quota.json"),
            )

    def test_model_lanes_state_dir_then_xdg_then_local(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(MODEL_LANES_STATE_DIR="/state/b", XDG_STATE_HOME="/state/c"),
        ):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path("/state/b/antigravity-quota.json"),
            )
        with mock.patch.dict(os.environ, self._env(XDG_STATE_HOME="/state/c")):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path(
                    "/state/c/herdr/plugins/terry.herdr-model-lanes/"
                    "antigravity-quota.json"
                ),
            )
        with mock.patch.dict(os.environ, self._env()):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path(
                    os.path.join(
                        os.path.expanduser("~"),
                        ".local/state/herdr/plugins/terry.herdr-model-lanes",
                        "antigravity-quota.json",
                    )
                ),
            )


class QuotaNormalizationTests(unittest.TestCase):
    NOW = 1_795_341_005

    def _windows(self, quota: object) -> dict:
        return collector.parse_quota_document({"quota": quota}, self.NOW)

    def test_normalizes_remaining_fraction_and_reset_time(self) -> None:
        windows = self._windows(
            {"gemini-weekly": _bucket(0.86, reset_time="2026-11-23T08:48:15Z")}
        )

        self.assertEqual(
            windows["gemini-weekly"],
            {
                "remaining_fraction": 0.86,
                "used_percent": 14,
                "remaining_percent": 86,
                "resets_at": int(
                    datetime.fromisoformat("2026-11-23T08:48:15Z").timestamp()
                ),
                "window_seconds": collector.WEEKLY_WINDOW_SECONDS,
            },
        )

    def test_reset_in_seconds_is_used_when_reset_time_is_absent(self) -> None:
        windows = self._windows({"gemini-5h": _bucket(0.5, reset_in_secs=900)})

        self.assertEqual(windows["gemini-5h"]["resets_at"], self.NOW + 900)
        self.assertEqual(windows["gemini-5h"]["window_seconds"], 18_000)

    def test_reset_time_wins_over_reset_in_seconds(self) -> None:
        windows = self._windows(
            {
                "gemini-weekly": _bucket(
                    0.5, reset_time=_iso(self.NOW + 3_600), reset_in_secs=99_999
                )
            }
        )

        self.assertEqual(windows["gemini-weekly"]["resets_at"], self.NOW + 3_600)

    def test_tolerates_tiny_past_skew_but_rejects_old_resets(self) -> None:
        skew = self._windows(
            {"gemini-weekly": _bucket(0.5, reset_time=_iso(self.NOW - 30))}
        )
        stale = self._windows(
            {"gemini-weekly": _bucket(0.5, reset_time=_iso(self.NOW - 120))}
        )

        self.assertIn("gemini-weekly", skew)
        self.assertNotIn("gemini-weekly", stale)

    def test_rejects_implausible_or_malformed_buckets(self) -> None:
        cases = [
            {"remaining_fraction": 1.5, "reset_in_seconds": 900},
            {"remaining_fraction": -0.1, "reset_in_seconds": 900},
            {"remaining_fraction": "80", "reset_in_seconds": 900},
            {"remaining_fraction": True, "reset_in_seconds": 900},
            {"remaining_fraction": float("nan"), "reset_in_seconds": 900},
            {"remaining_fraction": float("inf"), "reset_in_seconds": 900},
            {"remaining_fraction": 0.5},
            {"remaining_fraction": 0.5, "reset_time": "next tuesday"},
            {"remaining_fraction": 0.5, "reset_time": 12345},
            {"remaining_fraction": 0.5, "reset_time": ""},
            {"remaining_fraction": 0.5, "reset_in_seconds": float("nan")},
            {"remaining_fraction": 0.5, "reset_in_seconds": -5_000},
            {"remaining_fraction": 0.5, "reset_in_seconds": 366 * 86_400},
            "not-a-bucket",
        ]
        for bad in cases:
            with self.subTest(bucket=bad):
                self.assertEqual(self._windows({"gemini-weekly": bad}), {})

    def test_window_seconds_follow_the_bucket_id(self) -> None:
        cases = {
            "gemini-5h": 18_000,
            "3p-5h": 18_000,
            "gemini-weekly": 604_800,
            "3p-weekly": 604_800,
            "gemini-monthly": 604_800,
        }
        for bucket_id, window_seconds in cases.items():
            with self.subTest(bucket_id=bucket_id):
                windows = self._windows({bucket_id: _bucket(0.5)})
                self.assertEqual(windows[bucket_id]["window_seconds"], window_seconds)

    def test_absent_or_non_mapping_quota_yields_no_windows(self) -> None:
        for document in ({}, {"quota": None}, {"quota": "nope"}, {"quota": []}):
            with self.subTest(document=document):
                self.assertEqual(collector.parse_quota_document(document, self.NOW), {})

    def test_non_object_payload_is_an_error(self) -> None:
        with self.assertRaisesRegex(collector.StatuslineError, "not an object"):
            collector.parse_quota_document(["nope"], self.NOW)


class TightestGeminiTests(unittest.TestCase):
    NOW = 1_795_341_005

    def _tightest(self, quota: dict) -> dict | None:
        return collector.tightest_gemini_window(
            collector.parse_quota_document({"quota": quota}, self.NOW)
        )

    def test_picks_the_smallest_remaining_gemini_bucket(self) -> None:
        window = self._tightest(
            {
                "gemini-5h": _bucket(0.80),
                "gemini-weekly": _bucket(0.42, reset_in_secs=514_800),
            }
        )

        self.assertIsNotNone(window)
        self.assertEqual(window["remaining_percent"], 42)
        self.assertEqual(window["window_seconds"], collector.WEEKLY_WINDOW_SECONDS)

    def test_breaks_ties_toward_the_longer_window(self) -> None:
        window = self._tightest(
            {
                "gemini-5h": _bucket(0.5),
                "gemini-weekly": _bucket(0.5, reset_in_secs=514_800),
            }
        )

        self.assertEqual(window["window_seconds"], collector.WEEKLY_WINDOW_SECONDS)

    def test_considers_any_gemini_prefixed_bucket(self) -> None:
        window = self._tightest(
            {
                "gemini-5h": _bucket(0.9),
                "gemini-pro-monthly": _bucket(0.2, reset_in_secs=514_800),
            }
        )

        self.assertEqual(window["remaining_percent"], 20)
        self.assertEqual(window["window_seconds"], collector.WEEKLY_WINDOW_SECONDS)

    def test_ignores_third_party_buckets(self) -> None:
        window = self._tightest(
            {
                "3p-5h": _bucket(0.01),
                "3p-weekly": _bucket(0.01, reset_in_secs=514_800),
                "gemini-weekly": _bucket(0.9, reset_in_secs=514_800),
            }
        )

        self.assertEqual(window["remaining_percent"], 90)

    def test_no_gemini_bucket_yields_none(self) -> None:
        self.assertIsNone(self._tightest({"3p-weekly": _bucket(0.5)}))
        self.assertIsNone(self._tightest({}))


class CollectorMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.cache = self.state / "antigravity-quota.json"
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in ("HERDR_PLUGIN_STATE_DIR", "MODEL_LANES_STATE_DIR", "XDG_STATE_HOME")
        }
        env["HERDR_PLUGIN_STATE_DIR"] = str(self.state)
        self._env = env
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, stdin_bytes: bytes, argv: list[str] | None = None) -> tuple:
        with _Stdio(stdin_bytes) as stdio:
            rc = collector.main(argv if argv is not None else [])
            output = stdio.stdout_text
            errors = stdio.stderr_text
        return rc, output, errors

    def test_persists_the_tightest_gemini_window_atomically_and_privately(self) -> None:
        rc, output, _ = self._run(json.dumps(_payload(_live_quota())).encode())

        self.assertEqual(rc, 0)
        self.assertIn("Neon 4 xhigh", output)
        self.assertIn("7d 14%", output)
        self.assertIn("3p 22%", output)
        self.assertTrue(self.cache.exists())
        mode = stat.S_IMODE(self.cache.stat().st_mode)
        self.assertEqual(mode, 0o600)
        cached = json.loads(self.cache.read_text())
        self.assertEqual(set(cached), {"gemini", "fetched_at"})
        self.assertEqual(
            set(cached["gemini"]),
            {"used_percent", "remaining_percent", "resets_at", "window_seconds"},
        )
        self.assertEqual(cached["gemini"]["used_percent"], 14)
        self.assertEqual(cached["gemini"]["remaining_percent"], 86)
        self.assertEqual(cached["gemini"]["window_seconds"], 604_800)
        self.assertEqual(cached["gemini"]["resets_at"], cached["fetched_at"] + 514_800)
        leftovers = [p.name for p in self.state.iterdir() if p.name != self.cache.name]
        self.assertEqual(leftovers, [])

    def test_cache_round_trips_through_model_lanes(self) -> None:
        import herdr_model_lanes

        self._run(json.dumps(_payload(_live_quota())).encode())

        usage = herdr_model_lanes.load_antigravity_cache(self.cache)
        self.assertIsNotNone(usage)
        cached = json.loads(self.cache.read_text())
        self.assertEqual(usage.gemini.used_percent, cached["gemini"]["used_percent"])
        self.assertEqual(
            usage.gemini.remaining_percent, cached["gemini"]["remaining_percent"]
        )
        self.assertEqual(usage.gemini.resets_at, cached["gemini"]["resets_at"])
        self.assertEqual(
            usage.gemini.window_seconds, cached["gemini"]["window_seconds"]
        )
        self.assertEqual(usage.fetched_at, cached["fetched_at"])

    def test_never_persists_sensitive_input_fields(self) -> None:
        rc, _, _ = self._run(json.dumps(_payload(_live_quota())).encode())

        self.assertEqual(rc, 0)
        raw = self.cache.read_text()
        for secret in (
            SYNTHETIC_SESSION,
            SYNTHETIC_TRANSCRIPT,
            "secret-project",
            "Gemini 4 Neon",
            "gemini-4-neon",
            "terminal_width",
        ):
            self.assertNotIn(secret, raw)

    def test_oversize_input_is_refused_without_persistence(self) -> None:
        rc, _, errors = self._run(b"x" * (collector.MAX_INPUT_BYTES + 1))

        self.assertEqual(rc, 1)
        self.assertIn("1 MiB", errors)
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("x" * 64, errors)
        self.assertFalse(self.cache.exists())

    def test_malformed_json_fails_safely(self) -> None:
        rc, _, errors = self._run(b"{not json")

        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("not json", errors)
        self.assertFalse(self.cache.exists())

    def test_non_object_payload_fails_safely(self) -> None:
        rc, _, errors = self._run(b"[1, 2]")

        self.assertEqual(rc, 1)
        self.assertIn("not an object", errors)
        self.assertFalse(self.cache.exists())

    def test_absent_quota_renders_na_and_keeps_prior_cache(self) -> None:
        rc, _, _ = self._run(json.dumps(_payload(_live_quota())).encode())
        self.assertEqual(rc, 0)
        before = self.cache.read_text()

        rc, output, _ = self._run(json.dumps(_payload()).encode())

        self.assertEqual(rc, 0)
        self.assertIn("5h n/a", output)
        self.assertIn("7d n/a", output)
        self.assertEqual(self.cache.read_text(), before)

    def test_invalid_buckets_never_overwrite_prior_cache(self) -> None:
        rc, _, _ = self._run(json.dumps(_payload(_live_quota())).encode())
        before = json.loads(self.cache.read_text())

        cases = [
            {"remaining_fraction": 1.5, "reset_in_seconds": 900},
            {"remaining_fraction": 0.5, "reset_in_seconds": -9_000},
            {"remaining_fraction": "0.5", "reset_in_seconds": 900},
            {"remaining_fraction": True, "reset_in_seconds": 900},
            {"remaining_fraction": float("nan"), "reset_in_seconds": 900},
            {"remaining_fraction": 0.5, "reset_time": "tomorrow"},
            {"remaining_fraction": 0.5, "reset_in_seconds": float("inf")},
            "not-a-bucket",
        ]
        for bad in cases:
            with self.subTest(bucket=bad):
                rc, output, _ = self._run(
                    json.dumps(
                        _payload({"gemini-5h": bad, "gemini-weekly": bad})
                    ).encode()
                )
                self.assertEqual(rc, 0)
                self.assertIn("n/a", output)
                self.assertEqual(json.loads(self.cache.read_text()), before)

    def test_downstream_receives_stdin_bytes_without_shell(self) -> None:
        stdin_bytes = json.dumps(_payload(_live_quota())).encode()
        argv = [
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
        ]
        with mock.patch(
            "antigravity_statusline.subprocess.run", wraps=collector.subprocess.run
        ) as run:
            rc, output, _ = self._run(stdin_bytes, argv)

        self.assertEqual(rc, 0)
        self.assertEqual(output.encode("utf-8")[::-1], stdin_bytes)
        self.assertEqual(
            run.call_args.args[0],
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
            ],
        )
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_successful_downstream_still_writes_cache(self) -> None:
        stdin_bytes = json.dumps(_payload(_live_quota())).encode()
        argv = [
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
        ]
        rc, output, _ = self._run(stdin_bytes, argv)

        self.assertEqual(rc, 0)
        self.assertEqual(output.encode("utf-8")[::-1], stdin_bytes)
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["gemini"]["used_percent"], 14)

    def test_downstream_failure_falls_back_to_rendering(self) -> None:
        argv = ["--", sys.executable, "-c", "import sys; sys.exit(3)"]
        rc, output, _ = self._run(json.dumps(_payload(_live_quota())).encode(), argv)

        self.assertEqual(rc, 0)
        self.assertIn("Neon 4 xhigh", output)

    def test_downstream_timeout_falls_back_to_rendering(self) -> None:
        argv = [
            "--",
            sys.executable,
            "-c",
            "import sys, time; time.sleep(2); sys.stdout.write('late')",
        ]
        rc, output, _ = self._run(json.dumps(_payload(_live_quota())).encode(), argv)

        self.assertEqual(rc, 0)
        self.assertIn("Neon 4 xhigh", output)
        self.assertNotIn("late", output)

    def test_cache_write_failure_warns_but_still_renders(self) -> None:
        with mock.patch.object(
            collector, "save_cache", side_effect=OSError("disk full")
        ):
            rc, output, errors = self._run(json.dumps(_payload(_live_quota())).encode())

        self.assertEqual(rc, 0)
        self.assertIn("cannot persist cache", errors)
        self.assertIn("Neon 4 xhigh", output)
        self.assertNotIn("Traceback", errors)
        self.assertFalse(self.cache.exists())

    def test_idle_session_with_quiet_transcript_renders_but_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as quiet:
            transcript = Path(quiet) / "secret.jsonl"
            transcript.write_text("{}")
            old = time.time() - 300
            os.utime(transcript, (old, old))
            payload = _payload(
                _live_quota(), agent_state="idle", transcript_path=str(transcript)
            )
            rc, output, _ = self._run(json.dumps(payload).encode())

        self.assertEqual(rc, 0)
        self.assertIn("7d 14%", output)
        self.assertFalse(self.cache.exists())

    def test_idle_session_with_recent_transcript_still_writes(self) -> None:
        with tempfile.TemporaryDirectory() as active:
            transcript = Path(active) / "secret.jsonl"
            transcript.write_text("{}")
            payload = _payload(
                _live_quota(), agent_state="idle", transcript_path=str(transcript)
            )
            rc, _, _ = self._run(json.dumps(payload).encode())

        self.assertEqual(rc, 0)
        self.assertTrue(self.cache.exists())

    def test_working_session_with_quiet_transcript_still_writes(self) -> None:
        with tempfile.TemporaryDirectory() as quiet:
            transcript = Path(quiet) / "secret.jsonl"
            transcript.write_text("{}")
            old = time.time() - 300
            os.utime(transcript, (old, old))
            payload = _payload(
                _live_quota(), agent_state="working", transcript_path=str(transcript)
            )
            rc, _, _ = self._run(json.dumps(payload).encode())

        self.assertEqual(rc, 0)
        self.assertTrue(self.cache.exists())

    def test_missing_transcript_still_writes(self) -> None:
        payload = _payload(
            _live_quota(),
            agent_state="idle",
            transcript_path="/nonexistent/launch.jsonl",
        )
        rc, _, _ = self._run(json.dumps(payload).encode())

        self.assertEqual(rc, 0)
        self.assertTrue(self.cache.exists())

    def test_arguments_before_separator_are_a_usage_error(self) -> None:
        rc, _, errors = self._run(b"{}", ["stray", "--"])

        self.assertEqual(rc, 2)
        self.assertIn("usage:", errors)
        self.assertFalse(self.cache.exists())


class RenderSegmentsTests(unittest.TestCase):
    """Model, effort, context depth, quota pools and activity counts."""

    NOW = 1_795_341_005

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": str(Path(self._tmp.name))}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _document(self, **extra: object) -> dict:
        document = {
            "model": {"id": "gemini-4-neon", "display_name": "Gemini 4 Neon (XHigh)"},
            "context_window": {
                "current_usage": {
                    "input_tokens": 118_000,
                    "cache_creation_input_tokens": 4_300,
                    "cache_read_input_tokens": 20_900,
                },
                "total_input_tokens": 40_000,
            },
            "task_count": 2,
            "subagents": [
                {"status": "running"},
                {"status": "done"},
                {"status": "running"},
            ],
        }
        document.update(extra)
        return document

    def _windows(self, **buckets: object) -> dict:
        quota = {
            "gemini-5h": _bucket(0.99),
            "gemini-weekly": _bucket(0.86, reset_in_secs=514_800),
            "3p-weekly": _bucket(0.78, reset_in_secs=514_800),
        }
        quota.update(buckets)
        return collector.parse_quota_document({"quota": quota}, self.NOW)

    def _render(
        self,
        document: object | None = None,
        windows: dict | None = None,
        **kwargs: object,
    ) -> str:
        kwargs.setdefault("color", False)
        return collector.render_status_line(
            document if document is not None else self._document(),
            windows if windows is not None else self._windows(),
            self.NOW,
            **kwargs,
        )

    def test_full_line(self) -> None:
        line = self._render()

        self.assertEqual(
            line,
            "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h)"
            " | 3p 22% | 2t 2sub",
        )
        self.assertTrue(line.isascii())

    def test_shortens_common_model_labels(self) -> None:
        cases = {
            "Gemini 4 Neon (XHigh)": ("Neon 4", "xhigh"),
            "Gemini 4 Hydrogen (High)": ("Hydro 4", "high"),
            "Gemini 4 Carbon (High)": ("Carbon 4", "high"),
            "Gemini 4 Argon (Medium)": ("Argon 4", "medium"),
            "Gemini 3.8 Flash (High)": ("Flash 3.8", "high"),
            "Orchestrator (Neon)": ("Orch Neon", None),
            "Orchestrator (Carbon)": ("Orch Carbon", None),
            "Orchestrator (Argon)": ("Orch Argon", None),
            "Claude Opus 5.5 (High)": ("Opus 5.5", "high"),
            "Claude Sonnet 5.5 (High)": ("Sonnet 5.5", "high"),
            "GPT-5 Pro (High)": ("GPT-5 Pro", "high"),
            "Gemini 4 Neon": ("Neon 4", None),
        }
        for display, (name, effort) in cases.items():
            with self.subTest(display=display):
                line = self._render(
                    self._document(
                        model={"display_name": display},
                        context_window=None,
                        task_count=0,
                        subagents=[],
                    ),
                    windows={},
                )
                head = name if effort is None else f"{name} {effort}"
                labels = (
                    ("3p5h n/a", "3p7d n/a")
                    if any(m in display.lower() for m in ("claude", "gpt"))
                    else ("5h n/a", "7d n/a")
                )
                self.assertEqual(line, " | ".join((head, *labels)))

    def test_model_effort_sources_and_fallbacks(self) -> None:
        cases = [
            (
                {"display_name": "Gemini 4 Neon (XHigh)", "effort": "high"},
                "Neon 4 high",
            ),
            (
                {"display_name": "Gemini 4 Neon (XHigh)", "effort": "x high; rm"},
                "Neon 4 xhigh",
            ),
            ({"display_name": "Gemini 4 Neon (XHigh)", "effort": 3}, "Neon 4 xhigh"),
            ({"id": "gemini-4-neon"}, "gemini-4-neon"),
            ({"display_name": "", "id": "gemini-4-carbon"}, "gemini-4-carbon"),
            ({}, "Gemini"),
            ("Opus 4.5", "Gemini"),
        ]
        for model, head in cases:
            with self.subTest(model=model):
                line = self._render(
                    self._document(
                        model=model, context_window=None, task_count=0, subagents=[]
                    ),
                    windows={},
                )
                self.assertEqual(line, f"{head} | 5h n/a | 7d n/a")

    def test_model_id_only_keeps_the_3p_pool_for_claude_ids(self) -> None:
        line = self._render(
            self._document(
                model={"id": "claude-opus-5-5"},
                context_window=None,
                task_count=0,
                subagents=[],
            ),
            windows={},
        )
        self.assertEqual(line, "claude-opus-5-5 | 3p5h n/a | 3p7d n/a")

    def test_third_party_model_reads_the_3p_pool_with_a_gemini_summary(self) -> None:
        document = self._document(
            model={"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5 (High)"},
            context_window={"total_input_tokens": 61_000},
            task_count=1,
            subagents=[],
        )
        windows = collector.parse_quota_document(
            {
                "quota": {
                    "3p-5h": _bucket(0.97),
                    "3p-weekly": _bucket(0.86, reset_in_secs=514_800),
                    "gemini-weekly": _bucket(0.86, reset_in_secs=514_800),
                }
            },
            self.NOW,
        )

        line = self._render(document, windows)

        self.assertEqual(
            line,
            "Opus 5.5 high | ctx 61K | 3p5h 3% (4h54m) | 3p7d 14% (5d23h) | G 14% | 1t",
        )

    def test_a_gpt_model_id_also_switches_to_the_3p_pool(self) -> None:
        line = self._render(
            self._document(
                model={"id": "gpt-5-pro"},
                context_window=None,
                task_count=0,
                subagents=[],
            ),
            windows={},
        )

        self.assertEqual(line, "gpt-5-pro | 3p5h n/a | 3p7d n/a")

    def test_gemini_summary_of_an_unused_3p_pool_is_omitted(self) -> None:
        windows = self._windows(**{"3p-weekly": _bucket(1.0, reset_in_secs=514_800)})

        line = self._render(windows=windows)

        self.assertNotIn("3p 0%", line)
        self.assertEqual(
            line,
            "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h) | 2t 2sub",
        )

    def test_gemini_summary_shows_even_at_zero_use(self) -> None:
        document = self._document(
            model={"display_name": "Claude Opus 5.5 (High)"}, task_count=0, subagents=[]
        )
        windows = collector.parse_quota_document(
            {
                "quota": {
                    "3p-5h": _bucket(0.97),
                    "3p-weekly": _bucket(0.86, reset_in_secs=514_800),
                    "gemini-weekly": _bucket(1.0, reset_in_secs=514_800),
                }
            },
            self.NOW,
        )

        line = self._render(document, windows)

        self.assertIn("G 0%", line)
        self.assertTrue(line.endswith("G 0%"))

    def test_countdown_formats_match_the_claude_collector(self) -> None:
        cases = {
            0: "(0s)",
            45: "(45s)",
            720: "(12m)",
            17_640: "(4h54m)",
            514_800: "(5d23h)",
        }
        for offset, expected in cases.items():
            with self.subTest(offset=offset):
                windows = self._windows(
                    **{"gemini-5h": _bucket(0.99, reset_in_secs=offset)}
                )
                line = self._render(windows=windows)
                self.assertIn(f"5h 1% {expected}", line)

    def test_readings_near_a_limit_are_coloured(self) -> None:
        warn, high, reset = (
            collector.ANSI_WARN,
            collector.ANSI_HIGH,
            collector.ANSI_RESET,
        )
        document = self._document(
            model={"display_name": "Gemini 4 Argon (Medium)"},
            context_window={"current_usage": {"input_tokens": 143_200}},
        )
        windows = self._windows(
            **{
                "gemini-5h": _bucket(0.30),
                "gemini-weekly": _bucket(0.10, reset_in_secs=514_800),
                "3p-weekly": _bucket(0.05, reset_in_secs=514_800),
            }
        )

        line = self._render(document, windows, color=True)

        self.assertEqual(
            line,
            f"Argon 4 medium | ctx 143K | {warn}5h 70% (4h54m){reset}"
            f" | {high}7d 90% (5d23h){reset} | {high}3p 95%{reset} | 2t 2sub",
        )

    def test_calm_readings_carry_no_escape_codes_even_with_colour_on(self) -> None:
        document = self._document(model={"display_name": "Gemini 4 Argon (Medium)"})
        self.assertNotIn("\033", self._render(document, color=True))

    def test_no_color_environment_variable_disables_colour(self) -> None:
        document = self._document(
            context_window={"current_usage": {"input_tokens": 250_000}}
        )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            plain = self._render(document, color=None)
        environment = {
            key: value for key, value in os.environ.items() if key != "NO_COLOR"
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            coloured = self._render(document, color=None)
        self.assertNotIn("\033", plain)
        self.assertIn(f"{collector.ANSI_WARN}ctx 250K", coloured)

    def test_costly_effort_is_flagged_and_never_shed(self) -> None:
        warn, reset = collector.ANSI_WARN, collector.ANSI_RESET
        line = self._render(color=True)
        self.assertIn(f"{warn}xhigh{reset}", line)
        self.assertEqual(self._render(columns=20), "xhigh 143K 5h 1% 7d 14% 2t 2sub")

    def test_context_prefers_current_usage_and_falls_back(self) -> None:
        self.assertIn("ctx 143K", self._render())
        line = self._render(
            self._document(context_window={"total_input_tokens": 61_000})
        )
        self.assertIn("ctx 61K", line)
        line = self._render(
            self._document(
                context_window={
                    "current_usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "total_input_tokens": 61_000,
                }
            )
        )
        self.assertIn("ctx 61K", line)

    def test_invalid_or_absent_context_is_omitted(self) -> None:
        expected = "Neon 4 xhigh | 5h 1% (4h54m) | 7d 14% (5d23h) | 3p 22% | 2t 2sub"
        documents = [
            self._document(context_window=None),
            self._document(context_window={}),
            self._document(context_window={"current_usage": {}}),
            self._document(
                context_window={
                    "current_usage": {
                        "input_tokens": "61k",
                        "cache_read_input_tokens": True,
                    },
                    "total_input_tokens": float("nan"),
                }
            ),
            self._document(context_window={"total_input_tokens": 0}),
            self._document(context_window={"total_input_tokens": "61000"}),
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(self._render(document), expected)

    def test_context_depth_formats_thousands_and_millions(self) -> None:
        cases = {
            400: "ctx 0K",
            1_499: "ctx 1K",
            999_499: "ctx 999K",
            999_500: "ctx 1.0M",
            1_240_000: "ctx 1.2M",
        }
        for tokens, expected in cases.items():
            with self.subTest(tokens=tokens):
                line = self._render(
                    self._document(
                        context_window={"current_usage": {"input_tokens": tokens}}
                    )
                )
                self.assertIn(f"| {expected} |", line)

    def test_context_depth_is_coloured_at_absolute_thresholds(self) -> None:
        warn, high, reset = (
            collector.ANSI_WARN,
            collector.ANSI_HIGH,
            collector.ANSI_RESET,
        )
        cases = {
            199_999: None,
            200_000: warn,
            299_999: warn,
            300_000: high,
        }
        for tokens, level in cases.items():
            with self.subTest(tokens=tokens):
                line = self._render(
                    self._document(
                        context_window={"current_usage": {"input_tokens": tokens}}
                    ),
                    color=True,
                )
                amount = collector._context_amount(tokens)
                if level is None:
                    self.assertNotIn(f"\033{amount}", line)
                    self.assertIn(f"ctx {amount}", line)
                else:
                    self.assertIn(f"{level}ctx {amount}{reset}", line)

    def test_task_and_subagent_counts(self) -> None:
        cases = [
            ((3, [{"status": "running"}, {"status": "done"}]), "3t 1sub"),
            ((1, [{"status": "done"}, "running"]), "1t"),
            ((0, []), "3p 22%"),
            ((None, None), "3p 22%"),
            (("2", [{"status": "running"}]), "1sub"),
            ((True, None), "3p 22%"),
        ]
        for (task_count, subagents), expected_tail in cases:
            with self.subTest(task_count=task_count, subagents=subagents):
                line = self._render(
                    self._document(task_count=task_count, subagents=subagents)
                )
                _, _, tail = line.rpartition(" | ")
                self.assertEqual(tail, expected_tail)

    def test_detail_is_shed_in_order_as_the_terminal_narrows(self) -> None:
        # Usable width is columns minus the four-column margin.
        cases = {
            None: "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h)"
            " | 3p 22% | 2t 2sub",
            120: "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h)"
            " | 3p 22% | 2t 2sub",
            79: "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h)"
            " | 3p 22% | 2t 2sub",
            78: "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% (5d23h) | 2t 2sub",
            69: "Neon 4 xhigh | ctx 143K | 5h 1% (4h54m) | 7d 14% | 2t 2sub",
            61: "Neon 4 xhigh | 143K | 5h 1% (4h54m) | 7d 14% | 2t 2sub",
            57: "Neon 4 xhigh | 143K | 5h 1% | 7d 14% | 2t 2sub",
            49: "Neon 4 xhigh 143K 5h 1% 7d 14% 2t 2sub",
            41: "xhigh 143K 5h 1% 7d 14% 2t 2sub",
            40: "xhigh 143K 5h 1% 7d 14% 2t 2sub",
            30: "xhigh 143K 5h 1% 7d 14% 2t 2sub",
        }
        for columns, expected in cases.items():
            with self.subTest(columns=columns):
                line = self._render(columns=columns)
                self.assertEqual(line, expected)
                if columns is not None and columns >= 40:
                    self.assertLessEqual(
                        len(line), columns - collector.STATUS_LINE_MARGIN_COLUMNS
                    )

    def test_plain_effort_is_shed_before_the_model(self) -> None:
        document = self._document(model={"display_name": "Gemini 4 Argon (Medium)"})

        self.assertEqual(
            self._render(document, columns=42), "Argon 4 143K 5h 1% 7d 14% 2t 2sub"
        )
        self.assertEqual(
            self._render(document, columns=36), "143K 5h 1% 7d 14% 2t 2sub"
        )

    def test_columns_come_from_the_payload_before_the_environment(self) -> None:
        expected = "Neon 4 xhigh | 143K | 5h 1% (4h54m) | 7d 14% | 2t 2sub"
        with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
            self.assertEqual(self._render(self._document(terminal_width=61)), expected)
        for bad in ("wide", 0, 9, 2001, 12.5, True):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"COLUMNS": "61"}):
                self.assertEqual(
                    self._render(self._document(terminal_width=bad)), expected
                )
        environment = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertIn("(5d23h)", self._render(self._document(), color=False))

    def test_colour_codes_do_not_count_towards_the_width(self) -> None:
        document = self._document(
            context_window={"current_usage": {"input_tokens": 212_000}}
        )
        plain = self._render(document, columns=80)
        coloured = self._render(document, columns=80, color=True)
        self.assertEqual(
            plain,
            "Neon 4 xhigh | ctx 212K | 5h 1% (4h54m) | 7d 14% (5d23h)"
            " | 3p 22% | 2t 2sub",
        )
        self.assertEqual(
            coloured.replace(collector.ANSI_WARN, "").replace(collector.ANSI_RESET, ""),
            plain,
        )


if __name__ == "__main__":
    unittest.main()

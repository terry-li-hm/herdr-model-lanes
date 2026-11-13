"""Tests for the Claude Code status-line quota collector."""

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest import mock

import claude_statusline as collector


def setUpModule() -> None:
    """Isolate the suite from the launching session's profile and colour setting.

    A session started under a non-default ``CLAUDE_CONFIG_DIR`` would otherwise
    key every cache path under ``profiles/`` and add an account label.
    """
    patcher = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ.pop("COLUMNS", None)


def _payload(
    five_hour: object | None = None,
    seven_day: object | None = None,
    **extra: object,
) -> dict:
    document = {
        "session_id": "0193-secret-session",
        "transcript_path": "/home/user/.claude/projects/secret.jsonl",
        "cwd": "/home/user/secret-project",
        "workspace": {"current_dir": "/home/user/secret-project", "project_dir": "/"},
        "version": "2.0.0",
        "output_style": {"name": "default"},
        "cost": {"total_cost_usd": 1.25, "total_duration_ms": 9000},
        "exceeds_200k_tokens": False,
        "model": {"id": "claude-sonnet-4-5", "display_name": "Opus 4.5"},
    }
    if five_hour is not None or seven_day is not None:
        document["rate_limits"] = {}
        if five_hour is not None:
            document["rate_limits"]["five_hour"] = five_hour
        if seven_day is not None:
            document["rate_limits"]["seven_day"] = seven_day
    document.update(extra)
    return document


def _live_window(used_percentage: object, resets_in_secs: int = 3600) -> dict:
    """Documented status-line window shape: used_percentage plus reset epoch."""
    return {
        "used_percentage": used_percentage,
        "resets_at": int(time.time()) + resets_in_secs,
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
    def stdout_bytes(self) -> bytes:
        self.out.flush()
        return self.out.buffer.getvalue()

    @property
    def stderr_text(self) -> str:
        self.err.flush()
        return self.err.buffer.getvalue().decode("utf-8")

    @property
    def stdout_text(self) -> str:
        return self.stdout_bytes.decode("utf-8")


class StatePathPrecedenceTests(unittest.TestCase):
    def _env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "HERDR_PLUGIN_STATE_DIR",
                "MODEL_LANES_STATE_DIR",
                "XDG_STATE_HOME",
                "CLAUDE_CONFIG_DIR",
            )
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
                Path("/state/a/claude-statusline.json"),
            )

    def test_model_lanes_state_dir_then_xdg_then_local(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(MODEL_LANES_STATE_DIR="/state/b", XDG_STATE_HOME="/state/c"),
        ):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path("/state/b/claude-statusline.json"),
            )
        with mock.patch.dict(os.environ, self._env(XDG_STATE_HOME="/state/c")):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path(
                    "/state/c/herdr/plugins/terry.herdr-model-lanes/claude-statusline.json"
                ),
            )
        with mock.patch.dict(os.environ, self._env()):
            self.assertEqual(
                collector.statusline_cache_path(),
                Path(
                    os.path.join(
                        os.path.expanduser("~"),
                        ".local/state/herdr/plugins/terry.herdr-model-lanes",
                        "claude-statusline.json",
                    )
                ),
            )


class CollectorMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.cache = self.state / "claude-statusline.json"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("HERDR_PLUGIN_STATE_DIR", "CLAUDE_CONFIG_DIR")
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

    def test_persists_normalized_windows_atomically_and_privately(self) -> None:
        rc, output, _ = self._run(
            json.dumps(
                _payload(
                    five_hour=_live_window(42.5),
                    seven_day=_live_window(80, resets_in_secs=86400),
                )
            ).encode()
        )

        self.assertEqual(rc, 0)
        self.assertIn("Opus 4.5", output)
        self.assertIn("5h 42.5%", output)
        self.assertIn("7d 80%", output)
        self.assertTrue(self.cache.exists())
        mode = stat.S_IMODE(self.cache.stat().st_mode)
        self.assertEqual(mode, 0o600)
        cached = json.loads(self.cache.read_text())
        self.assertEqual(set(cached), {"captured_at", "five_hour", "seven_day"})
        for window in (cached["five_hour"], cached["seven_day"]):
            self.assertEqual(set(window), {"utilization", "resets_at"})
        self.assertEqual(cached["five_hour"]["utilization"], 42.5)
        self.assertEqual(cached["seven_day"]["utilization"], 80)
        leftovers = [p.name for p in self.state.iterdir() if p.name != self.cache.name]
        self.assertEqual(leftovers, [])

    def test_never_persists_sensitive_input_fields(self) -> None:
        rc, _, _ = self._run(
            json.dumps(
                _payload(five_hour=_live_window(10), seven_day=_live_window(20))
            ).encode()
        )
        self.assertEqual(rc, 0)
        raw = self.cache.read_text()
        for secret in (
            "0193-secret-session",
            "secret-project",
            "secret.jsonl",
            "Opus 4.5",
            "claude-sonnet-4-5",
            "total_cost_usd",
        ):
            self.assertNotIn(secret, raw)

    def test_oversize_input_is_refused_without_persistence(self) -> None:
        rc, _, errors = self._run(b"x" * (collector.MAX_INPUT_BYTES + 1))

        self.assertEqual(rc, 1)
        self.assertIn("1 MiB", errors)
        self.assertNotIn("Traceback", errors)
        self.assertFalse(self.cache.exists())

    def test_malformed_json_fails_safely(self) -> None:
        rc, _, errors = self._run(b"{not json")

        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", errors)
        self.assertFalse(self.cache.exists())

    def test_absent_rate_limits_renders_na_and_keeps_prior_cache(self) -> None:
        rc, _, _ = self._run(
            json.dumps(
                _payload(five_hour=_live_window(10), seven_day=_live_window(20))
            ).encode()
        )
        self.assertEqual(rc, 0)
        before = self.cache.read_text()

        rc, output, _ = self._run(json.dumps(_payload()).encode())

        self.assertEqual(rc, 0)
        self.assertIn("5h n/a", output)
        self.assertIn("7d n/a", output)
        self.assertEqual(self.cache.read_text(), before)

    def test_no_valid_live_window_never_overwrites_prior_cache(self) -> None:
        rc, _, _ = self._run(
            json.dumps(
                _payload(five_hour=_live_window(10), seven_day=_live_window(20))
            ).encode()
        )
        before = json.loads(self.cache.read_text())

        cases = [
            {"used_percentage": 42, "resets_at": int(time.time()) - 10},
            {"used_percentage": 150, "resets_at": int(time.time()) + 3600},
            {"used_percentage": "42", "resets_at": int(time.time()) + 3600},
            {"used_percentage": 42, "resets_at": "tomorrow"},
            {"used_percentage": float("nan"), "resets_at": int(time.time()) + 3600},
            {"used_percentage": float("inf"), "resets_at": int(time.time()) + 3600},
            {"used_percentage": 42, "resets_at": float("nan")},
            {"used_percentage": 42, "resets_at": float("inf")},
            {"used_percentage": 42, "resets_at": 10**30},
        ]
        for bad in cases:
            with self.subTest(window=bad):
                rc, output, _ = self._run(
                    json.dumps(_payload(five_hour=bad, seven_day=bad)).encode()
                )
                self.assertEqual(rc, 0)
                self.assertIn("n/a", output)
                self.assertEqual(json.loads(self.cache.read_text()), before)

    def test_invalid_five_hour_alone_keeps_valid_seven_day(self) -> None:
        rc, _, _ = self._run(
            json.dumps(
                _payload(five_hour={"used_percentage": 999}, seven_day=_live_window(30))
            ).encode()
        )
        self.assertEqual(rc, 0)
        cached = json.loads(self.cache.read_text())
        self.assertIsNone(cached["five_hour"])
        self.assertEqual(cached["seven_day"]["utilization"], 30)

    def test_downstream_receives_stdin_bytes_without_shell(self) -> None:
        stdin_bytes = json.dumps(
            _payload(five_hour=_live_window(1), seven_day=_live_window(2))
        ).encode()
        argv = [
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
        ]
        with mock.patch(
            "claude_statusline.subprocess.run", wraps=collector.subprocess.run
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
        stdin_bytes = json.dumps(
            _payload(five_hour=_live_window(11), seven_day=_live_window(22))
        ).encode()
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
        self.assertEqual(cached["five_hour"]["utilization"], 11)
        self.assertEqual(cached["seven_day"]["utilization"], 22)

    def test_five_hour_only_event_preserves_prior_cache(self) -> None:
        rc, _, _ = self._run(
            json.dumps(
                _payload(five_hour=_live_window(10), seven_day=_live_window(20))
            ).encode()
        )
        self.assertEqual(rc, 0)
        before = self.cache.read_text()

        rc, output, _ = self._run(
            json.dumps(_payload(five_hour=_live_window(55))).encode()
        )

        self.assertEqual(rc, 0)
        self.assertIn("7d n/a", output)
        self.assertEqual(self.cache.read_text(), before)

    def test_cache_write_failure_warns_but_still_renders(self) -> None:
        payload = json.dumps(
            _payload(five_hour=_live_window(10), seven_day=_live_window(20))
        ).encode()
        with mock.patch(
            "claude_statusline.save_cache",
            side_effect=OSError("disk failure at /secret/state/path"),
        ):
            rc, output, errors = self._run(payload)

        self.assertEqual(rc, 0)
        self.assertIn("5h 10%", output)
        self.assertIn("7d 20%", output)
        self.assertEqual(
            errors.strip(), "claude statusline warning: cannot persist cache"
        )
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("/secret/state/path", errors)
        self.assertFalse(self.cache.exists())

    def test_cache_write_failure_still_forwards_byte_exact_stdin(self) -> None:
        stdin_bytes = json.dumps(
            _payload(five_hour=_live_window(1), seven_day=_live_window(2))
        ).encode()
        argv = [
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
        ]
        with mock.patch(
            "claude_statusline.save_cache",
            side_effect=OSError("disk failure at /secret/state/path"),
        ):
            rc, output, errors = self._run(stdin_bytes, argv)

        self.assertEqual(rc, 0)
        self.assertEqual(output.encode("utf-8")[::-1], stdin_bytes)
        self.assertEqual(
            errors.strip(), "claude statusline warning: cannot persist cache"
        )
        self.assertFalse(self.cache.exists())

    def _transcript(self, age_secs: int) -> str:
        transcript = self.state / "session.jsonl"
        transcript.write_text("{}\n")
        stamp = time.time() - age_secs
        os.utime(transcript, (stamp, stamp))
        return str(transcript)

    def test_idle_session_renders_but_never_overwrites_the_cache(self) -> None:
        active = _payload(
            five_hour=_live_window(45),
            seven_day=_live_window(30),
            transcript_path=self._transcript(age_secs=5),
        )
        rc, _, _ = self._run(json.dumps(active).encode())
        self.assertEqual(rc, 0)
        before = self.cache.read_text()

        idle = _payload(
            five_hour=_live_window(10),
            seven_day=_live_window(20),
            transcript_path=self._transcript(
                age_secs=collector.ACTIVE_TRANSCRIPT_SECS + 30
            ),
        )
        rc, output, errors = self._run(json.dumps(idle).encode())

        self.assertEqual(rc, 0)
        self.assertIn("5h 10%", output)
        self.assertIn("7d 20%", output)
        self.assertEqual(errors, "")
        self.assertEqual(self.cache.read_text(), before)

    def test_recently_written_transcript_still_writes_the_cache(self) -> None:
        payload = _payload(
            five_hour=_live_window(45),
            seven_day=_live_window(30),
            transcript_path=self._transcript(age_secs=collector.ACTIVE_TRANSCRIPT_SECS - 20),
        )
        rc, _, _ = self._run(json.dumps(payload).encode())

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.cache.read_text())["seven_day"]["utilization"], 30)

    def test_unprompted_session_is_current_only_for_its_first_minute(self) -> None:
        now = int(time.time())
        missing = str(self.state / "not-yet-written.jsonl")
        limit_ms = collector.ACTIVE_TRANSCRIPT_SECS * 1000
        cases = {
            limit_ms: True,
            limit_ms + 1: False,
            None: True,
            "9000": True,
            float("nan"): True,
        }
        for running_ms, expected in cases.items():
            with self.subTest(running_ms=running_ms):
                document = {"transcript_path": missing, "cost": {"total_duration_ms": running_ms}}
                self.assertIs(collector.windows_are_current(document, now), expected)
        self.assertTrue(collector.windows_are_current({"transcript_path": missing}, now))

        idle = _payload(
            five_hour=_live_window(10),
            seven_day=_live_window(20),
            transcript_path=missing,
            cost={"total_duration_ms": limit_ms * 30},
        )
        rc, output, _ = self._run(json.dumps(idle).encode())
        self.assertEqual(rc, 0)
        self.assertIn("7d 20%", output)
        self.assertFalse(self.cache.exists())

    def test_unusable_transcript_path_counts_as_current(self) -> None:
        now = int(time.time())
        for transcript in (None, "", 7, "/nonexistent/session.jsonl", "bad\0path"):
            with self.subTest(transcript=transcript):
                self.assertTrue(
                    collector.windows_are_current({"transcript_path": transcript}, now)
                )
        self.assertTrue(collector.windows_are_current([], now))

    def test_arguments_before_separator_are_a_usage_error(self) -> None:
        rc, _, errors = self._run(b"{}", ["--explain"])

        self.assertEqual(rc, 2)
        self.assertIn("usage:", errors)
        self.assertNotIn("Traceback", errors)

    def test_downstream_failure_falls_back_to_rendering(self) -> None:
        rc, output, _ = self._run(
            b"{}",
            ["--", sys.executable, "-c", "import sys; sys.exit(3)"],
        )

        self.assertEqual(rc, 0)
        self.assertIn("Claude", output)
        self.assertIn("n/a", output)

    def test_downstream_timeout_falls_back_to_rendering(self) -> None:

        rc, output, _ = self._run(
            b"{}",
            ["--", sys.executable, "-c", "import time; time.sleep(30)"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("Claude", output)
        self.assertLessEqual(collector.DOWNSTREAM_TIMEOUT_SECS, 10)


if __name__ == "__main__":
    unittest.main()


class ScopedLimitRenderTests(unittest.TestCase):
    def test_renders_scoped_limit_from_cache(self) -> None:
        now = 1788950000
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claude-scoped-limit.json").write_text(json.dumps(
                {"scope": "Fable", "utilization": 76.0, "resets_at": 1789290000, "fetched_at": now - 60}))
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
                line = collector.render_status_line(
                    {"model": {"display_name": "Fable 5.1"}},
                    {"utilization": 12, "resets_at": now + 6000},
                    {"utilization": 38, "resets_at": now + 340000},
                    now,
                )
        self.assertEqual(line, "Fable 5.1 | 5h 12% 1h40m | 7d 38% | Fable 76% 3d22h")

    def test_stale_or_reset_scoped_cache_is_ignored(self) -> None:
        now = 1788950000
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claude-scoped-limit.json").write_text(json.dumps(
                {"scope": "Fable", "utilization": 76.0, "resets_at": now - 1, "fetched_at": now - 60}))
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmp}):
                line = collector.render_status_line({"model": "Fable"}, None, None, now)
        self.assertEqual(line, "Fable | 5h n/a | 7d n/a")


class RenderSegmentsTests(unittest.TestCase):
    """Model, effort, context depth, quota windows and account label."""

    NOW = 1788950000

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _document(self, **extra: object) -> dict:
        document = {
            "model": {"display_name": "Fable 5.2"},
            "effort": {"level": "xhigh"},
            "context_window": {"total_input_tokens": 143_200, "total_output_tokens": 900},
        }
        document.update(extra)
        return document

    def _render(self, document: object, **kwargs: object) -> str:
        five_hour = kwargs.pop("five_hour", {"utilization": 40, "resets_at": self.NOW + 7080})
        seven_day = kwargs.pop("seven_day", {"utilization": 27, "resets_at": self.NOW + 176400})
        kwargs.setdefault("color", False)
        return collector.render_status_line(document, five_hour, seven_day, self.NOW, **kwargs)

    def _write_scoped(self, directory: Path, resets_at: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "claude-scoped-limit.json").write_text(
            json.dumps(
                {
                    "scope": "Fable",
                    "utilization": 45.0,
                    "resets_at": resets_at,
                    "fetched_at": self.NOW - 60,
                }
            )
        )

    def test_full_line_for_a_numbered_account_profile(self) -> None:
        config_dir = os.path.join(os.path.expanduser("~"), ".claude-account6")
        self._write_scoped(self.state / "profiles" / ".claude-account6", self.NOW + 176400)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config_dir}):
            line = self._render(self._document())
        self.assertEqual(
            line,
            "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% | Fable 45% 2d1h | a6",
        )
        self.assertTrue(line.isascii())

    def test_seven_day_keeps_countdown_when_scoped_limit_resets_separately(self) -> None:
        self._write_scoped(self.state, self.NOW + 90000)
        line = self._render(self._document())
        self.assertEqual(
            line, "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% 2d1h | Fable 45% 1d1h"
        )

    def test_absent_or_invalid_context_and_effort_are_omitted(self) -> None:
        expected = "Fable 5.2 | 5h 40% 1h58m | 7d 27% 2d1h"
        documents = [
            {"model": {"display_name": "Fable 5.2"}},
            self._document(effort=None, context_window=None),
            self._document(effort={"level": "x high; rm"}, context_window={}),
            self._document(effort={"level": 3}, context_window={"total_input_tokens": 0}),
            self._document(effort="max", context_window={"total_input_tokens": float("nan")}),
            self._document(effort={}, context_window={"total_input_tokens": "143200"}),
            self._document(effort={"level": ""}, context_window={"total_input_tokens": True}),
        ]
        for document in documents:
            with self.subTest(document=document):
                self.assertEqual(self._render(document), expected)

    def test_context_depth_formats_thousands_and_millions(self) -> None:
        cases = {400: "ctx 0K", 1_499: "ctx 1K", 199_600: "ctx 200K", 1_240_000: "ctx 1.2M"}
        for tokens, expected in cases.items():
            with self.subTest(tokens=tokens):
                line = self._render(
                    self._document(context_window={"total_input_tokens": tokens})
                )
                self.assertIn(f"| {expected} |", line)

    def test_readings_near_a_limit_are_coloured(self) -> None:
        warn, high, reset = collector.ANSI_WARN, collector.ANSI_HIGH, collector.ANSI_RESET
        line = self._render(
            self._document(
                effort={"level": "max"}, context_window={"total_input_tokens": 150_000}
            ),
            five_hour={"utilization": 90, "resets_at": self.NOW + 60},
            seven_day={"utilization": 70, "resets_at": self.NOW + 3600},
            color=True,
        )
        self.assertEqual(
            line,
            f"Fable 5.2 {warn}max{reset} | {warn}ctx 150K{reset}"
            f" | {high}5h 90% 1m{reset} | {warn}7d 70% 1h00m{reset}",
        )
        deep = self._render(
            self._document(context_window={"total_input_tokens": 200_000}), color=True
        )
        self.assertIn(f"{high}ctx 200K{reset}", deep)

    def test_calm_readings_carry_no_escape_codes_even_with_colour_on(self) -> None:
        self.assertNotIn("\033", self._render(self._document(), color=True))

    def test_no_color_environment_variable_disables_colour(self) -> None:
        document = self._document(context_window={"total_input_tokens": 250_000})
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            plain = self._render(document, color=None)
        environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with mock.patch.dict(os.environ, environment, clear=True):
            coloured = self._render(document, color=None)
        self.assertNotIn("\033", plain)
        self.assertIn(f"{collector.ANSI_HIGH}ctx 250K", coloured)

    def _render_at(self, columns: int | None, **kwargs: object) -> str:
        config_dir = os.path.join(os.path.expanduser("~"), ".claude-account6")
        self._write_scoped(self.state / "profiles" / ".claude-account6", self.NOW + 176400)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": config_dir}):
            return self._render(kwargs.pop("document", self._document()), columns=columns, **kwargs)

    def test_detail_is_shed_in_order_as_the_terminal_narrows(self) -> None:
        # Usable width is columns minus the four-column margin.
        cases = {
            None: "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% | Fable 45% 2d1h | a6",
            76: "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% | Fable 45% 2d1h | a6",
            75: "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% | Fable 45% 2d1h",
            70: "Fable 5.2 xhigh | ctx 143K | 5h 40% 1h58m | 7d 27% | Fable 45%",
            65: "Fable 5.2 xhigh | 143K | 5h 40% 1h58m | 7d 27% | Fable 45%",
            61: "Fable 5.2 xhigh | 143K | 5h 40% | 7d 27% | Fable 45%",
            55: "Fable 5.2 xhigh 143K 5h 40% 7d 27% Fable 45%",
            47: "Fable 5.2 143K 5h 40% 7d 27% Fable 45%",
            41: "143K 5h 40% 7d 27% Fable 45%",
            12: "143K 5h 40% 7d 27% Fable 45%",
        }
        for columns, expected in cases.items():
            with self.subTest(columns=columns):
                line = self._render_at(columns)
                self.assertEqual(line, expected)
                if columns is not None and columns >= 32:
                    self.assertLessEqual(
                        len(line), columns - collector.STATUS_LINE_MARGIN_COLUMNS
                    )

    def test_colour_codes_do_not_count_towards_the_width(self) -> None:
        document = self._document(context_window={"total_input_tokens": 212_000})
        plain = self._render_at(76, document=document)
        coloured = self._render_at(76, document=document, color=True)
        self.assertEqual(
            plain, "Fable 5.2 xhigh | ctx 212K | 5h 40% 1h58m | 7d 27% | Fable 45% 2d1h | a6"
        )
        self.assertEqual(
            coloured.replace(collector.ANSI_HIGH, "").replace(collector.ANSI_RESET, ""), plain
        )

    def test_costly_effort_survives_the_narrowest_terminal(self) -> None:
        line = self._render_at(20, document=self._document(effort={"level": "max"}))
        self.assertEqual(line, "max 143K 5h 40% 7d 27% Fable 45%")

    def test_columns_come_from_the_environment_and_bad_values_are_ignored(self) -> None:
        full = self._render_at(None)
        for raw, expected in {
            "61": "Fable 5.2 xhigh | 143K | 5h 40% | 7d 27% | Fable 45%",
            "": full,
            "0": full,
            "-5": full,
            "wide": full,
            "99999": full,
        }.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"COLUMNS": raw}):
                self.assertEqual(self._render_at(None), expected)

    def test_account_label_only_for_a_non_default_profile(self) -> None:
        home = os.path.expanduser("~")
        cases = {
            "": None,
            os.path.join(home, ".claude"): None,
            os.path.join(home, ".claude-account2"): "a2",
            os.path.join(home, ".claude-account12"): "a12",
            os.path.join(home, ".claude-work"): "work",
            "/opt/My Profile!": "MyProfile",
            "/srv/a-very-long-profile-directory-name": "a-very-long-",
        }
        for config_dir, expected in cases.items():
            with self.subTest(config_dir=config_dir):
                self.assertEqual(collector._account_label(config_dir), expected)

    def test_main_prints_context_and_effort_from_stdin(self) -> None:
        payload = _payload(
            five_hour=_live_window(10),
            seven_day=_live_window(20),
            effort={"level": "high"},
            context_window={"total_input_tokens": 61_000, "context_window_size": 1_000_000},
        )
        with _Stdio(json.dumps(payload).encode()) as stdio:
            rc = collector.main([])
            output = stdio.stdout_text
        self.assertEqual(rc, 0)
        self.assertTrue(output.startswith("Opus 4.5 high | ctx 61K | 5h 10% "))
        cache = json.loads((self.state / "claude-statusline.json").read_text())
        self.assertEqual(sorted(cache), ["captured_at", "five_hour", "seven_day"])


class ProfileScopedCacheTests(unittest.TestCase):
    """A non-default CLAUDE_CONFIG_DIR must not collide with ~/.claude."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def _env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "HERDR_PLUGIN_STATE_DIR",
                "MODEL_LANES_STATE_DIR",
                "XDG_STATE_HOME",
                "CLAUDE_CONFIG_DIR",
            )
        }
        env.update({k: v for k, v in overrides.items() if v is not None})
        return env

    def _run(self, stdin_bytes: bytes) -> tuple:
        with _Stdio(stdin_bytes) as stdio:
            rc = collector.main([])
            output = stdio.stdout_text
            errors = stdio.stderr_text
        return rc, output, errors

    def test_unset_or_default_config_dir_uses_root_state(self) -> None:
        for config_dir in (None, "~/.claude"):
            with self.subTest(claude_config_dir=config_dir), mock.patch.dict(
                os.environ,
                self._env(
                    HERDR_PLUGIN_STATE_DIR=str(self.state),
                    CLAUDE_CONFIG_DIR=config_dir,
                ),
            ):
                self.assertEqual(
                    collector.statusline_cache_path(),
                    self.state / "claude-statusline.json",
                )

    def test_non_default_profile_keys_into_profiles_subdir(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR=str(self.state),
                CLAUDE_CONFIG_DIR="~/.claude-account3",
            ),
        ):
            expected = self.state / "profiles" / ".claude-account3" / "claude-statusline.json"
            self.assertEqual(collector.statusline_cache_path(), expected)
            self.assertEqual(
                collector.root_statusline_cache_path(),
                self.state / "claude-statusline.json",
            )

    def test_non_home_config_dir_keys_by_sanitized_basename_and_digest(self) -> None:
        raw = "/opt/claude/team profile"
        digest = hashlib.sha256(os.path.normpath(raw).encode("utf-8")).hexdigest()[:12]
        expected = self.state / "profiles" / f"team_profile-{digest}" / "claude-statusline.json"
        with mock.patch.dict(
            os.environ, self._env(HERDR_PLUGIN_STATE_DIR=str(self.state))
        ):
            self.assertEqual(collector.statusline_cache_path(config_dir=raw), expected)

    def test_already_scoped_state_dir_does_not_double_nest(self) -> None:
        for leaf in ("profiles/.claude-account3", "profile-abc"):
            with self.subTest(leaf=leaf):
                scoped = str(self.state / leaf)
                with mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR=scoped,
                        CLAUDE_CONFIG_DIR="~/.claude-account3",
                    ),
                ):
                    self.assertEqual(
                        collector.statusline_cache_path(),
                        Path(scoped) / "claude-statusline.json",
                    )

    def test_main_writes_profile_cache_and_root_cache(self) -> None:
        env = self._env(
            HERDR_PLUGIN_STATE_DIR=str(self.state),
            CLAUDE_CONFIG_DIR="~/.claude-account3",
        )
        payload = json.dumps(
            _payload(five_hour=_live_window(10), seven_day=_live_window(20))
        ).encode()
        with mock.patch.dict(os.environ, env):
            rc, _output, errors = self._run(payload)

        self.assertEqual(rc, 0)
        self.assertNotIn("warning", errors)
        profile_cache = self.state / "profiles" / ".claude-account3" / "claude-statusline.json"
        root_cache = self.state / "claude-statusline.json"
        for path in (profile_cache, root_cache):
            self.assertTrue(path.exists(), path)
            cached = json.loads(path.read_text())
            self.assertEqual(cached["five_hour"]["utilization"], 10)
            self.assertEqual(cached["seven_day"]["utilization"], 20)
        self.assertEqual(stat.S_IMODE(profile_cache.stat().st_mode), 0o600)

        scoped_limit = profile_cache.with_name("claude-scoped-limit.json")
        now = int(time.time())
        scoped_limit.write_text(
            json.dumps(
                {
                    "scope": "Fable",
                    "utilization": 5.0,
                    "resets_at": now + 3600,
                    "fetched_at": now - 60,
                }
            )
        )
        with mock.patch.dict(os.environ, env):
            loaded = collector.load_scoped_limit(now)
        self.assertEqual(loaded["scope"], "Fable")

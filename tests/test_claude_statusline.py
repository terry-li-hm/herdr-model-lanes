"""Tests for the Claude Code status-line quota collector."""

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
            if key != "HERDR_PLUGIN_STATE_DIR"
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

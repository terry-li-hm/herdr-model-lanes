"""Credential and data-boundary tests. The sentinel must never appear in sinks."""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import claude_max_usage as claude_helper
import glm_usage as glm_helper
import grok_usage as grok_helper
import herdr_model_lanes as quota

SENTINEL = "SENTINEL_SECRET_123"
NOW = 1_700_000_000
NESTED_DEPTH = 200_000


def nested_json_array() -> bytes:
    return ("[" * NESTED_DEPTH + "]" * NESTED_DEPTH).encode()


def _assert_clean(test: unittest.TestCase, *parts: object) -> None:
    blob = "\n".join("" if part is None else str(part) for part in parts)
    test.assertNotIn(SENTINEL, blob)


class RecordingResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.limit: int | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        self.limit = limit
        if limit < 0:
            return self.payload
        return self.payload[:limit]


def _env_from(mapping: dict):
    return lambda name: mapping.get(name, "")


class GrokHelperBoundaryTests(unittest.TestCase):
    def test_auth_extra_fields_are_ignored_and_never_echoed(self) -> None:
        payload = json.dumps(
            {
                "https://auth.x.ai::1": {
                    "key": "live-login-key",
                    "refresh": SENTINEL,
                    "email": SENTINEL,
                }
            }
        )

        self.assertEqual(grok_helper.parse_auth_payload(payload), "live-login-key")
        with self.assertRaises(grok_helper.GrokUsageError) as raised:
            grok_helper.parse_auth_payload("{")
        _assert_clean(self, raised.exception)

    def test_timestamp_suffix_is_rejected_before_stdout(self) -> None:
        payload = {
            "config": {
                "creditUsagePercent": 42.5,
                "currentPeriod": {
                    "type": grok_helper.WEEKLY_PERIOD_TYPE,
                    "end": "2026-08-22T00:00:00Z" + SENTINEL,
                },
            }
        }

        with self.assertRaises(grok_helper.GrokUsageError) as raised:
            grok_helper.parse_billing(payload)
        _assert_clean(self, raised.exception)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(grok_helper, "read_login_key", return_value=SENTINEL),
            mock.patch.object(
                grok_helper,
                "fetch_credits",
                side_effect=grok_helper.GrokUsageError(
                    "billing period end is not a timestamp"
                ),
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            rc = grok_helper.main()

        self.assertEqual(rc, 1)
        self.assertIn("grok usage error", stderr.getvalue())
        _assert_clean(self, stdout.getvalue(), stderr.getvalue())

    def test_oversized_body_is_rejected_before_json_loads(self) -> None:
        body = b'{"ok": true}' + b" " * grok_helper.MAX_RESPONSE_BYTES
        response = RecordingResponse(body)

        with (
            mock.patch.object(grok_helper.json, "loads", wraps=json.loads) as loads,
            self.assertRaises(grok_helper.GrokUsageError) as raised,
        ):
            grok_helper.fetch_credits(
                SENTINEL, opener=lambda _request, timeout: response
            )

        self.assertEqual(response.limit, grok_helper.MAX_RESPONSE_BYTES + 1)
        loads.assert_not_called()
        self.assertIn("1 MiB", str(raised.exception))
        _assert_clean(self, raised.exception)

    def test_nested_json_is_invalid_not_a_crash(self) -> None:
        response = RecordingResponse(nested_json_array())

        with self.assertRaises(grok_helper.GrokUsageError) as raised:
            grok_helper.fetch_credits(
                SENTINEL, opener=lambda _request, timeout: response
            )

        self.assertIn("JSON", str(raised.exception))
        _assert_clean(self, raised.exception)

    def test_network_error_and_redirect_do_not_echo_sentinel(self) -> None:
        def opener(_request, timeout):
            raise URLError(f"down {SENTINEL}")

        with self.assertRaises(grok_helper.GrokUsageError) as raised:
            grok_helper.fetch_credits(SENTINEL, opener=opener)
        _assert_clean(self, raised.exception)

        request = urllib.request.Request(
            grok_helper.BILLING_URL,
            headers={"Authorization": f"Bearer {SENTINEL}"},
        )
        with self.assertRaises(grok_helper.GrokUsageError) as raised:
            grok_helper.RejectRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, f"https://x.invalid/{SENTINEL}"
            )
        _assert_clean(self, raised.exception)


class ClaudeHelperBoundaryTests(unittest.TestCase):
    def test_keychain_payload_fields_are_not_echoed(self) -> None:
        payload = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": SENTINEL,
                    "refreshToken": SENTINEL,
                    "expiresAt": NOW - 1,
                }
            }
        )
        with self.assertRaises(claude_helper.UsageHelperError) as raised:
            claude_helper.parse_keychain_payload(payload, NOW)
        _assert_clean(self, raised.exception)

    def test_window_passthrough_of_sentinel_is_rejected(self) -> None:
        with self.assertRaises(claude_helper.UsageHelperError) as raised:
            claude_helper._normalize_window(
                {"utilization": SENTINEL, "resets_at": SENTINEL}, "five_hour"
            )
        _assert_clean(self, raised.exception)
        with self.assertRaises(claude_helper.UsageHelperError) as raised:
            claude_helper._normalize_window(
                {"utilization": 2, "resets_at": "2026-08-17T14:29:59Z" + SENTINEL},
                "five_hour",
            )
        _assert_clean(self, raised.exception)

    def test_fetch_rejects_sentinel_window_and_main_stays_clean(self) -> None:
        payload = {
            "five_hour": {"utilization": SENTINEL, "resets_at": SENTINEL},
            "seven_day": {
                "utilization": 9,
                "resets_at": "2026-08-23T08:59:59Z",
            },
            "seven_day_sonnet": None,
        }
        response = RecordingResponse(json.dumps(payload).encode())
        with self.assertRaises(claude_helper.UsageHelperError) as raised:
            claude_helper.fetch_usage(
                SENTINEL, opener=lambda _request, timeout: response
            )
        _assert_clean(self, raised.exception)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(claude_helper, "read_oauth_token", return_value=SENTINEL),
            mock.patch.object(
                claude_helper,
                "fetch_usage",
                side_effect=claude_helper.UsageHelperError(
                    "usage endpoint returned an invalid five_hour window"
                ),
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            rc = claude_helper.main()
        self.assertEqual(rc, 1)
        self.assertIn("claude usage error", stderr.getvalue())
        _assert_clean(self, stdout.getvalue(), stderr.getvalue())

    def test_oversized_and_nested_bodies_are_contained(self) -> None:
        oversize = RecordingResponse(
            b'{"ok": true}' + b" " * claude_helper.MAX_RESPONSE_BYTES
        )
        with (
            mock.patch.object(claude_helper.json, "loads", wraps=json.loads) as loads,
            self.assertRaises(claude_helper.UsageHelperError) as raised,
        ):
            claude_helper.fetch_usage(
                SENTINEL, opener=lambda _request, timeout: oversize
            )
        loads.assert_not_called()
        self.assertIn("1 MiB", str(raised.exception))
        _assert_clean(self, raised.exception)

        with self.assertRaises(claude_helper.UsageHelperError) as raised:
            claude_helper.fetch_usage(
                SENTINEL,
                opener=lambda _request, timeout: RecordingResponse(nested_json_array()),
            )
        _assert_clean(self, raised.exception)


class GlmHelperBoundaryTests(unittest.TestCase):
    def test_reads_only_bigmodel_coding_key_and_treats_dollar_refs_as_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pi_path = root / "models.json"
            pi_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai": {"apiKey": SENTINEL},
                            "anthropic": {"apiKey": SENTINEL},
                            "bigmodel-coding": {"apiKey": "$ZHIPU_API_KEY"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(glm_helper, "PI_MODELS_FILENAME", pi_path),
                mock.patch.object(
                    glm_helper, "ENV_RESOLVED_FILENAME", root / "env.resolved"
                ),
                mock.patch.object(glm_helper, "CONFIG_KEY_FILES", ()),
                self.assertRaises(glm_helper.GlmUsageError) as raised,
            ):
                glm_helper.resolve_key(_env_from({}))
        _assert_clean(self, raised.exception)

    def test_literal_bigmodel_coding_key_wins_and_other_providers_are_ignored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pi_path = root / "models.json"
            pi_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai": {"apiKey": SENTINEL},
                            "bigmodel-coding": {"apiKey": "glm-live-key"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(glm_helper, "PI_MODELS_FILENAME", pi_path),
                mock.patch.object(
                    glm_helper, "ENV_RESOLVED_FILENAME", root / "env.resolved"
                ),
                mock.patch.object(glm_helper, "CONFIG_KEY_FILES", ()),
            ):
                key, base = glm_helper.resolve_key(_env_from({}))
        self.assertEqual(key, "glm-live-key")
        self.assertEqual(base, glm_helper.BIGMODEL_BASE)
        self.assertNotEqual(key, SENTINEL)

    def test_oversized_nested_redirect_and_errors_stay_clean(self) -> None:
        oversize = RecordingResponse(
            b'{"ok": true}' + b" " * glm_helper.MAX_RESPONSE_BYTES
        )
        with (
            mock.patch.object(glm_helper.json, "loads", wraps=json.loads) as loads,
            self.assertRaises(glm_helper.GlmUsageError) as raised,
        ):
            glm_helper.fetch_quota(SENTINEL, opener=lambda _request, timeout: oversize)
        loads.assert_not_called()
        _assert_clean(self, raised.exception)

        with self.assertRaises(glm_helper.GlmUsageError) as raised:
            glm_helper.fetch_quota(
                SENTINEL,
                opener=lambda _request, timeout: RecordingResponse(nested_json_array()),
            )
        _assert_clean(self, raised.exception)

        request = urllib.request.Request(
            glm_helper.BIGMODEL_BASE + glm_helper.QUOTA_PATH,
            headers={"Authorization": f"Bearer {SENTINEL}"},
        )
        with self.assertRaises(glm_helper.GlmUsageError) as raised:
            glm_helper.RejectRedirectHandler().redirect_request(
                request, None, 307, "Tmp", {}, f"https://evil.invalid/{SENTINEL}"
            )
        _assert_clean(self, raised.exception)


class PublisherBoundaryTests(unittest.TestCase):
    def test_non_utf8_helper_stdout_does_not_crash_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            script = root / "helper.py"
            script.write_text(
                "import sys\n"
                f"sys.stdout.buffer.write(b'\\xff' + {SENTINEL.encode()!r})\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(quota, "publish_to_focused_workspace") as publish,
                mock.patch.object(
                    quota, "query_codex", side_effect=quota.QuotaError("offline")
                ),
                mock.patch.object(
                    quota, "query_claude", side_effect=quota.QuotaError("offline")
                ),
            ):
                outcome = quota.refresh(
                    state_dir=state,
                    now=NOW,
                    include_grok=True,
                    grok_command=[sys.executable, str(script)],
                )

        self.assertIsNone(outcome.grok)
        token = publish.call_args.args[0]
        self.assertIn("Gk n/a", token)
        _assert_clean(self, token, outcome.errors)
        for path in state.rglob("*"):
            if path.is_file():
                _assert_clean(self, path.read_text(encoding="utf-8", errors="replace"))

    def test_nested_helper_stdout_is_quota_error_not_crash(self) -> None:
        with (
            self.assertRaises(quota.QuotaError) as raised,
            mock.patch.object(quota.subprocess, "run") as run,
        ):
            run.return_value = mock.Mock(
                returncode=0,
                stdout="[" * NESTED_DEPTH + "]" * NESTED_DEPTH,
                stderr=SENTINEL,
            )
            quota.query_grok(now=NOW, grok_command=["fake-grok-helper"])
        _assert_clean(self, raised.exception)

    def test_huge_helper_stdout_is_rejected_before_json_loads(self) -> None:
        huge = "{" + " " * (quota.MAX_HELPER_STDOUT_BYTES) + "}"
        with (
            mock.patch.object(quota.json, "loads", wraps=json.loads) as loads,
            mock.patch.object(quota.subprocess, "run") as run,
            self.assertRaises(quota.QuotaError) as raised,
        ):
            run.return_value = mock.Mock(returncode=0, stdout=huge, stderr="")
            quota.query_glm(now=NOW, glm_command=["fake-glm-helper"])
        loads.assert_not_called()
        _assert_clean(self, raised.exception)

    def test_helper_stderr_sentinel_is_not_cached_or_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            script = root / "helper.py"
            script.write_text(
                f"import sys\nprint({SENTINEL!r}, file=sys.stderr)\nsys.exit(1)\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(quota, "publish_to_focused_workspace") as publish,
                mock.patch.object(
                    quota, "query_codex", side_effect=quota.QuotaError("offline")
                ),
                mock.patch.object(
                    quota, "query_claude", side_effect=quota.QuotaError("offline")
                ),
            ):
                outcome = quota.refresh(
                    state_dir=state,
                    now=NOW,
                    include_glm=True,
                    glm_command=[sys.executable, str(script)],
                )

        token = publish.call_args.args[0]
        _assert_clean(self, token, outcome.errors)
        for path in state.rglob("*"):
            if path.is_file():
                _assert_clean(self, path.read_text(encoding="utf-8", errors="replace"))

    def test_cache_round_trip_is_numbers_only_and_mode_0600(self) -> None:
        grok = quota.GrokUsage(quota.QuotaWindow(10, 90, NOW + 10), NOW)
        glm = quota.GlmUsage(
            quota.QuotaWindow(20, 80, NOW + 10, quota.GLM_WINDOW_SECONDS), NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grok_path = root / quota.GROK_CACHE_FILENAME
            glm_path = root / quota.GLM_CACHE_FILENAME
            quota.save_grok_cache(grok_path, grok)
            quota.save_glm_cache(glm_path, glm)
            grok_raw = grok_path.read_text(encoding="utf-8")
            glm_raw = glm_path.read_text(encoding="utf-8")
            self.assertEqual(stat.S_IMODE(grok_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(glm_path.stat().st_mode), 0o600)
            self.assertEqual(set(json.loads(grok_raw)), {"weekly", "fetched_at"})
            self.assertEqual(set(json.loads(glm_raw)), {"five_hour", "fetched_at"})
            _assert_clean(self, grok_raw, glm_raw)


class AgQuotingTests(unittest.TestCase):
    def test_route_argv_quotes_toml_metacharacters_so_eval_cannot_run_them(
        self,
    ) -> None:
        evil = quota.LaneSpec(
            name="x; touch pwned-name",
            kind="/usr/bin/true",
            args=("$(touch pwned-args)", "; touch pwned-semi"),
            quota="claude",
            classified_ok=True,
        )
        argv_line = shlex.join(quota.lane_command(evil))
        self.assertNotIn(evil.name, argv_line)

        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            completed = subprocess.run(
                ["bash", "-c", 'eval exec "$1"', "_", argv_line],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            leftovers = [path.name for path in cwd.iterdir()]

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(leftovers, [])

    def test_ag_eval_does_not_run_lane_name_or_ag_timeout_injection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            ag_text = Path("bin/ag").read_text(encoding="utf-8")
            (bin_dir / "ag").write_text(ag_text, encoding="utf-8")
            (bin_dir / "ag").chmod(0o755)
            argv_line = shlex.join(
                ["/usr/bin/true", "$(touch pwned-args)", "; touch pwned-semi"]
            )
            (root / "herdr_model_lanes.py").write_text(
                "import sys\n"
                "print('x; touch pwned-name: n/a (claude quota unavailable)', file=sys.stderr)\n"
                "print('pick: x; touch pwned-name (first healthy lane in order)', file=sys.stderr)\n"
                f"print({argv_line!r})\n",
                encoding="utf-8",
            )
            tmpdir = root / "tmp"
            tmpdir.mkdir()
            env = os.environ.copy()
            env["AG_YES"] = "1"
            env["AG_TIMEOUT"] = "10; touch pwned-timeout"
            env["HERDR_PLUGIN_STATE_DIR"] = str(root / "state")
            env["TMPDIR"] = str(tmpdir)
            completed = subprocess.run(
                ["sh", str(bin_dir / "ag"), "medium"],
                cwd=root,
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
            )
            pwned = list(root.rglob("pwned*"))
            leftovers = list(tmpdir.iterdir())

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(pwned, [])
        self.assertEqual(leftovers, [])
        _assert_clean(self, completed.stdout, completed.stderr)


if __name__ == "__main__":
    unittest.main()

import io
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import claude_max_usage as helper
import herdr_model_lanes as quota


def setUpModule() -> None:
    """Isolate the suite from the launching session's Claude profile.

    The per-test environments are applied without ``clear=True``, so a
    session started under a non-default ``CLAUDE_CONFIG_DIR`` would otherwise
    leak that profile into every cache and credential path.
    """
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)


NOW_MS = 1_700_000_000_000
SYNTHETIC_TOKEN = "synthetic-oauth-token-for-tests"


def _iso_timestamp(epoch: int) -> str:
    """Render a Unix epoch as the ISO-8601 shape the endpoint returns."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filtered_env(**overrides: str | None) -> dict:
    """Env without the state-dir/profile variables; a None value removes."""
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
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _state_env(state_dir: str, **overrides: str | None) -> dict:
    """Env for an isolated plugin state dir without profile leakage."""
    return _filtered_env(HERDR_PLUGIN_STATE_DIR=state_dir, **overrides)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode()


class CredentialParsingTests(unittest.TestCase):
    def test_extracts_live_oauth_token(self) -> None:
        payload = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": SYNTHETIC_TOKEN,
                    "expiresAt": NOW_MS + 60_000,
                }
            }
        )

        self.assertEqual(
            helper.parse_credential_payload(payload, NOW_MS), SYNTHETIC_TOKEN
        )

    def test_rejects_expired_or_malformed_credentials_without_echoing_payload(
        self,
    ) -> None:
        expired = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": SYNTHETIC_TOKEN,
                    "expiresAt": NOW_MS - 1,
                }
            }
        )
        for payload in (expired, SYNTHETIC_TOKEN):
            with self.assertRaises(helper.UsageHelperError) as raised:
                helper.parse_credential_payload(payload, NOW_MS)
            self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))

    def test_rejects_non_finite_credential_expiry(self) -> None:
        for expires_at in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(expires_at=expires_at):
                payload = json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": SYNTHETIC_TOKEN,
                            "expiresAt": expires_at,
                        }
                    }
                )
                with self.assertRaises(helper.UsageHelperError) as raised:
                    helper.parse_credential_payload(payload, NOW_MS)
                self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))


class KeychainTests(unittest.TestCase):
    @mock.patch("claude_max_usage.subprocess.run")
    def test_reads_only_named_keychain_item_with_bounded_subprocess(
        self, run: mock.Mock
    ) -> None:
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": SYNTHETIC_TOKEN,
                        "expiresAt": NOW_MS + 60_000,
                    }
                }
            ),
            stderr="",
        )

        oauth_value = helper.read_oauth_token(
            now_ms=NOW_MS, security_bin="/usr/bin/security", platform="darwin"
        )

        self.assertEqual(oauth_value, SYNTHETIC_TOKEN)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
        )
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @mock.patch("claude_max_usage.subprocess.run")
    def test_keychain_failure_does_not_include_stderr(self, run: mock.Mock) -> None:
        run.return_value = mock.Mock(
            returncode=44,
            stdout="",
            stderr=f"failure containing {SYNTHETIC_TOKEN}",
        )

        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_oauth_token(now_ms=NOW_MS, platform="darwin")

        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))


class FetchTests(unittest.TestCase):
    @mock.patch("claude_max_usage.urllib.request.build_opener")
    def test_default_fetch_uses_redirect_rejecting_opener(
        self, build_opener: mock.Mock
    ) -> None:
        director = mock.Mock()
        director.open.return_value = FakeResponse(
            {
                "five_hour": None,
                "seven_day": {
                    "utilization": 9,
                    "resets_at": "2026-08-23T08:59:59Z",
                },
                "seven_day_sonnet": None,
            }
        )
        build_opener.return_value = director

        result = helper.fetch_usage(SYNTHETIC_TOKEN)

        self.assertEqual(result["seven_day"]["utilization"], 9)
        self.assertIs(build_opener.call_args.args[0], helper.RejectRedirectHandler)
        request = director.open.call_args.args[0]
        self.assertEqual(request.full_url, helper.USAGE_URL)
        self.assertEqual(
            director.open.call_args.kwargs["timeout"], helper.FETCH_TIMEOUT_SECS
        )

    def test_sends_token_only_in_authorization_header_and_normalizes_response(
        self,
    ) -> None:
        captured = {}
        payload = {
            "five_hour": {
                "utilization": 2,
                "resets_at": "2026-08-17T14:29:59Z",
                "limit_dollars": 100,
            },
            "seven_day": {
                "utilization": 9,
                "resets_at": "2026-08-23T08:59:59Z",
                "used_dollars": 20,
            },
            "seven_day_sonnet": None,
            "extra_usage": {"monthly_limit": 1000, "used_credits": 2043},
        }

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["timeout"] = timeout
            return FakeResponse(payload)

        result = helper.fetch_usage(SYNTHETIC_TOKEN, timeout=5, opener=opener)

        self.assertEqual(captured["url"], helper.USAGE_URL)
        self.assertEqual(
            captured["headers"]["authorization"], f"Bearer {SYNTHETIC_TOKEN}"
        )
        self.assertEqual(captured["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(captured["timeout"], 5)
        self.assertEqual(set(result), {"five_hour", "seven_day", "seven_day_sonnet"})
        self.assertEqual(set(result["five_hour"]), {"utilization", "resets_at"})
        self.assertEqual(set(result["seven_day"]), {"utilization", "resets_at"})
        self.assertNotIn("extra_usage", result)

    def test_network_failure_never_echoes_token(self) -> None:
        def opener(_request, timeout):
            raise URLError(f"network detail {SYNTHETIC_TOKEN}")

        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.fetch_usage(SYNTHETIC_TOKEN, opener=opener)

        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))

    def test_redirects_are_refused_without_echoing_destination(self) -> None:
        request = urllib.request.Request(
            helper.USAGE_URL,
            headers={"Authorization": f"Bearer {SYNTHETIC_TOKEN}"},
        )
        handler = helper.RejectRedirectHandler()

        with self.assertRaises(helper.UsageHelperError) as raised:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                f"https://example.invalid/{SYNTHETIC_TOKEN}",
            )

        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))


def _write_credentials(directory: str, token: str, expires_in_ms: int = 60_000) -> str:
    path = os.path.join(directory, ".credentials.json")
    payload = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "expiresAt": NOW_MS + expires_in_ms,
            }
        }
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)
    return path


class LinuxCredentialsPathTests(unittest.TestCase):
    def test_custom_config_dir_is_preferred_over_home(self) -> None:
        self.assertEqual(
            helper.credentials_path("/custom/dir"),
            os.path.join("/custom/dir", ".credentials.json"),
        )

    def test_env_var_then_home_default(self) -> None:
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": "/env/claude"}, clear=False
        ):
            self.assertEqual(helper.credentials_path(), "/env/claude/.credentials.json")
        environ = {
            key: value
            for key, value in os.environ.items()
            if key != "CLAUDE_CONFIG_DIR"
        }
        with mock.patch("os.environ", environ):
            self.assertEqual(
                helper.credentials_path(),
                os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json"),
            )


class LinuxCredentialsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = self._tmp.name
        self.path = _write_credentials(self.directory, SYNTHETIC_TOKEN)

    def test_secure_read_returns_live_token(self) -> None:
        extracted = helper.read_credentials_file(self.path, now_ms=NOW_MS)
        self.assertEqual(extracted, SYNTHETIC_TOKEN)

    def test_read_via_dispatch_reads_default_path(self) -> None:
        extracted = helper.read_oauth_token(
            now_ms=NOW_MS,
            config_dir=self.directory,
            platform="linux",
        )
        self.assertEqual(extracted, SYNTHETIC_TOKEN)

    def test_oversize_file_is_refused(self) -> None:
        with open(self.path, "r+b") as handle:
            handle.truncate(helper.MAX_CREDENTIALS_BYTES + 1)
        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_credentials_file(self.path, now_ms=NOW_MS)
        self.assertIn("64 KiB", str(raised.exception))

    def test_symlink_is_refused(self) -> None:
        link = os.path.join(self.directory, "link.json")
        os.symlink(self.path, link)
        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_credentials_file(link, now_ms=NOW_MS)
        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))

    def test_non_regular_file_is_refused(self) -> None:
        if os.path.exists("/dev/null") is False:
            self.skipTest("no /dev/null")
        with self.assertRaises(helper.UsageHelperError):
            helper.read_credentials_file("/dev/null", now_ms=NOW_MS)

    def test_wrong_owner_is_refused_when_root_not_in_effect(self) -> None:
        info = os.stat(self.path)
        if info.st_uid == 0 and os.geteuid() != 0:
            fake = os.stat(self.path)
            with (
                mock.patch("os.fstat", return_value=fake),
                mock.patch("os.geteuid", return_value=info.st_uid + 1),
            ):
                with self.assertRaises(helper.UsageHelperError) as raised:
                    helper.read_credentials_file(self.path, now_ms=NOW_MS)
                self.assertIn("owned", str(raised.exception))
        else:
            fake = type(info)(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    info.st_uid + 1,
                    info.st_gid,
                    info.st_size,
                    info.st_atime,
                    info.st_mtime,
                    info.st_ctime,
                )
            )
            with mock.patch("os.fstat", return_value=fake):
                with self.assertRaises(helper.UsageHelperError) as raised:
                    helper.read_credentials_file(self.path, now_ms=NOW_MS)
                self.assertIn("owned", str(raised.exception))

    def test_group_or_world_permissions_are_refused(self) -> None:
        for mode in (0o640, 0o604, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path, mode)
                with self.assertRaises(helper.UsageHelperError) as raised:
                    helper.read_credentials_file(self.path, now_ms=NOW_MS)
                self.assertIn("accessible", str(raised.exception))

    def test_malformed_payload_never_echoes_contents(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(f"{{not json with {SYNTHETIC_TOKEN}")
        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_credentials_file(self.path, now_ms=NOW_MS)
        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))

    def test_expired_payload_is_refused_without_echoing_token(self) -> None:
        _write_credentials(self.directory, SYNTHETIC_TOKEN, expires_in_ms=-1)
        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_credentials_file(self.path, now_ms=NOW_MS)
        self.assertIn("expired", str(raised.exception))
        self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))


class PlatformDispatchTests(unittest.TestCase):
    @mock.patch("claude_max_usage.subprocess.run")
    def test_macos_dispatch_uses_keychain(self, run: mock.Mock) -> None:
        run.return_value = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": SYNTHETIC_TOKEN,
                        "expiresAt": NOW_MS + 60_000,
                    }
                }
            ),
            stderr="",
        )

        extracted = helper.read_oauth_token(
            now_ms=NOW_MS, security_bin="/usr/bin/security", platform="darwin"
        )

        self.assertEqual(extracted, SYNTHETIC_TOKEN)
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/security")

    def test_unsupported_platform_fails_clearly(self) -> None:
        with self.assertRaises(helper.UsageHelperError) as raised:
            helper.read_oauth_token(now_ms=NOW_MS, platform="win32")
        self.assertIn("unsupported platform", str(raised.exception))

    def test_linux_dispatch_honors_custom_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_credentials(directory, SYNTHETIC_TOKEN)
            extracted = helper.read_oauth_token(
                now_ms=NOW_MS, config_dir=directory, platform="linux"
            )
        self.assertEqual(extracted, SYNTHETIC_TOKEN)


class PublicSurfaceTests(unittest.TestCase):
    def test_repository_has_public_release_scaffolding_and_no_internal_task_file(
        self,
    ) -> None:
        self.assertFalse(Path("TASK.md").exists())
        self.assertTrue(Path("LICENSE").read_text().startswith("Apache License"))
        self.assertTrue(Path("SECURITY.md").is_file())
        self.assertTrue(Path("CHANGELOG.md").is_file())
        self.assertTrue(Path(".github/workflows/test.yml").is_file())

    def test_main_plugin_keeps_credential_operations_in_helper(self) -> None:
        plugin = Path("herdr_model_lanes.py").read_text()
        helper_source = Path("claude_max_usage.py").read_text()

        self.assertNotIn("find-generic-password", plugin)
        self.assertNotIn("Authorization", plugin)
        self.assertIn("find-generic-password", helper_source)
        self.assertIn("Authorization", helper_source)

    def test_main_plugin_is_credential_blind_on_linux_too(self) -> None:
        plugin = Path("herdr_model_lanes.py").read_text()

        self.assertNotIn("CLAUDE_CONFIG_DIR", plugin)
        self.assertNotIn(".credentials.json", plugin)
        self.assertNotIn("/.claude", plugin)

    def test_security_policy_and_manifest_no_longer_claim_macos_only(self) -> None:
        security = Path("SECURITY.md").read_text()
        manifest = Path("herdr-plugin.toml").read_text()

        for document in (security, manifest):
            self.assertNotIn("macOS-only", document)
            self.assertNotIn("macos-only", document)
        self.assertIn("CLAUDE_CONFIG_DIR", security)
        self.assertIn("macos", manifest)
        self.assertIn("linux", manifest)


class StatuslineCachePathTests(unittest.TestCase):
    def _env(self, **overrides) -> dict:
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

    def test_same_precedence_as_ag(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR="/state/a",
                MODEL_LANES_STATE_DIR="/state/b",
                XDG_STATE_HOME="/state/c",
            ),
        ):
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join("/state/a", "claude-statusline.json"),
            )
        with mock.patch.dict(
            os.environ,
            self._env(MODEL_LANES_STATE_DIR="/state/b", XDG_STATE_HOME="/state/c"),
        ):
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join("/state/b", "claude-statusline.json"),
            )
        with mock.patch.dict(os.environ, self._env(XDG_STATE_HOME="/state/c")):
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join(
                    "/state/c",
                    "herdr/plugins/terry.herdr-model-lanes",
                    "claude-statusline.json",
                ),
            )
        with mock.patch.dict(os.environ, self._env()):
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join(
                    os.path.expanduser("~"),
                    ".local/state/herdr/plugins/terry.herdr-model-lanes",
                    "claude-statusline.json",
                ),
            )


class StatuslineCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.NOW = int(time.time())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("HERDR_PLUGIN_STATE_DIR", "CLAUDE_CONFIG_DIR")
        }
        env["HERDR_PLUGIN_STATE_DIR"] = self._tmp.name
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self._tmp.name, "claude-statusline.json")

    def _write_cache(
        self, captured_at: int, five_hour: object, seven_day: object
    ) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "captured_at": captured_at,
                    "five_hour": five_hour,
                    "seven_day": seven_day,
                },
                handle,
            )

    def test_fresh_cache_short_circuits_before_token_or_network(self) -> None:
        self._write_cache(
            self.NOW - 60,
            {"utilization": 42, "resets_at": self.NOW + 1800},
            {"utilization": 80, "resets_at": self.NOW + 86400},
        )

        with mock.patch(
            "claude_max_usage.read_oauth_token",
            side_effect=AssertionError("credential access must not happen"),
        ):
            result = helper.load_statusline_usage(now=self.NOW)

        self.assertEqual(result["seven_day"]["utilization"], 80)
        self.assertEqual(result["five_hour"]["utilization"], 42)
        self.assertNotIn("seven_day_sonnet", result)
        self.assertTrue(result["seven_day"]["resets_at"].endswith("Z"))

    def test_main_uses_cache_without_credentials_or_refresh_bypass(self) -> None:
        self._write_cache(
            self.NOW - 60,
            None,
            {"utilization": 80, "resets_at": self.NOW + 86400},
        )
        captured = {}

        def fail(name):
            def _fail(*_args, **_kwargs):
                captured[name] = True
                raise AssertionError(f"{name} must not be reached")

            return _fail

        with (
            mock.patch("claude_max_usage.read_oauth_token", side_effect=fail("token")),
            mock.patch("claude_max_usage.fetch_usage", side_effect=fail("network")),
            mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
        ):
            for argv in ([], ["--refresh"]):
                with self.subTest(argv=argv):
                    rc = helper.main(list(argv))
                    self.assertEqual(rc, 0)
                    payload = json.loads(out.getvalue())
                    self.assertEqual(set(payload), {"five_hour", "seven_day"})
                    out.truncate(0)
                    out.seek(0)
        self.assertEqual(captured, {})

    def test_main_rejects_unknown_arguments(self) -> None:
        with mock.patch("sys.stderr", new_callable=lambda: io.StringIO()):
            rc = helper.main(["--bogus"])
        self.assertEqual(rc, 2)

    def test_stale_cache_falls_back_to_endpoint(self) -> None:
        self._write_cache(
            self.NOW - helper.STATUSLINE_CACHE_MAX_AGE_SECS - 1,
            {"utilization": 42, "resets_at": self.NOW + 3600},
            {"utilization": 80, "resets_at": self.NOW + 86400},
        )

        self.assertIsNone(helper.load_statusline_usage(now=self.NOW))

    def test_malformed_or_missing_cache_falls_back(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(helper.load_statusline_usage(now=self.NOW))

        os.unlink(self.path)
        self.assertIsNone(helper.load_statusline_usage(now=self.NOW))

        for document in (
            [],
            {"captured_at": "now"},
            {"captured_at": self.NOW, "seven_day": None},
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": 200, "resets_at": self.NOW + 1},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": 50, "resets_at": "soon"},
            },
            {
                "captured_at": float("nan"),
                "seven_day": {"utilization": 50, "resets_at": self.NOW + 1},
            },
            {
                "captured_at": float("inf"),
                "seven_day": {"utilization": 50, "resets_at": self.NOW + 1},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": float("nan"), "resets_at": self.NOW + 1},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": float("inf"), "resets_at": self.NOW + 1},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": 50, "resets_at": float("nan")},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": 50, "resets_at": float("inf")},
            },
            {
                "captured_at": self.NOW,
                "seven_day": {"utilization": 50, "resets_at": 10**30},
            },
        ):
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            with self.subTest(document=document):
                self.assertIsNone(helper.load_statusline_usage(now=self.NOW))

    def test_non_finite_five_hour_is_dropped_but_seven_day_still_used(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self._write_cache(
                    self.NOW - 60,
                    {"utilization": bad, "resets_at": self.NOW + 1},
                    {"utilization": 50, "resets_at": self.NOW + 86400},
                )
                result = helper.load_statusline_usage(now=self.NOW)
                self.assertIsNone(result["five_hour"])
                self.assertEqual(result["seven_day"]["utilization"], 50)

    def test_expired_seven_day_falls_back_but_expired_five_hour_is_dropped(
        self,
    ) -> None:
        self._write_cache(
            self.NOW - 60,
            {"utilization": 42, "resets_at": self.NOW - 1},
            {"utilization": 80, "resets_at": self.NOW - 1},
        )
        self.assertIsNone(helper.load_statusline_usage(now=self.NOW))

        self._write_cache(
            self.NOW - 60,
            {"utilization": 42, "resets_at": self.NOW - 1},
            {"utilization": 80, "resets_at": self.NOW + 86400},
        )
        result = helper.load_statusline_usage(now=self.NOW)
        self.assertIsNone(result["five_hour"])
        self.assertEqual(result["seven_day"]["utilization"], 80)

    def test_main_endpoint_fallback_is_unchanged(self) -> None:
        with (
            mock.patch(
                "claude_max_usage.read_oauth_token", return_value=SYNTHETIC_TOKEN
            ),
            mock.patch("claude_max_usage.fetch_usage") as fetch,
            mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
        ):
            fetch.return_value = {
                "five_hour": None,
                "seven_day": {"utilization": 9, "resets_at": "2026-08-23T08:59:59Z"},
                "seven_day_sonnet": None,
            }
            rc = helper.main([])

        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(set(payload), {"five_hour", "seven_day", "seven_day_sonnet"})
        fetch.assert_called_once_with(
            SYNTHETIC_TOKEN, timeout=helper.FETCH_TIMEOUT_SECS
        )


if __name__ == "__main__":
    unittest.main()


class ScopedLimitTests(unittest.TestCase):
    def test_extracts_model_scoped_weekly_limit(self) -> None:
        document = {
            "limits": [
                {
                    "kind": "session",
                    "percent": 13,
                    "resets_at": "2026-09-09T13:20:00+00:00",
                    "scope": None,
                },
                {
                    "kind": "weekly_all",
                    "percent": 39,
                    "resets_at": "2026-09-13T09:00:00+00:00",
                    "scope": None,
                },
                {
                    "kind": "weekly_scoped",
                    "percent": 76,
                    "severity": "warning",
                    "resets_at": "2026-09-13T09:00:00+00:00",
                    "scope": {
                        "model": {"id": None, "display_name": "Fable"},
                        "surface": None,
                    },
                },
            ]
        }
        scoped = helper.extract_scoped_limit(document)
        self.assertEqual(
            scoped, {"scope": "Fable", "utilization": 76.0, "resets_at": 1789290000}
        )

    def test_no_scoped_entry_returns_none(self) -> None:
        self.assertIsNone(
            helper.extract_scoped_limit(
                {"limits": [{"kind": "weekly_all", "percent": 1}]}
            )
        )
        self.assertIsNone(helper.extract_scoped_limit({"limits": None}))
        self.assertIsNone(helper.extract_scoped_limit("nope"))

    def test_save_scoped_limit_round_trips_with_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "claude-scoped-limit.json")
            helper.save_scoped_limit(
                {"scope": "Fable", "utilization": 76.0, "resets_at": 1789290000},
                1788950000,
                path,
            )
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as handle:
                self.assertEqual(
                    json.load(handle),
                    {
                        "scope": "Fable",
                        "utilization": 76.0,
                        "resets_at": 1789290000,
                        "fetched_at": 1788950000,
                    },
                )
            self.assertEqual(helper._scoped_cache_age(1788950900, path), 900)
            helper.save_scoped_limit(None, 1788950000, path)
            self.assertFalse(os.path.exists(path))


class ProfileScopedStatuslineTests(unittest.TestCase):
    """A non-default CLAUDE_CONFIG_DIR must not collide with ~/.claude."""

    def _env(self, **overrides) -> dict:
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

    def test_profile_config_dir_keys_statusline_and_scoped_paths(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR="/state/a",
                CLAUDE_CONFIG_DIR="~/.claude-account5",
            ),
        ):
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join(
                    "/state/a", "profiles", ".claude-account5", "claude-statusline.json"
                ),
            )
            self.assertEqual(
                helper.scoped_cache_path(),
                os.path.join(
                    "/state/a",
                    "profiles",
                    ".claude-account5",
                    "claude-scoped-limit.json",
                ),
            )

    def test_default_config_dir_keeps_root_paths(self) -> None:
        for config_dir in (None, "~/.claude"):
            with (
                self.subTest(claude_config_dir=config_dir),
                mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR="/state/a",
                        CLAUDE_CONFIG_DIR=config_dir,
                    ),
                ),
            ):
                self.assertEqual(
                    helper.statusline_cache_path(),
                    "/state/a/claude-statusline.json",
                )

    def test_already_scoped_state_dir_does_not_double_nest(self) -> None:
        for leaf in ("profiles/.claude-account5", "profile-123"):
            with (
                self.subTest(leaf=leaf),
                mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR=os.path.join("/state/a", leaf),
                        CLAUDE_CONFIG_DIR="~/.claude-account5",
                    ),
                ),
            ):
                self.assertEqual(
                    helper.statusline_cache_path(),
                    os.path.join("/state/a", leaf, "claude-statusline.json"),
                )

    def test_root_fresh_cache_does_not_short_circuit_profile_main(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "claude-statusline.json"), "w") as handle:
                json.dump(
                    {
                        "captured_at": now - 60,
                        "five_hour": None,
                        "seven_day": {"utilization": 80, "resets_at": now + 86400},
                    },
                    handle,
                )
            profile_cache = os.path.join(
                tmp, "profiles", ".claude-account5", "claude-statusline.json"
            )
            os.makedirs(os.path.dirname(profile_cache), exist_ok=True)
            with open(profile_cache, "w") as handle:
                json.dump(
                    {
                        "captured_at": now - 30,
                        "five_hour": {"utilization": 7, "resets_at": now + 3600},
                        "seven_day": {"utilization": 40, "resets_at": now + 86400},
                    },
                    handle,
                )
            with (
                mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR=tmp,
                        CLAUDE_CONFIG_DIR="~/.claude-account5",
                    ),
                ),
                mock.patch(
                    "claude_max_usage.read_oauth_token",
                    side_effect=AssertionError("credential access must not happen"),
                ),
                mock.patch(
                    "claude_max_usage.fetch_usage",
                    side_effect=AssertionError("network access must not happen"),
                ),
                mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
            ):
                rc = helper.main([])

        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["seven_day"]["utilization"], 40)
        self.assertEqual(payload["five_hour"]["utilization"], 7)

    def test_profile_main_fresh_fetch_ignores_root_cache(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "claude-statusline.json"), "w") as handle:
                json.dump(
                    {
                        "captured_at": now - 60,
                        "five_hour": None,
                        "seven_day": {"utilization": 80, "resets_at": now + 86400},
                    },
                    handle,
                )
            with (
                mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR=tmp,
                        CLAUDE_CONFIG_DIR="~/.claude-account5",
                    ),
                ),
                mock.patch(
                    "claude_max_usage.read_oauth_token", return_value=SYNTHETIC_TOKEN
                ),
                mock.patch("claude_max_usage.fetch_usage") as fetch,
                mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
            ):
                fetch.return_value = {
                    "five_hour": None,
                    "seven_day": {
                        "utilization": 9,
                        "resets_at": "2026-08-23T08:59:59Z",
                    },
                    "seven_day_sonnet": None,
                }
                rc = helper.main([])

        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["seven_day"]["utilization"], 9)
        fetch.assert_called_once()


class RootStatuslineCachePathTests(unittest.TestCase):
    """The root mirror path ignores profile keying entirely."""

    def _env(self, **overrides: str | None) -> dict:
        return _filtered_env(**overrides)

    def test_root_path_uses_state_dir_precedence_and_ignores_profiles(self) -> None:
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR="/state/a",
                CLAUDE_CONFIG_DIR="~/.claude-account6",
            ),
            clear=True,
        ):
            self.assertEqual(
                helper.root_statusline_cache_path(),
                "/state/a/claude-statusline.json",
            )
        with mock.patch.dict(
            os.environ,
            self._env(
                HERDR_PLUGIN_STATE_DIR="/state/a",
                MODEL_LANES_STATE_DIR="/state/b",
                CLAUDE_CONFIG_DIR="~/.claude-account6",
            ),
            clear=True,
        ):
            self.assertEqual(
                helper.root_statusline_cache_path(),
                "/state/a/claude-statusline.json",
            )
        with mock.patch.dict(
            os.environ,
            self._env(MODEL_LANES_STATE_DIR="/state/b", XDG_STATE_HOME="/state/c"),
            clear=True,
        ):
            self.assertEqual(
                helper.root_statusline_cache_path(),
                "/state/b/claude-statusline.json",
            )

    def test_root_path_matches_statusline_path_when_already_scoped(self) -> None:
        for leaf in ("profiles/.claude-account6", "profile-123"):
            with (
                self.subTest(leaf=leaf),
                mock.patch.dict(
                    os.environ,
                    self._env(
                        HERDR_PLUGIN_STATE_DIR=os.path.join("/state/a", leaf),
                        CLAUDE_CONFIG_DIR="~/.claude-account6",
                    ),
                ),
            ):
                self.assertEqual(
                    helper.root_statusline_cache_path(),
                    helper.statusline_cache_path(),
                    os.path.join("/state/a", leaf, "claude-statusline.json"),
                )

    def test_root_path_falls_back_to_xdg_then_home(self) -> None:
        with mock.patch.dict(
            os.environ, self._env(XDG_STATE_HOME="/state/c"), clear=True
        ):
            self.assertEqual(
                helper.root_statusline_cache_path(),
                os.path.join(
                    "/state/c",
                    "herdr/plugins/terry.herdr-model-lanes",
                    "claude-statusline.json",
                ),
            )
        with mock.patch.dict(os.environ, self._env(), clear=True):
            self.assertEqual(
                helper.root_statusline_cache_path(),
                os.path.join(
                    os.path.expanduser("~"),
                    ".local/state/herdr/plugins/terry.herdr-model-lanes",
                    "claude-statusline.json",
                ),
            )


class ScopedStatuslineMirrorTests(unittest.TestCase):
    """--scoped must mirror the official windows into both caches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_config = os.path.join(os.path.expanduser("~"), ".claude-account6")
        patcher = mock.patch.dict(
            os.environ,
            _state_env(self._tmp.name, CLAUDE_CONFIG_DIR=self.profile_config),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root_path = os.path.join(self._tmp.name, "claude-statusline.json")
        self.profile_path = os.path.join(
            self._tmp.name, "profiles", ".claude-account6", "claude-statusline.json"
        )

    def _windows(self, now: int) -> dict:
        return {
            "five_hour": {
                "utilization": 12.5,
                "resets_at": _iso_timestamp(now + 3600),
            },
            "seven_day": {"utilization": 9, "resets_at": _iso_timestamp(now + 86400)},
            "seven_day_sonnet": None,
            "weekly_scoped": {
                "scope": "Fable",
                "utilization": 76.0,
                "resets_at": 1789290000,
            },
        }

    def _run_scoped(self, windows: dict) -> tuple[int, str]:
        with (
            mock.patch(
                "claude_max_usage.read_oauth_token", return_value=SYNTHETIC_TOKEN
            ),
            mock.patch("claude_max_usage.fetch_usage", return_value=windows),
            mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
        ):
            rc = helper.main(["--scoped"])
        return rc, out.getvalue()

    def _statusline_files(self) -> list[str]:
        return [
            os.path.join(root, name)
            for root, _dirs, files in os.walk(self._tmp.name)
            for name in files
            if name == helper.STATUSLINE_CACHE_FILENAME
        ]

    def test_scoped_persists_windows_to_profile_and_root_caches(self) -> None:
        before = int(time.time())
        windows = self._windows(before)
        rc, stdout = self._run_scoped(windows)
        after = int(time.time())

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.strip(), json.dumps(windows["weekly_scoped"]))
        for path in (self.profile_path, self.root_path):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
                with open(path, encoding="utf-8") as handle:
                    document = json.load(handle)
                self.assertEqual(
                    set(document), {"captured_at", "five_hour", "seven_day"}
                )
                self.assertTrue(before <= document["captured_at"] <= after)
                self.assertEqual(
                    document["five_hour"],
                    {"utilization": 12.5, "resets_at": before + 3600},
                )
                self.assertEqual(
                    document["seven_day"],
                    {"utilization": 9, "resets_at": before + 86400},
                )
        self.assertEqual(
            sorted(self._statusline_files()),
            sorted([self.profile_path, self.root_path]),
        )
        result = helper.load_statusline_usage(now=after)
        self.assertEqual(result["five_hour"]["utilization"], 12.5)
        self.assertEqual(result["seven_day"]["utilization"], 9)
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.claude")}
        ):
            root_result = helper.load_statusline_usage(now=after)
        self.assertEqual(root_result["seven_day"]["utilization"], 9)

    def test_scoped_default_or_scoped_state_dir_writes_only_once(self) -> None:
        real_save = helper.save_statusline_usage
        windows = self._windows(int(time.time()))
        expected_path = self.root_path
        state_dir = self._tmp.name
        with (
            self.subTest(claude_config_dir="~/.claude"),
            mock.patch.dict(
                os.environ,
                {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.claude")},
            ),
            mock.patch(
                "claude_max_usage.save_statusline_usage", side_effect=real_save
            ) as save,
        ):
            rc, stdout = self._run_scoped(windows)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout), windows["weekly_scoped"])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.kwargs.get("path"), expected_path)
        self.assertEqual(self._statusline_files(), [expected_path])
        self.assertFalse(os.path.exists(os.path.join(state_dir, "profiles")))
        os.unlink(expected_path)

        already_scoped = os.path.join(state_dir, "profiles", ".claude-account6")
        expected_path = os.path.join(already_scoped, "claude-statusline.json")
        with (
            self.subTest(state_dir="already profile-scoped"),
            mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": already_scoped}),
            mock.patch(
                "claude_max_usage.save_statusline_usage", side_effect=real_save
            ) as save,
        ):
            rc, stdout = self._run_scoped(windows)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout), windows["weekly_scoped"])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.kwargs.get("path"), expected_path)
        self.assertEqual(self._statusline_files(), [expected_path])

    def test_scoped_survives_statusline_write_oserror(self) -> None:
        windows = self._windows(int(time.time()))
        with (
            mock.patch(
                "claude_max_usage.read_oauth_token", return_value=SYNTHETIC_TOKEN
            ),
            mock.patch("claude_max_usage.fetch_usage", return_value=windows),
            mock.patch(
                "claude_max_usage.save_statusline_usage",
                side_effect=OSError("status-line cache write failed"),
            ),
            mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
        ):
            rc = helper.main(["--scoped"])

        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), json.dumps(windows["weekly_scoped"]))
        self.assertEqual(self._statusline_files(), [])
        self.assertTrue(
            os.path.exists(helper.scoped_cache_path()),
            "scoped-limit cache must still be written",
        )

    def test_scoped_root_write_failure_keeps_profile_mirror(self) -> None:
        # A directory occupying the root cache path makes os.replace fail
        # with a real OSError after the temp file is written.
        os.makedirs(self.root_path)
        windows = self._windows(int(time.time()))
        rc, stdout = self._run_scoped(windows)

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.strip(), json.dumps(windows["weekly_scoped"]))
        self.assertTrue(os.path.isfile(self.profile_path))
        self.assertTrue(os.path.isdir(self.root_path))
        leftovers = [
            name
            for name in os.listdir(self._tmp.name)
            if name.startswith(f".{helper.STATUSLINE_CACHE_FILENAME}.")
        ]
        self.assertEqual(leftovers, [])


class SaveStatuslineUsageTests(unittest.TestCase):
    """Unit tests for the atomic status-line cache writer."""

    CAPTURED_AT = 1_789_000_000

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "claude-statusline.json")

    def _valid_windows(self) -> dict:
        return {
            "five_hour": {
                "utilization": 12.5,
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 3600),
            },
            "seven_day": {
                "utilization": 9,
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
            },
        }

    def _read_document(self) -> dict:
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_round_trips_with_mode_0600_and_epoch_resets(self) -> None:
        written = helper.save_statusline_usage(
            self._valid_windows(), self.CAPTURED_AT, path=self.path
        )

        self.assertTrue(written)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(
            self._read_document(),
            {
                "captured_at": self.CAPTURED_AT,
                "five_hour": {
                    "utilization": 12.5,
                    "resets_at": self.CAPTURED_AT + 3600,
                },
                "seven_day": {"utilization": 9, "resets_at": self.CAPTURED_AT + 86400},
            },
        )

    def test_default_path_targets_the_statusline_cache_path(self) -> None:
        with mock.patch.dict(os.environ, _state_env(self._tmp.name)):
            written = helper.save_statusline_usage(
                self._valid_windows(), self.CAPTURED_AT
            )
            self.assertEqual(
                helper.statusline_cache_path(),
                os.path.join(self._tmp.name, "claude-statusline.json"),
            )

        self.assertTrue(written)
        self.assertTrue(os.path.isfile(self.path))

    def test_monotonic_guard_refuses_regression_allows_equal_or_newer(self) -> None:
        windows = self._valid_windows()
        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )

        self.assertFalse(
            helper.save_statusline_usage(windows, self.CAPTURED_AT - 1, path=self.path)
        )
        self.assertEqual(self._read_document()["captured_at"], self.CAPTURED_AT)

        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        self.assertEqual(self._read_document()["captured_at"], self.CAPTURED_AT)

        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT + 60, path=self.path)
        )
        self.assertEqual(self._read_document()["captured_at"], self.CAPTURED_AT + 60)

    def test_monotonic_guard_ignores_malformed_or_non_finite_caches(self) -> None:
        windows = self._valid_windows()
        for content in (
            "{not json",
            "[]",
            '{"captured_at": "soon"}',
            '{"captured_at": NaN}',
            '{"captured_at": null}',
        ):
            with (
                self.subTest(content=content),
                open(self.path, "w", encoding="utf-8") as handle,
            ):
                handle.write(content)
            self.assertTrue(
                helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
            )
            self.assertEqual(self._read_document()["captured_at"], self.CAPTURED_AT)

    def test_monotonic_guard_honours_finite_float_newer_timestamp(self) -> None:
        windows = self._valid_windows()
        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"captured_at": self.CAPTURED_AT + 0.5}))

        self.assertFalse(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        self.assertEqual(self._read_document()["captured_at"], self.CAPTURED_AT + 0.5)

    def test_drops_expired_or_invalid_five_hour_but_saves_seven_day(self) -> None:
        far_ahead = self.CAPTURED_AT + helper.MAX_RESET_AHEAD_SECS + 3600
        invalid_five_hour = (
            None,
            "not-a-window",
            {},
            {"utilization": 5},
            {"resets_at": _iso_timestamp(self.CAPTURED_AT + 600)},
            {"utilization": 5, "resets_at": 12345},
            {"utilization": 5, "resets_at": "not-a-timestamp"},
            {"utilization": 5, "resets_at": _iso_timestamp(self.CAPTURED_AT - 1)},
            {"utilization": 5, "resets_at": _iso_timestamp(self.CAPTURED_AT)},
            {"utilization": 5, "resets_at": _iso_timestamp(far_ahead)},
            {"utilization": 200, "resets_at": _iso_timestamp(self.CAPTURED_AT + 600)},
            {"utilization": -1, "resets_at": _iso_timestamp(self.CAPTURED_AT + 600)},
            {"utilization": True, "resets_at": _iso_timestamp(self.CAPTURED_AT + 600)},
            {
                "utilization": float("nan"),
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 600),
            },
            {
                "utilization": float("inf"),
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 600),
            },
        )
        for five_hour in invalid_five_hour:
            with self.subTest(five_hour=five_hour):
                windows = {
                    "five_hour": five_hour,
                    "seven_day": {
                        "utilization": 9,
                        "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
                    },
                }
                self.assertTrue(
                    helper.save_statusline_usage(
                        windows, self.CAPTURED_AT, path=self.path
                    )
                )
                document = self._read_document()
                self.assertIsNone(document["five_hour"])
                self.assertEqual(
                    document["seven_day"],
                    {"utilization": 9, "resets_at": self.CAPTURED_AT + 86400},
                )
                os.unlink(self.path)

        windows = {
            "seven_day": {
                "utilization": 9,
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
            }
        }
        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        self.assertIsNone(self._read_document()["five_hour"])

    def test_skips_write_when_seven_day_missing_expired_or_invalid(self) -> None:
        far_ahead = self.CAPTURED_AT + helper.MAX_RESET_AHEAD_SECS + 3600
        invalid_seven_day = (
            None,
            "not-a-window",
            {},
            {"utilization": 9},
            {"resets_at": _iso_timestamp(self.CAPTURED_AT + 86400)},
            {"utilization": 9, "resets_at": 12345},
            {"utilization": 9, "resets_at": "not-a-timestamp"},
            {"utilization": 9, "resets_at": _iso_timestamp(self.CAPTURED_AT - 1)},
            {"utilization": 9, "resets_at": _iso_timestamp(self.CAPTURED_AT)},
            {"utilization": 9, "resets_at": _iso_timestamp(far_ahead)},
            {"utilization": 200, "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400)},
            {"utilization": -1, "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400)},
            {
                "utilization": True,
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
            },
            {
                "utilization": float("nan"),
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
            },
            {
                "utilization": float("inf"),
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 86400),
            },
        )
        five_hour = {
            "utilization": 12.5,
            "resets_at": _iso_timestamp(self.CAPTURED_AT + 3600),
        }
        for seven_day in invalid_seven_day:
            with self.subTest(seven_day=seven_day):
                self.assertFalse(
                    helper.save_statusline_usage(
                        {"five_hour": five_hour, "seven_day": seven_day},
                        self.CAPTURED_AT,
                        path=self.path,
                    )
                )
                self.assertFalse(os.path.exists(self.path))

        self.assertFalse(
            helper.save_statusline_usage(
                {"five_hour": five_hour}, self.CAPTURED_AT, path=self.path
            )
        )
        self.assertFalse(
            helper.save_statusline_usage(
                "not-a-window", self.CAPTURED_AT, path=self.path
            )
        )
        self.assertFalse(os.path.exists(self.path))

    def test_reset_horizon_is_inclusive_at_the_far_edge(self) -> None:
        edge = self.CAPTURED_AT + helper.MAX_RESET_AHEAD_SECS
        windows = {
            "five_hour": {
                "utilization": 12.5,
                "resets_at": _iso_timestamp(self.CAPTURED_AT + 3600),
            },
            "seven_day": {"utilization": 9, "resets_at": _iso_timestamp(edge)},
        }
        self.assertTrue(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        self.assertEqual(self._read_document()["seven_day"]["resets_at"], edge)

        windows["seven_day"]["resets_at"] = _iso_timestamp(edge + 1)
        self.assertFalse(
            helper.save_statusline_usage(windows, self.CAPTURED_AT, path=self.path)
        )
        self.assertEqual(self._read_document()["seven_day"]["resets_at"], edge)

    def test_failed_write_cleans_up_temp_file_and_raises(self) -> None:
        os.makedirs(self.path)
        with self.assertRaises(OSError):
            helper.save_statusline_usage(
                self._valid_windows(), self.CAPTURED_AT, path=self.path
            )
        self.assertEqual(os.listdir(self._tmp.name), ["claude-statusline.json"])


class StaleStatuslineFallbackTests(unittest.TestCase):
    """Bounded 6-hour stale fallback for the default usage path."""

    def setUp(self) -> None:
        self.NOW = int(time.time())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, _state_env(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self._tmp.name, "claude-statusline.json")

    def _write_cache(
        self, captured_at: int, five_hour: object, seven_day: object
    ) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "captured_at": captured_at,
                    "five_hour": five_hour,
                    "seven_day": seven_day,
                },
                handle,
            )

    def _live_five_hour(self) -> dict:
        return {"utilization": 42, "resets_at": self.NOW + 1800}

    def _live_seven_day(self) -> dict:
        return {"utilization": 80, "resets_at": self.NOW + 86400}

    def test_stale_cache_within_six_hours_is_returned_marked_stale(self) -> None:
        self._write_cache(
            self.NOW - 45 * 60,
            {"utilization": 42, "resets_at": self.NOW - 1},
            self._live_seven_day(),
        )
        result = helper.load_stale_statusline_usage(now=self.NOW)
        self.assertEqual(
            result,
            {
                "five_hour": None,
                "seven_day": {
                    "utilization": 80,
                    "resets_at": _iso_timestamp(self.NOW + 86400),
                },
                "stale": True,
                "stale_age_seconds": 45 * 60,
            },
        )

        self._write_cache(
            self.NOW - 6 * 3600, self._live_five_hour(), self._live_seven_day()
        )
        result = helper.load_stale_statusline_usage(now=self.NOW)
        self.assertEqual(result["stale"], True)
        self.assertEqual(result["stale_age_seconds"], 6 * 3600)
        self.assertEqual(result["five_hour"]["utilization"], 42)

        self._write_cache(self.NOW + 30, self._live_five_hour(), self._live_seven_day())
        result = helper.load_stale_statusline_usage(now=self.NOW)
        self.assertEqual(result["stale"], True)
        self.assertEqual(result["stale_age_seconds"], 0)

    def test_stale_loader_rejects_too_old_or_invalid_caches(self) -> None:
        cases = (
            ("just past six hours", self.NOW - 6 * 3600 - 1),
            ("far in the future", self.NOW + 120),
        )
        for label, captured_at in cases:
            with (
                self.subTest(label=label),
            ):
                self._write_cache(
                    captured_at, self._live_five_hour(), self._live_seven_day()
                )
                self.assertIsNone(helper.load_stale_statusline_usage(now=self.NOW))

        for label, seven_day in (
            ("seven_day expired", {"utilization": 80, "resets_at": self.NOW - 1}),
            ("seven_day invalid", {"utilization": 200, "resets_at": self.NOW + 1}),
            ("seven_day missing", None),
        ):
            with self.subTest(label=label):
                self._write_cache(self.NOW - 45 * 60, None, seven_day)
                self.assertIsNone(helper.load_stale_statusline_usage(now=self.NOW))

        os.unlink(self.path)
        self.assertIsNone(helper.load_stale_statusline_usage(now=self.NOW))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(helper.load_stale_statusline_usage(now=self.NOW))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[]")
        self.assertIsNone(helper.load_stale_statusline_usage(now=self.NOW))

    def test_main_returns_stale_cache_when_live_fetch_fails(self) -> None:
        self._write_cache(
            int(time.time()) - 45 * 60,
            {"utilization": 42, "resets_at": self.NOW - 1},
            self._live_seven_day(),
        )
        with (
            mock.patch(
                "claude_max_usage.read_oauth_token",
                side_effect=helper.UsageHelperError(
                    "cannot open credentials file: FileNotFoundError"
                ),
            ),
            mock.patch(
                "claude_max_usage.fetch_usage",
                side_effect=AssertionError("network access must not happen"),
            ),
            mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
            mock.patch("sys.stderr", new_callable=lambda: io.StringIO()) as err,
        ):
            rc = helper.main([])

        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")
        payload = json.loads(out.getvalue())
        self.assertIs(payload["stale"], True)
        self.assertTrue(45 * 60 <= payload["stale_age_seconds"] <= 45 * 60 + 5)
        self.assertIsNone(payload["five_hour"])
        self.assertEqual(payload["seven_day"]["utilization"], 80)

        usage = quota.parse_claude_usage(payload, self.NOW)
        self.assertTrue(usage.source_stale)
        self.assertEqual(usage.fetched_at, self.NOW - payload["stale_age_seconds"])
        self.assertEqual(usage.weekly.used_percent, 80)
        self.assertEqual(usage.weekly.remaining_percent, 20)
        self.assertEqual(usage.weekly.resets_at, self.NOW + 86400)
        self.assertIsNone(usage.session)
        self.assertIsNone(usage.sonnet)

    def test_main_fails_cleanly_when_fallback_is_unavailable(self) -> None:
        for label, setup in (
            (
                "cache older than six hours",
                lambda: self._write_cache(
                    int(time.time()) - 7 * 3600,
                    self._live_five_hour(),
                    self._live_seven_day(),
                ),
            ),
            ("cache entirely missing", lambda: None),
            (
                "seven_day expired",
                lambda: self._write_cache(
                    int(time.time()) - 45 * 60,
                    self._live_five_hour(),
                    {"utilization": 80, "resets_at": self.NOW - 1},
                ),
            ),
        ):
            with self.subTest(label=label):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                setup()
                with (
                    mock.patch(
                        "claude_max_usage.read_oauth_token",
                        side_effect=helper.UsageHelperError(
                            "Claude OAuth token has expired; sign in again"
                        ),
                    ),
                    mock.patch(
                        "claude_max_usage.fetch_usage",
                        side_effect=AssertionError("network access must not happen"),
                    ),
                    mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out,
                    mock.patch("sys.stderr", new_callable=lambda: io.StringIO()) as err,
                ):
                    rc = helper.main([])
                self.assertEqual(rc, 1)
                self.assertIn("claude usage error:", err.getvalue())
                self.assertNotIn(SYNTHETIC_TOKEN, err.getvalue())
                self.assertEqual(out.getvalue(), "")

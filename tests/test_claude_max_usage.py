import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import claude_max_usage as helper

NOW_MS = 1_700_000_000_000
SYNTHETIC_TOKEN = "synthetic-oauth-token-for-tests"


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


if __name__ == "__main__":
    unittest.main()

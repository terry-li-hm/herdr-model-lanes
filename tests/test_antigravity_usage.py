import io
import json
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import antigravity_usage as helper

SYNTHETIC_CSRF = "synthetic-csrf-token-for-tests"
FIXTURES = Path("tests/fixtures/antigravity")
APP_PS = (
    "42389 /Applications/Antigravity.app/Contents/Resources/bin/language_server "
    f"--csrf_token {SYNTHETIC_CSRF} --app_data_dir antigravity --https_server_port 0\n"
)
IDE_PS = (
    "50001 /Applications/Antigravity IDE.app/language_server "
    f"--csrf_token {SYNTHETIC_CSRF} --app_data_dir antigravity-ide\n"
)
AGY_PS = "61001 /Users/terry/.local/bin/agy --print-timeout 10m0s\n"
LSOF = "language_ 42389 terry 7u IPv4 0x1 0t0 TCP 127.0.0.1:63842 (LISTEN)\n"


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


class ParseTests(unittest.TestCase):
    def test_accepts_weekly_gemini_fixture(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-summary-weekly.json").read_text(encoding="utf-8")
        )

        window = helper.parse_quota_summary(payload)

        self.assertEqual(
            window,
            {
                "gemini": {
                    "used_percent": 58,
                    "remaining_percent": 42,
                    "resets_at": "2026-08-29T10:10:41Z",
                    "window_seconds": helper.WEEKLY_WINDOW_SECONDS,
                }
            },
        )

    def test_picks_most_constrained_gemini_bucket(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-summary-nested.json").read_text(encoding="utf-8")
        )

        window = helper.parse_quota_summary(payload)

        self.assertEqual(window["gemini"]["remaining_percent"], 80)
        self.assertEqual(
            window["gemini"]["window_seconds"], helper.FIVE_HOUR_WINDOW_SECONDS
        )
        self.assertEqual(window["gemini"]["resets_at"], "2026-08-22T14:10:41Z")

    def test_rejects_missing_gemini_group(self) -> None:
        with self.assertRaisesRegex(helper.AntigravityUsageError, "Gemini"):
            helper.parse_quota_summary({"response": {"groups": []}})

    def test_rejects_out_of_range_fraction(self) -> None:
        payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "remainingFraction": 1.5,
                                "resetTime": "2026-08-29T10:10:41Z",
                            }
                        ],
                    }
                ]
            }
        }
        with self.assertRaisesRegex(helper.AntigravityUsageError, "Gemini"):
            helper.parse_quota_summary(payload)


class DiscoveryTests(unittest.TestCase):
    def test_discovers_app_language_server_and_skips_ide(self) -> None:
        servers = helper.discover_servers(
            ps_output=APP_PS + IDE_PS,
            lsof_for_pid=lambda _pid: LSOF,
        )

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].kind, "app")
        self.assertEqual(servers[0].port, 63842)
        self.assertEqual(servers[0].csrf_token, SYNTHETIC_CSRF)

    def test_discovers_running_agy_without_csrf(self) -> None:
        servers = helper.discover_servers(
            ps_output=AGY_PS,
            lsof_for_pid=lambda _pid: LSOF,
        )

        self.assertEqual(servers[0].kind, "cli")
        self.assertIsNone(servers[0].csrf_token)

    def test_probe_available_is_true_for_app_server(self) -> None:
        self.assertTrue(
            helper.probe_available(ps_output=APP_PS, lsof_for_pid=lambda _pid: LSOF)
        )
        self.assertFalse(
            helper.probe_available(ps_output=IDE_PS, lsof_for_pid=lambda _pid: LSOF)
        )

    def test_skips_app_server_without_csrf(self) -> None:
        line = (
            "42389 /Applications/Antigravity.app/bin/language_server "
            "--app_data_dir antigravity\n"
        )
        servers = helper.discover_servers(
            ps_output=line, lsof_for_pid=lambda _pid: LSOF
        )
        self.assertEqual(servers, [])


class FetchTests(unittest.TestCase):
    def test_posts_csrf_and_returns_normalized_window(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-summary-weekly.json").read_text(encoding="utf-8")
        )
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["csrf"] = next(
                (
                    value
                    for _name, value in request.header_items()
                    if "csrf" in _name.lower()
                ),
                None,
            )
            captured["timeout"] = timeout
            return FakeResponse(payload)

        server = helper.LocalServer(42389, 63842, SYNTHETIC_CSRF, "app")
        window = helper.fetch_quota(server, opener=opener)

        self.assertEqual(window["gemini"]["remaining_percent"], 42)
        self.assertIn("127.0.0.1:63842", str(captured["url"]))
        self.assertEqual(captured["csrf"], SYNTHETIC_CSRF)

    def test_errors_do_not_echo_csrf(self) -> None:
        def opener(request, timeout):
            raise URLError("timed out")

        server = helper.LocalServer(42389, 63842, SYNTHETIC_CSRF, "app")
        with self.assertRaises(helper.AntigravityUsageError) as raised:
            helper.fetch_quota(server, opener=opener)

        self.assertNotIn(SYNTHETIC_CSRF, str(raised.exception))

    def test_refuses_non_loopback_url(self) -> None:
        request = urllib.request.Request("https://example.invalid/quota")
        with self.assertRaisesRegex(helper.AntigravityUsageError, "non-loopback"):
            helper._open(request, 1)


class MainTests(unittest.TestCase):
    def test_main_prints_json_and_never_csrf(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-summary-weekly.json").read_text(encoding="utf-8")
        )
        server = helper.LocalServer(42389, 63842, SYNTHETIC_CSRF, "app")
        with (
            mock.patch.object(helper, "discover_servers", return_value=[server]),
            mock.patch.object(
                helper, "fetch_quota", return_value=helper.parse_quota_summary(payload)
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = helper.main()

        self.assertEqual(rc, 0)
        self.assertNotIn(SYNTHETIC_CSRF, stdout.getvalue())
        self.assertNotIn(SYNTHETIC_CSRF, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["gemini"]["remaining_percent"], 42
        )


if __name__ == "__main__":
    unittest.main()

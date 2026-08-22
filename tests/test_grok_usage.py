import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import grok_usage as helper

SYNTHETIC_KEY = "synthetic-grok-key-for-tests"
FIXTURES = Path("tests/fixtures/grok")


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


class AuthParsingTests(unittest.TestCase):
    def test_reads_only_key_of_first_auth_host_entry(self) -> None:
        payload = json.dumps(
            {
                "https://auth.x.ai::123": {
                    "key": SYNTHETIC_KEY,
                    "refresh": "other-secret",
                    "email": "terry@example.invalid",
                }
            }
        )

        self.assertEqual(helper.parse_auth_payload(payload), SYNTHETIC_KEY)

    def test_missing_auth_file_is_unavailable_without_echoing_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(helper.GrokUsageError) as raised,
        ):
            helper.read_login_key(Path(directory) / "absent.json")

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))

    def test_malformed_auth_is_rejected_without_echoing_key(self) -> None:
        for payload in ("", json.dumps({"https://auth.x.ai": {"nokia": 1}})):
            with self.assertRaises(helper.GrokUsageError) as raised:
                helper.parse_auth_payload(payload)

            self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))


class BillingParsingTests(unittest.TestCase):
    def test_accepts_weekly_fixture_and_normalizes_window(self) -> None:
        payload = json.loads(
            (FIXTURES / "credits-weekly.json").read_text(encoding="utf-8")
        )

        window = helper.parse_billing(payload)

        self.assertEqual(
            window,
            {
                "weekly": {
                    "used_percent": 42,
                    "remaining_percent": 58,
                    "resets_at": "2026-08-22T00:00:00Z",
                }
            },
        )

    def test_rejects_monthly_period_as_not_weekly(self) -> None:
        payload = json.loads(
            (FIXTURES / "credits-monthly.json").read_text(encoding="utf-8")
        )

        with self.assertRaisesRegex(helper.GrokUsageError, "weekly"):
            helper.parse_billing(payload)


class FetchTests(unittest.TestCase):
    def test_sends_key_only_in_headers_and_normalizes_response(self) -> None:
        captured = {}
        payload = json.loads(
            (FIXTURES / "credits-weekly.json").read_text(encoding="utf-8")
        )

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["timeout"] = timeout
            return FakeResponse(payload)

        result = helper.fetch_credits(SYNTHETIC_KEY, timeout=5, opener=opener)

        self.assertEqual(captured["url"], helper.BILLING_URL)
        self.assertEqual(
            captured["headers"]["authorization"], f"Bearer {SYNTHETIC_KEY}"
        )
        self.assertEqual(captured["headers"]["x-xai-token-auth"], "xai-grok-cli")
        self.assertEqual(captured["headers"]["accept"], "application/json")
        self.assertEqual(captured["timeout"], 5)
        self.assertEqual(set(result), {"weekly"})

    def test_network_failure_never_echoes_key(self) -> None:
        def opener(_request, timeout):
            raise URLError(f"network detail {SYNTHETIC_KEY}")

        with self.assertRaises(helper.GrokUsageError) as raised:
            helper.fetch_credits(SYNTHETIC_KEY, opener=opener)

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))

    def test_redirects_are_refused_without_echoing_destination(self) -> None:
        request = urllib.request.Request(
            helper.BILLING_URL,
            headers={"Authorization": f"Bearer {SYNTHETIC_KEY}"},
        )
        handler = helper.RejectRedirectHandler()

        with self.assertRaises(helper.GrokUsageError) as raised:
            handler.redirect_request(
                request, None, 302, "Found", {}, f"https://x.invalid/{SYNTHETIC_KEY}"
            )

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))

    @mock.patch("grok_usage.read_login_key")
    def test_main_prints_credential_free_error_when_auth_missing(
        self, read_key: mock.Mock
    ) -> None:
        import io

        read_key.side_effect = helper.GrokUsageError("cannot read auth file")
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr):
            rc = helper.main()

        self.assertEqual(rc, 1)
        self.assertIn("grok usage error", stderr.getvalue())
        self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

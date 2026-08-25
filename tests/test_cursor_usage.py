import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import cursor_usage as helper

SYNTHETIC_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjE3OTI0MTEyMzUsInN1YiI6InRlc3QifQ."
    "c2ln"
)
FIXTURES = Path("tests/fixtures/cursor")
NOW = 1_787_394_179  # well before SYNTHETIC_JWT exp 1792411235


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


def write_state_db(path: Path, token: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", (helper.TOKEN_KEY, token))
    conn.commit()
    conn.close()


class ParseTests(unittest.TestCase):
    def test_accepts_usage_summary_fixture(self) -> None:
        payload = json.loads((FIXTURES / "usage-summary.json").read_text())

        window = helper.parse_usage_summary(payload)

        self.assertEqual(window["monthly"]["used_percent"], 1)
        self.assertEqual(window["monthly"]["remaining_percent"], 99)
        self.assertEqual(window["monthly"]["window_seconds"], 2_678_400)
        self.assertTrue(
            window["monthly"]["resets_at"].startswith("2026-09-19T06:05:05")
        )

    def test_unlimited_is_full_remaining(self) -> None:
        payload = {
            "billingCycleStart": "2026-08-19T06:05:05.000Z",
            "billingCycleEnd": "2026-09-19T06:05:05.000Z",
            "isUnlimited": True,
        }
        window = helper.parse_usage_summary(payload)
        self.assertEqual(window["monthly"]["remaining_percent"], 100)

    def test_rejects_missing_plan(self) -> None:
        with self.assertRaisesRegex(helper.CursorUsageError, "plan"):
            helper.parse_usage_summary(
                {
                    "billingCycleStart": "2026-08-19T06:05:05.000Z",
                    "billingCycleEnd": "2026-09-19T06:05:05.000Z",
                    "individualUsage": {},
                }
            )


class TokenTests(unittest.TestCase):
    def test_reads_fresh_token_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            write_state_db(path, SYNTHETIC_JWT)
            token = helper.resolve_token(now=NOW, db_path=path)

        self.assertEqual(token, SYNTHETIC_JWT)

    def test_expired_token_is_unavailable(self) -> None:
        expired = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3MDAwMDAwMDB9.c2ln"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            write_state_db(path, expired)
            available = helper.key_available(now=NOW, db_path=path)

        self.assertFalse(available)

    def test_errors_do_not_echo_token(self) -> None:
        with self.assertRaises(helper.CursorUsageError) as raised:
            helper.resolve_token(now=NOW, candidates=())

        self.assertNotIn(SYNTHETIC_JWT, str(raised.exception))


class FetchTests(unittest.TestCase):
    def test_sends_double_colon_cookie_and_normalizes(self) -> None:
        payload = json.loads((FIXTURES / "usage-summary.json").read_text())
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["cookie"] = request.headers.get("Cookie")
            return FakeResponse(payload)

        window = helper.fetch_usage(SYNTHETIC_JWT, opener=opener)

        self.assertEqual(window["monthly"]["remaining_percent"], 99)
        self.assertEqual(
            captured["cookie"], f"WorkosCursorSessionToken=::{SYNTHETIC_JWT}"
        )

    def test_errors_do_not_echo_token(self) -> None:
        def opener(request, timeout):
            raise URLError("timed out")

        with self.assertRaises(helper.CursorUsageError) as raised:
            helper.fetch_usage(SYNTHETIC_JWT, opener=opener)

        self.assertNotIn(SYNTHETIC_JWT, str(raised.exception))


class MainTests(unittest.TestCase):
    def test_main_prints_json_and_never_token(self) -> None:
        payload = json.loads((FIXTURES / "usage-summary.json").read_text())
        with (
            mock.patch.object(helper, "resolve_token", return_value=SYNTHETIC_JWT),
            mock.patch.object(
                helper, "fetch_usage", return_value=helper.parse_usage_summary(payload)
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = helper.main()

        self.assertEqual(rc, 0)
        self.assertNotIn(SYNTHETIC_JWT, stdout.getvalue())
        self.assertNotIn(SYNTHETIC_JWT, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["monthly"]["remaining_percent"], 99
        )


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import kimi_usage as helper

SYNTHETIC_KEY = "synthetic-kimi-code-key-for-tests"
FIXTURES = Path("tests/fixtures/kimi")
NOW = 1_787_394_179


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


@contextlib.contextmanager
def isolated_lookup(
    *, env_resolved: dict[str, str] | None = None, cli: dict | None = None
):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env_path = root / "env.resolved"
        cred_path = root / "kimi-code.json"
        if env_resolved is not None:
            env_path.write_text(
                "".join(f"{name}={value}\n" for name, value in env_resolved.items()),
                encoding="utf-8",
            )
        if cli is not None:
            cred_path.write_text(json.dumps(cli), encoding="utf-8")
        yield env_path, cred_path


class ParseTests(unittest.TestCase):
    def test_equal_windows_prefer_weekly(self) -> None:
        payload = json.loads((FIXTURES / "usages-weekly.json").read_text())

        window = helper.parse_usages(payload)

        self.assertEqual(window["coding"]["remaining_percent"], 100)
        self.assertEqual(
            window["coding"]["window_seconds"], helper.WEEKLY_WINDOW_SECONDS
        )
        self.assertEqual(window["coding"]["resets_at"], "2026-08-28T02:01:19Z")

    def test_picks_tighter_five_hour_window(self) -> None:
        payload = json.loads((FIXTURES / "usages-five-hour-tighter.json").read_text())

        window = helper.parse_usages(payload)

        self.assertEqual(window["coding"]["remaining_percent"], 30)
        self.assertEqual(
            window["coding"]["window_seconds"], helper.FIVE_HOUR_WINDOW_SECONDS
        )

    def test_rejects_missing_usage(self) -> None:
        with self.assertRaisesRegex(helper.KimiUsageError, "usage"):
            helper.parse_usages({"limits": []})


class KeyLookupTests(unittest.TestCase):
    def test_env_beats_files(self) -> None:
        with isolated_lookup(
            env_resolved={"KIMI_CODE_API_KEY": "file-should-lose"},
            cli={"access_token": "cli-should-lose", "expires_at": NOW + 60},
        ) as (env_path, cred_path):
            key = helper.resolve_key(
                env=lambda name: SYNTHETIC_KEY if name == "KIMI_CODE_API_KEY" else "",
                now=NOW,
                env_resolved=env_path,
                credentials_path=cred_path,
            )

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_env_resolved_beats_expired_cli_token(self) -> None:
        with isolated_lookup(
            env_resolved={"KIMI_CODE_API_KEY": SYNTHETIC_KEY},
            cli={"access_token": "expired-cli", "expires_at": NOW - 10},
        ) as (env_path, cred_path):
            key = helper.resolve_key(
                env=lambda _name: "",
                now=NOW,
                env_resolved=env_path,
                credentials_path=cred_path,
            )

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_fresh_cli_token_is_used_when_no_api_key(self) -> None:
        with isolated_lookup(
            cli={"access_token": SYNTHETIC_KEY, "expires_at": NOW + 60}
        ) as (env_path, cred_path):
            key = helper.resolve_key(
                env=lambda _name: "",
                now=NOW,
                env_resolved=env_path,
                credentials_path=cred_path,
            )

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_expired_cli_token_is_unavailable(self) -> None:
        with isolated_lookup(
            cli={"access_token": SYNTHETIC_KEY, "expires_at": NOW - 1}
        ) as (env_path, cred_path):
            available = helper.key_available(
                env=lambda _name: "",
                now=NOW,
                env_resolved=env_path,
                credentials_path=cred_path,
            )

        self.assertFalse(available)

    def test_errors_do_not_echo_key(self) -> None:
        with (
            isolated_lookup() as (env_path, cred_path),
            self.assertRaises(helper.KimiUsageError) as raised,
        ):
            helper.resolve_key(
                env=lambda _name: "",
                now=NOW,
                env_resolved=env_path,
                credentials_path=cred_path,
            )

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))


class FetchTests(unittest.TestCase):
    def test_posts_bearer_and_returns_normalized_window(self) -> None:
        payload = json.loads((FIXTURES / "usages-weekly.json").read_text())
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["auth"] = request.headers.get("Authorization")
            captured["timeout"] = timeout
            return FakeResponse(payload)

        window = helper.fetch_usages(SYNTHETIC_KEY, opener=opener)

        self.assertEqual(window["coding"]["remaining_percent"], 100)
        self.assertEqual(captured["auth"], f"Bearer {SYNTHETIC_KEY}")

    def test_errors_do_not_echo_key(self) -> None:
        def opener(request, timeout):
            raise URLError("timed out")

        with self.assertRaises(helper.KimiUsageError) as raised:
            helper.fetch_usages(SYNTHETIC_KEY, opener=opener)

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))


class MainTests(unittest.TestCase):
    def test_main_prints_json_and_never_key(self) -> None:
        payload = json.loads((FIXTURES / "usages-weekly.json").read_text())
        with (
            mock.patch.object(helper, "resolve_key", return_value=SYNTHETIC_KEY),
            mock.patch.object(
                helper, "fetch_usages", return_value=helper.parse_usages(payload)
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            rc = helper.main()

        self.assertEqual(rc, 0)
        self.assertNotIn(SYNTHETIC_KEY, stdout.getvalue())
        self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())
        self.assertEqual(
            json.loads(stdout.getvalue())["coding"]["remaining_percent"], 100
        )


if __name__ == "__main__":
    unittest.main()

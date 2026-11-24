import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import glm_usage as helper

SYNTHETIC_KEY = "synthetic-glm-key-for-tests"
FIXTURES = Path("tests/fixtures/glm")
ENV_NAMES = helper.ENV_KEY_NAMES


def env_from(mapping: dict) -> dict:
    return lambda name: mapping.get(name, "")


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
    *,
    pi_key: str | None = None,
    env_resolved: dict[str, str] | None = None,
    config_key: str | None = None,
):
    """Point helper file sources at a temp dir. Missing files stay missing."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pi_path = root / "models.json"
        env_path = root / "env.resolved"
        config_path = root / "api_key"
        if pi_key is not None:
            pi_path.write_text(
                json.dumps({"providers": {"bigmodel-coding": {"apiKey": pi_key}}}),
                encoding="utf-8",
            )
        if env_resolved is not None:
            env_path.write_text(
                "".join(f"{name}={value}\n" for name, value in env_resolved.items()),
                encoding="utf-8",
            )
        patches = [
            mock.patch.object(helper, "PI_MODELS_FILENAME", pi_path),
            mock.patch.object(helper, "ENV_RESOLVED_FILENAME", env_path),
        ]
        if config_key is not None:
            config_path.write_text(f"{config_key}\n", encoding="utf-8")
            patches.append(
                mock.patch.object(helper, "CONFIG_KEY_FILES", ((config_path, True),))
            )
        else:
            patches.append(mock.patch.object(helper, "CONFIG_KEY_FILES", ()))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            yield root


class KeyLookupTests(unittest.TestCase):
    def test_env_order_wins_over_files(self) -> None:
        with isolated_lookup(
            pi_key="pi-should-lose",
            env_resolved={"ZHIPU_API_KEY": "file-should-lose"},
        ):
            key, base = helper.resolve_key(env_from({"ZAI_KEY": SYNTHETIC_KEY}))

        self.assertEqual(key, SYNTHETIC_KEY)
        self.assertEqual(base, helper.ZAI_BASE)

    def test_bigmodel_env_names_use_bigmodel_base(self) -> None:
        for name in ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"):
            with self.subTest(name=name):
                key, base = helper.resolve_key(env_from({name: SYNTHETIC_KEY}))

                self.assertEqual(key, SYNTHETIC_KEY)
                self.assertEqual(base, helper.BIGMODEL_BASE)

    def test_env_beats_later_env_names(self) -> None:
        mapping = {
            name: "later-value"
            for name in (
                "ZAI_KEY",
                "BIGMODEL_API_KEY",
                "GLM_API_KEY",
            )
        }
        mapping["ZAI_API_KEY"] = SYNTHETIC_KEY

        key, _ = helper.resolve_key(env_from(mapping))

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_pi_models_json_is_used_after_env(self) -> None:
        with isolated_lookup(pi_key=SYNTHETIC_KEY):
            key, base = helper.resolve_key(env_from({}))

        self.assertEqual(key, SYNTHETIC_KEY)
        self.assertEqual(base, helper.BIGMODEL_BASE)

    def test_env_resolved_is_used_after_process_env(self) -> None:
        with isolated_lookup(
            pi_key="!sialyl-wrapped-pi-key",
            env_resolved={"ZHIPU_API_KEY": SYNTHETIC_KEY},
        ):
            key, base = helper.resolve_key(env_from({}))

        self.assertEqual(key, SYNTHETIC_KEY)
        self.assertEqual(base, helper.BIGMODEL_BASE)

    def test_process_env_beats_env_resolved(self) -> None:
        with isolated_lookup(env_resolved={"ZHIPU_API_KEY": "file-should-lose"}):
            key, _ = helper.resolve_key(env_from({"ZHIPU_API_KEY": SYNTHETIC_KEY}))

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_quoted_env_resolved_value_is_unquoted(self) -> None:
        with isolated_lookup() as root:
            env_path = root / "quoted.env"
            env_path.write_text(f'ZHIPU_API_KEY="{SYNTHETIC_KEY}"\n', encoding="utf-8")
            with mock.patch.object(helper, "ENV_RESOLVED_FILENAME", env_path):
                key, _ = helper.resolve_key(env_from({}))

        self.assertEqual(key, SYNTHETIC_KEY)

    def test_dollar_prefixed_pi_key_is_treated_as_missing(self) -> None:
        with (
            isolated_lookup(pi_key="$ZHIPU_API_KEY"),
            self.assertRaises(helper.GlmUsageError),
        ):
            helper.resolve_key(env_from({}))

    def test_bang_prefixed_pi_key_falls_through_to_config(self) -> None:
        with isolated_lookup(pi_key="!sialyl-wrapped-pi-key", config_key=SYNTHETIC_KEY):
            key, base = helper.resolve_key(env_from({}))

        self.assertEqual(key, SYNTHETIC_KEY)
        self.assertEqual(base, helper.BIGMODEL_BASE)

    def test_config_key_file_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "api_key"
            key_file.write_text(f"{SYNTHETIC_KEY}\nsecond-line\n", encoding="utf-8")
            with (
                mock.patch.object(helper, "PI_MODELS_FILENAME", Path(directory) / "m"),
                mock.patch.object(
                    helper, "ENV_RESOLVED_FILENAME", Path(directory) / "env"
                ),
                mock.patch.object(
                    helper,
                    "CONFIG_KEY_FILES",
                    ((key_file, True),),
                ),
            ):
                key, base = helper.resolve_key(env_from({}))

        self.assertEqual(key, SYNTHETIC_KEY)
        self.assertEqual(base, helper.BIGMODEL_BASE)

    def test_missing_key_everywhere_is_unavailable(self) -> None:
        with (
            isolated_lookup(),
            self.assertRaisesRegex(helper.GlmUsageError, "no GLM API key"),
        ):
            helper.resolve_key(env_from({}))


@contextlib.contextmanager
def recording_opener():
    payload = json.loads((FIXTURES / "quota-healthy.json").read_text(encoding="utf-8"))
    with mock.patch("glm_usage.urllib.request.build_opener") as build:
        opener = build.return_value.open
        opener.return_value = FakeResponse(payload)
        yield opener


class RealmBindingTests(unittest.TestCase):
    realms = (("ZAI_KEY", helper.ZAI_BASE), ("BIGMODEL_API_KEY", helper.BIGMODEL_BASE))

    def test_key_only_sent_to_discovered_realm(self) -> None:
        for name, base in self.realms:
            for override in (None, "", base, base + "/"):
                env = {name: SYNTHETIC_KEY}
                if override is not None:
                    env["MODEL_LANES_GLM_BASE"] = override
                with (
                    self.subTest(name=name, override=override),
                    isolated_lookup(),
                    recording_opener() as opener,
                ):
                    key, resolved = helper.resolve_key(env.get)
                    self.assertEqual(resolved, base)
                    helper.fetch_quota(key, resolved)
                    opener.assert_called_once()
                    request = opener.call_args.args[0]
                    self.assertEqual(request.full_url, base + helper.QUOTA_PATH)
                    self.assertIsNone(request.data)
                    headers = dict(request.header_items())
                    self.assertEqual(
                        headers.pop("Authorization"), f"Bearer {SYNTHETIC_KEY}"
                    )
                    self.assertNotIn(SYNTHETIC_KEY, str(headers))

    def test_unapproved_overrides_fail_without_opening_or_leaking(self) -> None:
        for name, base in self.realms:
            other = (
                helper.ZAI_BASE
                if base == helper.BIGMODEL_BASE
                else helper.BIGMODEL_BASE
            )
            for override in (
                other,
                base.replace("https:", "http:"),
                "https://proxy.invalid",
                base + "/path",
                base + "?q=1",
                base + "#fragment",
                base.replace("://", "://u@"),
                base + ":8443",
                base + "//",
                " " + base,
            ):
                with (
                    self.subTest(name=name, override=override),
                    isolated_lookup(),
                    mock.patch.dict(
                        os.environ,
                        {name: SYNTHETIC_KEY, "MODEL_LANES_GLM_BASE": override},
                        clear=True,
                    ),
                    recording_opener() as opener,
                    contextlib.redirect_stdout(io.StringIO()) as stdout,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    with self.assertRaises(helper.GlmUsageError) as raised:
                        helper.resolve_key()
                    self.assertNotEqual(helper.main(), 0)
                    opener.assert_not_called()
                    output = (
                        str(raised.exception) + stdout.getvalue() + stderr.getvalue()
                    )
                    self.assertNotIn(override, output)
                    self.assertNotIn(SYNTHETIC_KEY, output)

    def test_direct_fetch_rejects_unapproved_base(self) -> None:
        base = "https://proxy.invalid"
        with (
            recording_opener() as opener,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(helper.GlmUsageError) as raised,
        ):
            helper.fetch_quota(SYNTHETIC_KEY, base)
        opener.assert_not_called()
        output = str(raised.exception) + stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(base, output)
        self.assertNotIn(SYNTHETIC_KEY, output)

    def test_direct_fetch_normalizes_single_slash(self) -> None:
        for _, base in self.realms:
            with self.subTest(base=base), recording_opener() as opener:
                helper.fetch_quota(SYNTHETIC_KEY, base + "/")
                opener.assert_called_once()
                self.assertEqual(
                    opener.call_args.args[0].full_url, base + helper.QUOTA_PATH
                )


class ParsingTests(unittest.TestCase):
    def test_healthy_fixture_normalizes_five_hour_window(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-healthy.json").read_text(encoding="utf-8")
        )

        window = helper.parse_quota(payload)

        self.assertEqual(
            window,
            {
                "five_hour": {
                    "used_percent": 38,
                    "remaining_percent": 62,
                    "resets_at": 1_787_356_800,
                }
            },
        )

    def test_credit_only_fixture_is_rejected(self) -> None:
        payload = json.loads(
            (FIXTURES / "quota-credit-only.json").read_text(encoding="utf-8")
        )

        with self.assertRaisesRegex(helper.GlmUsageError, "TOKENS_LIMIT"):
            helper.parse_quota(payload)

    def test_success_false_is_rejected(self) -> None:
        with self.assertRaisesRegex(helper.GlmUsageError, "success false"):
            helper.parse_quota({"success": False, "data": {"limits": []}})

    def test_computes_percentage_from_usage_when_absent(self) -> None:
        payload = {
            "success": True,
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "usage": 250,
                        "remaining": 750,
                        "nextResetTime": 1_787_356_800_000,
                    }
                ]
            },
        }

        window = helper.parse_quota(payload)

        self.assertEqual(window["five_hour"]["used_percent"], 25)
        self.assertEqual(window["five_hour"]["remaining_percent"], 75)

    def test_nearest_next_reset_time_wins(self) -> None:
        payload = {
            "success": True,
            "data": {
                "limits": [
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 90,
                        "nextResetTime": 1_787_443_200_000,
                    },
                    {
                        "type": "TOKENS_LIMIT",
                        "percentage": 10,
                        "nextResetTime": 1_787_356_800_000,
                    },
                ]
            },
        }

        window = helper.parse_quota(payload)

        self.assertEqual(window["five_hour"]["used_percent"], 10)


class FetchTests(unittest.TestCase):
    def test_sends_key_only_in_headers_and_normalizes_response(self) -> None:
        captured = {}
        payload = json.loads(
            (FIXTURES / "quota-healthy.json").read_text(encoding="utf-8")
        )

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {
                key.lower(): value for key, value in request.header_items()
            }
            captured["timeout"] = timeout
            return FakeResponse(payload)

        result = helper.fetch_quota(
            SYNTHETIC_KEY, base=helper.BIGMODEL_BASE, timeout=5, opener=opener
        )

        self.assertEqual(captured["url"], f"{helper.BIGMODEL_BASE}{helper.QUOTA_PATH}")
        self.assertEqual(
            captured["headers"]["authorization"], f"Bearer {SYNTHETIC_KEY}"
        )
        self.assertEqual(captured["headers"]["accept"], "application/json")
        self.assertEqual(captured["timeout"], 5)
        self.assertEqual(set(result), {"five_hour"})

    def test_network_failure_never_echoes_key(self) -> None:
        def opener(_request, timeout):
            raise URLError(f"network detail {SYNTHETIC_KEY}")

        with self.assertRaises(helper.GlmUsageError) as raised:
            helper.fetch_quota(SYNTHETIC_KEY, opener=opener)

        self.assertNotIn(SYNTHETIC_KEY, str(raised.exception))

    @mock.patch("glm_usage.resolve_key")
    def test_main_prints_credential_free_error_when_key_missing(
        self, resolve_key: mock.Mock
    ) -> None:
        resolve_key.side_effect = helper.GlmUsageError("no GLM API key found")
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr):
            rc = helper.main()

        self.assertEqual(rc, 1)
        self.assertIn("glm usage error", stderr.getvalue())
        self.assertNotIn(SYNTHETIC_KEY, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

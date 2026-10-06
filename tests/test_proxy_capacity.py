import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_capacity as p

NOW = 1_800_000_000


def cw(used, reset=NOW + 1000):
    return {"utilization": used, "resets_at": reset}


def codex(used=10, **extra):
    return {
        "rate_limit": {
            "allowed": True,
            "primary_window": {"used_percent": used, "reset_at": NOW + 1000},
        },
        **extra,
    }


class ParserTests(unittest.TestCase):
    def test_claude_shared_pool_blocks_every_model(self):
        body = {"five_hour": cw(100), "seven_day": cw(20)}
        for model in ("claude-fable", "claude-sonnet", "claude-opus"):
            self.assertEqual(
                p.parse_usage("claude", body, model, NOW)["state"], "exhausted"
            )

    def test_model_specific_and_unrecognized_claude_pools(self):
        body = {"five_hour": cw(0), "seven_day": cw(20), "seven_day_sonnet": cw(100)}
        self.assertEqual(
            p.parse_usage("claude", body, "claude-sonnet", NOW)["state"], "exhausted"
        )
        self.assertEqual(
            p.parse_usage("claude", body, "claude-opus", NOW)["remaining_percent"], 80
        )
        breakdown = {"rows": [{"key": "claude_code", "percent": 100}]}
        body["seven_day_breakdown"] = breakdown
        self.assertEqual(
            p.parse_usage("claude", body, "claude-opus", NOW)["remaining_percent"], 80
        )
        body["seven_day_new_pool"] = cw(100)
        self.assertEqual(
            p.parse_usage("claude", body, "claude-opus", NOW)["state"], "exhausted"
        )

    def test_codex_model_availability_and_additional_shared_limits(self):
        body = codex(
            model_usage={"gpt-astra": {"available": False, "available_at": NOW + 500}}
        )
        self.assertEqual(
            p.parse_usage("codex", body, "gpt-astra", NOW)["state"], "exhausted"
        )
        self.assertEqual(p.parse_usage("codex", body, "gpt-sol", NOW)["state"], "ok")
        body["additional_rate_limits"] = [
            {"limit_name": "gpt-sol", "rate_limit": codex(100)["rate_limit"]}
        ]
        self.assertEqual(
            p.parse_usage("codex", body, "gpt-sol", NOW)["state"], "exhausted"
        )
        body["additional_rate_limits"][0]["limit_name"] = "premium"
        self.assertEqual(
            p.parse_usage("codex", body, "gpt-other", NOW)["state"], "exhausted"
        )

    def test_shared_codex_limit_cannot_be_evaded_by_model(self):
        for model in ("gpt-astra", "gpt-sol"):
            self.assertEqual(
                p.parse_usage("codex", codex(100), model, NOW)["state"], "exhausted"
            )

    def test_missing_malformed_stale_and_nonfinite_windows(self):
        for used in (None, True, "12", float("nan"), -1):
            self.assertEqual(
                p.parse_usage("codex", codex(used), "gpt-sol", NOW)["state"], "unknown"
            )
        body = {"five_hour": cw(20, NOW - 1), "seven_day": cw(0)}
        self.assertEqual(p.parse_usage("claude", body, "claude", NOW)["state"], "stale")
        body["seven_day"] = cw(100)
        self.assertEqual(
            p.parse_usage("claude", body, "claude", NOW)["state"], "exhausted"
        )

    def test_credential_and_model_cooldowns_auth_errors(self):
        account = {
            "cooldowns": [
                {
                    "scope": "model",
                    "model_key": "gpt-astra",
                    "reason": "quota",
                    "retry_at": NOW + 1000,
                }
            ]
        }
        self.assertEqual(p.restriction(account, "gpt-astra", NOW)["state"], "exhausted")
        self.assertIsNone(p.restriction(account, "gpt-sol", NOW))
        account["cooldowns"][0]["scope"] = "credential"
        self.assertEqual(p.restriction(account, "gpt-sol", NOW)["state"], "exhausted")
        account["cooldowns"][0]["reason"] = "unauthorized"
        self.assertEqual(p.restriction(account, "gpt-sol", NOW)["state"], "auth_error")
        account["disabled"] = True
        self.assertEqual(p.restriction(account, "gpt-sol", NOW)["state"], "disabled")


class CollectionTests(unittest.TestCase):
    def client(self):
        client = mock.Mock()
        accounts = [
            {
                "auth_index": "disabled-secret",
                "name": "private",
                "provider": "codex",
                "disabled": True,
            },
            {
                "auth_index": "a",
                "name": "private-a",
                "provider": "codex",
                "cooldowns": [
                    {"scope": "credential", "reason": "quota", "retry_at": NOW + 1000}
                ],
            },
            {"auth_index": "b", "name": "private-b", "provider": "codex"},
        ]
        client.request.side_effect = lambda path: (
            {"files": accounts}
            if path == "/auth-files"
            else {"models": [{"id": "gpt-sol"}]}
        )
        client.usage.return_value = codex(20)
        return client

    def test_disabled_duplicate_unsupported_and_multiple_accounts(self):
        client = self.client()
        capacity = p.collect(
            client, [("codex", "gpt-sol"), ("codex", "gpt-absent")], NOW
        )
        self.assertEqual(
            [a["state"] for a in capacity["codex/gpt-sol"]], ["exhausted", "ok"]
        )
        self.assertEqual(capacity["codex/gpt-absent"], [])
        client.usage.assert_called_once()
        self.assertNotIn("private", json.dumps(capacity))
        self.assertNotIn("disabled-secret", str(client.request.call_args_list))
        self.assertTrue(
            all("usage" not in call.args[0] for call in client.request.call_args_list)
        )

    def test_failed_quota_read_distinguished_from_exhaustion(self):
        for state in ("auth_error", "unavailable", "unknown"):
            client = self.client()
            client.usage.side_effect = p.CapacityError(state)
            self.assertEqual(
                p.collect(client, [("codex", "gpt-sol")], NOW)["codex/gpt-sol"][-1][
                    "state"
                ],
                state,
            )

    def test_cache_expiry_failed_refresh_no_stale_healthy_fallback(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(p, "Client", return_value=self.client()) as factory,
        ):
            config = {"management_url": "http://localhost", "cache_seconds": 30}
            root = Path(directory)
            first = p.snapshot(config, [("codex", "gpt-sol")], root, now=NOW)
            cached = p.snapshot(config, [("codex", "gpt-sol")], root, now=NOW + 10)
            self.assertEqual(first, cached)
            self.assertEqual(factory.call_count, 1)
            factory.return_value.request.side_effect = p.CapacityError("auth_error")
            failed = p.snapshot(config, [("codex", "gpt-sol")], root, now=NOW + 31)
            self.assertEqual(
                failed["capacity"]["codex/gpt-sol"][0]["state"], "auth_error"
            )
            self.assertEqual(
                next(root.glob("proxy-*.json")).stat().st_mode & 0o777, 0o600
            )
            self.assertNotIn("private", next(root.glob("proxy-*.json")).read_text())

    def test_reset_invalidates_even_fresh_cache(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(p, "Client", return_value=self.client()) as factory,
        ):
            config = {"management_url": "http://localhost", "cache_seconds": 60}
            p.snapshot(config, [("codex", "gpt-sol")], Path(directory), now=NOW + 990)
            p.snapshot(config, [("codex", "gpt-sol")], Path(directory), now=NOW + 1001)
            self.assertEqual(factory.call_count, 2)

    def test_usage_cache_is_shared_across_lane_sets_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client()
            client.request.side_effect = lambda path: (
                {
                    "files": [
                        {"auth_index": "b", "name": "private-b", "provider": "codex"}
                    ]
                }
                if path == "/auth-files"
                else {"models": [{"id": "gpt-sol"}, {"id": "gpt-luna"}]}
            )
            cache = p.UsageCache(Path(directory), fresh=300, stale=1800)
            p.collect(client, [("codex", "gpt-sol")], NOW, cache)
            other = p.collect(client, [("codex", "gpt-luna")], NOW + 10, cache)
            self.assertEqual(other["codex/gpt-luna"][0]["state"], "ok")
            client.usage.assert_called_once()
            self.assertNotIn(
                "private", "".join(f.read_text() for f in Path(directory).iterdir())
            )

            client.usage.side_effect = p.CapacityError("unavailable")
            throttled = p.collect(client, [("codex", "gpt-sol")], NOW + 600, cache)
            self.assertEqual(throttled["codex/gpt-sol"][0]["remaining_percent"], 80)
            past_reset = p.collect(client, [("codex", "gpt-sol")], NOW + 1000, cache)
            self.assertEqual(past_reset["codex/gpt-sol"][0]["state"], "unavailable")

            client.usage.side_effect = p.CapacityError("auth_error")
            denied = p.collect(client, [("codex", "gpt-sol")], NOW + 600, cache)
            self.assertEqual(denied["codex/gpt-sol"][0]["state"], "auth_error")

            client.usage.side_effect = p.CapacityError("unavailable")
            forced = p.UsageCache(Path(directory), fresh=300, stale=1800, force=True)
            reset = p.collect(client, [("codex", "gpt-sol")], NOW + 10, forced)
            self.assertEqual(reset["codex/gpt-sol"][0]["state"], "unavailable")

    def test_usage_rechecks_sooner_as_an_account_nears_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = p.UsageCache(Path(directory), fresh=1800)
            for used, reuse, recheck in (
                (40, 1799, 1800),
                (70, 599, 600),
                (85, 299, 300),
            ):
                cache.put(
                    "claude", "a", {"m": p.result("ok", NOW + 9000, 100 - used)}, NOW
                )
                self.assertIsNotNone(cache.get("claude", "a", ["m"], NOW + reuse))
                self.assertIsNone(cache.get("claude", "a", ["m"], NOW + recheck))

    def test_concurrent_launches_share_one_refresh(self):
        import threading

        with tempfile.TemporaryDirectory() as directory:
            cache = p.UsageCache(Path(directory))
            client = self.client()
            held, release = threading.Event(), threading.Event()

            def other_launch():
                with cache.lock("codex", "b"):
                    held.set()
                    release.wait(5)
                    cache.put(
                        "codex", "b", {"gpt-sol": p.result("ok", NOW + 9000, 70)}, NOW
                    )

            thread = threading.Thread(target=other_launch)
            thread.start()
            held.wait(5)
            threading.Timer(0.3, release.set).start()
            capacity = p.collect(client, [("codex", "gpt-sol")], NOW, cache)
            thread.join()
            self.assertEqual(capacity["codex/gpt-sol"][-1]["remaining_percent"], 70)
            client.usage.assert_not_called()

    def test_proxy_cooldown_blocks_despite_a_healthy_cached_reading(self):
        # A session that hit its limit through the proxy leaves a cooldown; the
        # relaunch must see it now, not when the usage reading expires.
        with tempfile.TemporaryDirectory() as directory:
            cache = p.UsageCache(Path(directory))
            cache.put("codex", "a", {"gpt-sol": p.result("ok", NOW + 9000, 90)}, NOW)
            capacity = p.collect(self.client(), [("codex", "gpt-sol")], NOW + 10, cache)
            self.assertEqual(capacity["codex/gpt-sol"][0]["state"], "exhausted")

    def test_relaunch_right_after_a_launch_sees_a_new_cooldown(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(p, "Client", return_value=self.client()) as factory,
        ):
            config = {"management_url": "http://localhost"}
            lanes = [("codex", "gpt-sol")]
            first = p.snapshot(config, lanes, Path(directory), now=NOW)
            self.assertEqual(first["capacity"]["codex/gpt-sol"][1]["state"], "ok")
            accounts = factory.return_value.request("/auth-files")["files"]
            accounts[2]["cooldowns"] = [
                {
                    "scope": "credential",
                    "reason": "credential_quota",
                    "retry_at": NOW + 500,
                }
            ]
            again = p.snapshot(config, lanes, Path(directory), now=NOW + 5)
            self.assertEqual(
                again["capacity"]["codex/gpt-sol"][1]["state"], "exhausted"
            )
            factory.return_value.usage.assert_called_once()

    def test_cooldown_reason_from_the_proxy_and_expiry(self):
        account = {
            "cooldowns": [
                {
                    "scope": "credential",
                    "reason": "credential_quota",
                    "retry_at": NOW + 60,
                }
            ]
        }
        blocked = p.restriction(account, "claude-opus", NOW)
        self.assertEqual(
            (blocked["state"], blocked["reset_at"]), ("exhausted", NOW + 60)
        )
        self.assertIsNone(p.restriction(account, "claude-opus", NOW + 60))
        account["cooldowns"][0]["reason"] = "request_error"
        self.assertEqual(
            p.restriction(account, "claude-opus", NOW)["state"], "unavailable"
        )

    def test_corrupt_usage_reading_is_reread(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = p.UsageCache(Path(directory))
            client = self.client()
            p.collect(client, [("codex", "gpt-sol")], NOW, cache)
            for f in Path(directory).glob("usage-*.json"):
                f.write_text("{not json")
            capacity = p.collect(client, [("codex", "gpt-sol")], NOW + 10, cache)
            self.assertEqual(capacity["codex/gpt-sol"][1]["state"], "ok")
            self.assertEqual(client.usage.call_count, 2)

    def test_a_stuck_lock_holder_never_blocks_a_launch_forever(self):
        import threading
        import time

        with tempfile.TemporaryDirectory() as directory:
            cache = p.UsageCache(Path(directory))
            held, release = threading.Event(), threading.Event()

            def stuck():
                with cache.lock("codex", "b"):
                    held.set()
                    release.wait(5)

            thread = threading.Thread(target=stuck)
            thread.start()
            held.wait(5)
            started = time.monotonic()
            with cache.lock("codex", "b", wait=0.3):
                waited = time.monotonic() - started
            release.set()
            thread.join()
            self.assertGreaterEqual(waited, 0.3)
            self.assertLess(waited, 2)

    def test_key_file_permissions_and_never_source_dotenv(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / "key.env"
            path.write_text(
                'CLIPROXYAPI_MANAGEMENT_KEY="private-token"\nOTHER=$(exit 99)\n'
            )
            path.chmod(0o600)
            self.assertEqual(p.read_key({"key_file": str(path)}), "private-token")
            path.chmod(0o644)
            with self.assertRaisesRegex(p.CapacityError, "insecure_key_file"):
                p.read_key({"key_file": str(path)})

    def test_reject_remote_plaintext_url_and_redirect(self):
        with self.assertRaisesRegex(p.CapacityError, "insecure_management_url"):
            p.Client({"management_url": "http://remote.example"})
        with self.assertRaises(p.CapacityError):
            p.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://elsewhere.example"
            )

    def test_usage_request_has_only_placeholder_and_official_endpoint(self):
        with mock.patch.dict(os.environ, {"CLIPROXYAPI_MANAGEMENT_KEY": "secret"}):
            client = p.Client({"management_url": "http://127.0.0.1:8317"})
        client.request = mock.Mock(
            return_value={"status_code": 200, "body": json.dumps(codex())}
        )
        client.usage(
            {"auth_index": "opaque", "id_token": {"chatgpt_account_id": "account"}},
            "codex",
        )
        path, payload = client.request.call_args.args
        self.assertEqual(path, "/api-call")
        self.assertEqual(payload["header"]["Authorization"], "Bearer $TOKEN$")
        self.assertEqual(payload["header"]["ChatGPT-Account-Id"], "account")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(payload["method"], "GET")


class TransportTests(unittest.TestCase):
    def client(self):
        with mock.patch.dict(
            os.environ, {"CLIPROXYAPI_MANAGEMENT_KEY": "do-not-print"}
        ):
            return p.Client({"management_url": "http://127.0.0.1:8317"})

    def test_http_auth_error_does_not_expose_body_or_credentials(self):
        import io
        import urllib.error

        client = self.client()
        client.opener.open = mock.Mock(
            side_effect=urllib.error.HTTPError(
                "http://private",
                401,
                "secret error message",
                {},
                io.BytesIO(b"secret upstream response"),
            )
        )
        with self.assertRaises(p.CapacityError) as caught:
            client.request("/auth-files")
        self.assertEqual(str(caught.exception), "auth_error")
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer do-not-print")
        self.assertEqual(
            request.full_url, "http://127.0.0.1:8317/v0/management/auth-files"
        )

    def test_query_budget_stops_new_requests(self):
        client = self.client()
        client.deadline = 0
        client.opener.open = mock.Mock()
        with self.assertRaises(p.CapacityError):
            client.request("/auth-files")
        client.opener.open.assert_not_called()

    def test_usage_endpoint_throttling_is_unknown_not_inference_exhaustion(self):
        client = self.client()
        client.request = mock.Mock(
            return_value={"status_code": 429, "body": "private error"}
        )
        with self.assertRaisesRegex(p.CapacityError, "unavailable"):
            client.usage({"auth_index": "opaque"}, "claude")

    def test_overspent_window_is_exhausted(self):
        self.assertEqual(
            p.parse_usage("codex", codex(101), "gpt-sol", NOW)["state"], "exhausted"
        )

    def test_malformed_optional_limits_fail_closed_but_keep_depletion(self):
        for value in (False, 3, "invalid", {"premium": False}):
            for field in ("additional_rate_limits", "model_usage"):
                payload = (
                    {"gpt-sol": False}
                    if field == "model_usage" and isinstance(value, dict)
                    else value
                )
                body = codex(**{field: payload})
                self.assertEqual(
                    p.parse_usage("codex", body, "gpt-sol", NOW)["state"], "unknown"
                )
                body["rate_limit"]["primary_window"]["used_percent"] = 100
                self.assertEqual(
                    p.parse_usage("codex", body, "gpt-sol", NOW)["state"], "exhausted"
                )

    def test_past_reset_with_exhaustion_flag_requires_recheck(self):
        body = codex(100)
        body["rate_limit"]["limit_reached"] = True
        body["rate_limit"]["primary_window"]["reset_at"] = NOW - 1
        self.assertEqual(p.parse_usage("codex", body, "gpt-sol", NOW)["state"], "stale")

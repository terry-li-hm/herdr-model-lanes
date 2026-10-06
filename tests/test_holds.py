import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import holds as h
import proxy_capacity as p
import role_launch as role

NOW = 1_800_000_000


def lane(name, kind, model):
    return {"name": name, "kind": kind, "model": model, "command": [kind], "args": []}


def ok(remaining=90):
    return p.result("ok", NOW + 1000, remaining)


class HoldFileTests(unittest.TestCase):
    def test_missing_file_is_no_holds_and_save_is_private(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nested" / "holds.json"
            self.assertEqual(h.load(path), [])
            h.save(path, [{"provider": "codex", "until": None}])
            self.assertEqual(h.load(path), [{"provider": "codex", "until": None}])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_malformed_file_is_an_error_not_silently_no_holds(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "holds.json"
            path.write_text(json.dumps({"holds": "codex"}))
            with self.assertRaises(ValueError):
                h.load(path)

    def test_expiry_is_lazy(self):
        holds = [{"provider": "codex", "until": NOW + 10}, {"model": "claude-fable"}]
        self.assertEqual(len(h.active(holds, NOW)), 2)
        self.assertEqual(h.active(holds, NOW + 10), [{"model": "claude-fable"}])

    def test_matching(self):
        model = {"model": "claude-fable"}
        provider = {"provider": "codex"}
        account = {"provider": "codex", "account": "codex-a.json"}
        self.assertTrue(h.blocks_lane(model, "claude", "claude-fable-5-1"))
        self.assertFalse(h.blocks_lane(model, "claude", "claude-opus-5-5"))
        self.assertTrue(h.blocks_lane(provider, "codex", "gpt-6.1-sol"))
        self.assertFalse(h.blocks_lane(provider, "claude", "claude-opus-5-5"))
        self.assertFalse(h.blocks_lane(account, "codex", "gpt-6.1-sol"))
        self.assertEqual(h.account_hold([account], "codex", "codex-a.json"), account)
        self.assertIsNone(h.account_hold([account], "claude", "codex-a.json"))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.lanes = [
            lane("fable-high", "claude", "claude-fable-5-1"),
            lane("sol-xhigh", "codex", "gpt-6.1-sol"),
            lane("opus-xhigh", "claude", "claude-opus-5-5"),
        ]
        self.capacity = {
            "claude/claude-fable-5-1": [ok()],
            "codex/gpt-6.1-sol": [ok()],
            "claude/claude-opus-5-5": [ok()],
        }

    def test_model_and_provider_holds_skip_lanes_visibly(self):
        holds = [
            {"model": "claude-fable", "until": None, "reason": "off"},
            {"provider": "codex", "until": NOW + 50},
        ]
        selected, decisions = role.choose(
            self.lanes, self.capacity, holds=holds, now=NOW
        )
        self.assertEqual(selected["name"], "opus-xhigh")
        self.assertEqual([d["state"] for d in decisions], ["held", "held", "eligible"])
        self.assertEqual(decisions[0]["hold"], "claude-fable")
        self.assertEqual(decisions[0]["reason"], "off")
        self.assertEqual(decisions[1]["retry_at"], NOW + 50)

    def test_expired_hold_lifts_at_the_next_selection(self):
        holds = [{"provider": "codex", "until": NOW + 50}, {"model": "claude-fable"}]
        selected, _ = role.choose(self.lanes, self.capacity, holds=holds, now=NOW + 50)
        self.assertEqual(selected["name"], "sol-xhigh")

    def test_fully_held_accounts_hold_the_lane_partially_held_do_not(self):
        self.capacity["claude/claude-fable-5-1"] = [p.result("held", NOW + 9)]
        selected, decisions = role.choose(self.lanes, self.capacity, now=NOW)
        self.assertEqual(decisions[0]["state"], "held")
        self.assertEqual(selected["name"], "sol-xhigh")
        self.capacity["claude/claude-fable-5-1"] = [p.result("held", NOW + 9), ok()]
        self.assertEqual(
            role.choose(self.lanes, self.capacity)[0]["name"], "fable-high"
        )

    def test_every_lane_held_selects_nothing(self):
        holds = [{"provider": "claude"}, {"provider": "codex"}]
        selected, decisions = role.choose(self.lanes, self.capacity, holds=holds)
        self.assertIsNone(selected)
        self.assertEqual({d["state"] for d in decisions}, {"held"})


class CollectionTests(unittest.TestCase):
    def test_held_account_is_never_read(self):
        accounts = [
            {"auth_index": "a", "name": "codex-a.json", "provider": "codex"},
            {"auth_index": "b", "name": "codex-b.json", "provider": "codex"},
        ]
        client = mock.Mock()
        client.request.side_effect = lambda path: (
            {"files": accounts}
            if path == "/auth-files"
            else {"models": [{"id": "gpt-sol"}]}
        )
        client.usage.return_value = {
            "rate_limit": {
                "allowed": True,
                "primary_window": {"used_percent": 10, "reset_at": NOW + 1000},
            }
        }
        holds = [{"provider": "codex", "account": "codex-a.json", "until": NOW + 5}]
        capacity = p.collect(client, [("codex", "gpt-sol")], NOW, holds=holds)
        self.assertEqual(
            [a["state"] for a in capacity["codex/gpt-sol"]], ["held", "ok"]
        )
        client.usage.assert_called_once()
        self.assertNotIn("codex-a", json.dumps(capacity))
        lifted = p.collect(client, [("codex", "gpt-sol")], NOW + 5, holds=holds)
        self.assertEqual([a["state"] for a in lifted["codex/gpt-sol"]], ["ok", "ok"])


if __name__ == "__main__":
    unittest.main()

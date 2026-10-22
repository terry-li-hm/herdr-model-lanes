"""Offline contract and fault-injection tests. No Herdr/native/credential access.

The fake CLI speaks the staged result envelopes and raw terminal output. The OS
fake models /proc independently of Herdr's claims. RPC tests use anonymous pipes,
not a real Codex process. All task disclosure tests use synthetic canaries.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quota_handoff as qh

NOW = 1800000000
SID = "12345678-1234-1234-1234-123456789abc"
PI_SID = "12345678-1234-1234-1234-123456789def"
NOTICE = "  ⚠ Usage limit reached · limit resets 4:40pm"
PI_FRIENDLY = "You have hit your ChatGPT usage limit (pro plan). Try again in ~30 min."
LIVE = NOTICE + "\nContinuing automatically at 4:40pm · esc to cancel · /usage-credits to continue now\n──────\n❯ \nOpus 5.5 · c\n? for shortcuts\n"
AUTHORITY = {"owner": "Terry", "provenance": "offline owner policy revision 1"}
OAUTH = {"status": "ready", "provider": "openai-codex", "authType": "oauth"}
CANARY = "PRIVATE-GLOBAL-HOOK-CONTEXT-NOT-FOR-DESTINATION"


def window(used=10, reset=NOW + 3600):
    return {"utilization": used, "resets_at": reset}


def claude_payload(used=10, scoped=None, **changes):
    return {"five_hour": window(used), "seven_day": window(used), "weekly_scoped": scoped, **changes}


def codex_window(used=12):
    return {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": NOW + 3600}


def snapshot(used=12, **changes):
    return {"limitId": "codex", "primary": codex_window(used), "secondary": None,
            "spendControlReached": False, "planType": "pro", "rateLimitReachedType": None, **changes}


def codex_payload(used=12):
    snap = snapshot(used)
    return {"account": {"account": {"type": "chatgpt", "planType": "pro"},
                        "requiresOpenaiAuth": True, "authMethod": None},
            "limits": {"ordinaryUsageAllowed": True, "rateLimits": snap,
                       "rateLimitsByLimitId": {"codex": copy.deepcopy(snap)}}}


def iso(epoch=NOW):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def native_events():
    return [{"type": "user", "sessionId": SID, "uuid": "user-task-1", "timestamp": iso(NOW - 15),
             "message": {"role": "user", "content": CANARY}},
            {"type": "assistant", "sessionId": SID, "timestamp": iso(),
             "isApiErrorMessage": True, "error": "rate_limit", "apiError": {"status": 429},
             "quotaLimits": {}, "message": {"role": "assistant", "content": [{"type": "text", "text": NOTICE}]}}]


def pi_events(cwd):
    return [{"type": "session", "version": 3, "id": PI_SID, "cwd": cwd, "timestamp": iso(NOW - 60)},
            {"type": "model_change", "provider": "openai-codex", "modelId": "gpt-6.1-sol"},
            {"type": "message", "id": "user-1", "timestamp": iso(NOW - 5),
             "message": {"role": "user", "content": CANARY}},
            {"type": "message", "id": "error-1", "timestamp": iso(),
             "message": {"role": "assistant", "stopReason": "error", "errorMessage": "usage_limit_reached"}}]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def process(pid, pgid=10, native=None, ppid=99, state="S", start=None, uid=None):
    return {"pid": pid, "pgid": pgid, "ppid": ppid, "state": state,
            "start": start or 1000 + pid, "uid": os.getuid() if uid is None else uid,
            "exe": "/fake/" + (native or "bash"), "native": native}


class FakeOs(qh.OsPort):
    def __init__(self, home, config_dir, route="claude", audit=None):
        self.platform = "linux"
        self.home, self.config_dir = str(home), str(config_dir)
        self.processes = {10: process(10), 11: process(11, native=route, ppid=10),
                          99: process(99, pgid=99, ppid=1), 199: process(199, pgid=199, ppid=1)}
        self.envs = {11: {"HOME": self.home, "CLAUDE_CONFIG_DIR": self.config_dir, "overrides": False}}
        self.server = {"pid": 500, "start": 123456, "env": {"HOME": self.home, "overrides": False}}
        self.kills, self.audit = [], audit if audit is not None else []
        self.clock = NOW
        self.stop_effect = True
        self.after_stop = None

    def now(self):
        return self.clock

    def table(self):
        return copy.deepcopy(self.processes)

    def environment(self, pid):
        return copy.deepcopy(self.envs.get(pid))

    def launch_environment(self):
        return copy.deepcopy(self.server)

    def killpg(self, pgid, sig):
        self.kills.append((pgid, sig))
        self.audit.append("STOP")
        if self.stop_effect:
            for p in self.processes.values():
                if p["pgid"] == pgid:
                    p["state"] = "T"
        if self.after_stop:
            self.after_stop()


class FakeQuota(qh.QuotaPort):
    def __init__(self, state):
        super().__init__(state)
        self.claude = {n: claude_payload() for n in qh.CLAUDE_PROFILE_ORDER}
        self.pi, self.codex = copy.deepcopy(OAUTH), codex_payload()
        self.calls = []

    def claude_usage(self, profile, now):
        self.calls.append(copy.deepcopy(profile))
        value = self.claude.get(profile["name"])
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def pi_auth_check(self):
        return copy.deepcopy(self.pi)

    def codex_windows(self, now):
        return copy.deepcopy(self.codex)


class FakeCLI:
    """argv -> rc/stdout, including raw read, real envelopes and start/get identity."""
    def __init__(self, agent, os_port, audit):
        self.source, self.os, self.audit = copy.deepcopy(agent), os_port, audit
        self.agents = [self.source]
        self.workspaces = [{"workspace_id": "ws1", "label": "vivesca"}]
        self.live = LIVE
        self.argvs, self.created, self.started, self.prompts = [], [], [], []
        self.new = None
        self.fail = None
        self.start_changes = {}
        self.get_changes = {}
        self.pane_changes = {}
        self.create_changes = None
        self.multi = False
        self.hook = None

    def __call__(self, argv):
        self.argvs.append(list(argv))
        args = argv[1:]
        command = tuple(args[:2])
        if self.hook:
            self.hook(args)
        if command == ("workspace", "list"):
            return self.ok({"workspaces": self.workspaces})
        if command == ("agent", "list"):
            return self.ok({"agents": self.agents})
        if command == ("agent", "get"):
            value = self.source if args[2] == self.source["pane_id"] else {**self.new, **self.get_changes} if self.new else {}
            return self.ok({"agent": value})
        if command == ("agent", "read"):
            return 0, self.live
        if command == ("pane", "list"):
            if args != ["pane", "list", "--workspace", self.source["workspace_id"]]:
                raise AssertionError(args)
            panes = [{"pane_id": self.source["pane_id"], "tab_id": self.source["tab_id"],
                      "workspace_id": self.source["workspace_id"]}]
            if self.created:
                panes.append({"pane_id": "new-pane", "tab_id": "new-tab", "workspace_id": self.source["workspace_id"]})
            if self.multi:
                panes.append({"pane_id": "unrelated", "tab_id": self.source["tab_id"], "workspace_id": self.source["workspace_id"]})
            return self.ok({"panes": panes})
        if command == ("pane", "process-info"):
            if args != ["pane", "process-info", "--pane", args[-1]]:
                raise AssertionError(args)
            info = ({"shell_pid": 199} if args[-1] == "new-pane" else
                    {"foreground_process_group_id": 10, "shell_pid": 99,
                     "foreground_processes": [{"pid": 10, "command": "bash"}, {"pid": 11, "command": "claude"}]})
            return self.ok({"process_info": info})
        if command == ("tab", "create"):
            self.audit.append("CREATE")
            self.created.append(list(args))
            self.maybe_fail("create")
            env = {"HOME": self.os.home, "overrides": False}
            for i, arg in enumerate(args):
                if arg == "--env":
                    k, _, v = args[i + 1].partition("=")
                    env[k] = v
            self.os.envs[199] = env
            return self.ok(self.create_changes if self.create_changes is not None else
                           {"tab": {"tab_id": "new-tab"}, "root_pane": {"pane_id": "new-pane"}})
        if command == ("pane", "get"):
            return self.ok({"pane": {"pane_id": "new-pane", "tab_id": "new-tab", "cwd": self.source["cwd"],
                                      "workspace_id": self.source["workspace_id"], **self.pane_changes}})
        if command == ("agent", "start"):
            self.audit.append("START")
            self.started.append(list(args))
            self.maybe_fail("start")
            kind = args[args.index("--kind") + 1]
            self.new = {**self.source, "pane_id": "new-pane", "tab_id": "new-tab", "terminal_id": "new-terminal",
                        "agent": kind, "agent_session": {"agent": kind, "kind": "id", "source": kind, "value": "new-session"},
                        "interactive_ready": True, "agent_status": "idle", **self.start_changes}
            return self.ok({"agent": self.new})
        if command == ("agent", "prompt"):
            self.audit.append("PROMPT")
            self.prompts.append(list(args))
            self.maybe_fail("prompt")
            return self.ok({"agent": self.new})
        if command == ("notification", "show"):
            return self.ok({})
        raise AssertionError("unexpected/mutating CLI: " + repr(args))

    def maybe_fail(self, name):
        if self.fail == name:
            raise qh.HandoffError("ambiguous synthetic response")

    @staticmethod
    def ok(result):
        return 0, json.dumps({"result": result})


class Fixture:
    def __init__(self, tmp, route="claude"):
        self.root = Path(tmp)
        self.home, self.cwd = self.root / "home", self.root / "work"
        self.cwd.mkdir()
        self.home.mkdir()
        self.profiles = [{"name": n, "config_dir": str(self.root / ("cfg-" + n))} for n in qh.CLAUDE_PROFILE_ORDER]
        cd = self.profiles[0]["config_dir"]
        self.audit = []
        self.os = FakeOs(self.home, cd, route, self.audit)
        if route == "claude":
            slug = qh.re.sub(r"[^a-zA-Z0-9]", "-", str(self.cwd))
            self.path = Path(cd) / "projects" / slug / (SID + ".jsonl")
            session = {"agent": route, "kind": "id", "source": "native", "value": SID}
            events = native_events()
        else:
            self.path = self.root / ("2027-01-01_" + PI_SID + ".jsonl")
            session = {"agent": route, "kind": "path", "source": "native", "value": str(self.path)}
            events = pi_events(str(self.cwd))
        self.events = events
        self.save_events()
        self.agent = {"agent": route, "pane_id": "source-pane", "tab_id": "source-tab", "terminal_id": "source-terminal",
                      "workspace_id": "ws1", "cwd": str(self.cwd), "agent_status": "blocked", "interactive_ready": True,
                      "agent_session": session, "name": None}
        self.cli = FakeCLI(self.agent, self.os, self.audit)
        self.state = self.root / "state"
        self.quota = FakeQuota(self.state)
        if route == "pi":
            self.quota.codex = codex_payload(100)  # Pi sources require independent official exhaustion proof.
        self.config = qh.HandoffConfig(enabled=True, allowed_workspaces=["ws1"], authority=copy.deepcopy(AUTHORITY),
                                       claude_profiles=self.profiles)
        self.engine = qh.QuotaHandoffEngine(self.config, qh.HerdrPort("fake-herdr", self.cli), self.quota, self.os, self.state)
        save = self.engine.receipts.save

        def saving(key, receipt):
            self.audit.append("SAVE:" + receipt["phase"])
            save(key, receipt)
        self.engine.receipts.save = saving

    def save_events(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(json.dumps(x) for x in self.events) + "\n")

    def event(self):
        return self.engine.handle_event({"pane_id": "source-pane"})

    def sol(self):
        self.quota.claude = {}

    def checkpoint(self, **changes):
        path = self.root / "checkpoint.json"
        write_json(path, {"classification": "unclassified", "authority": AUTHORITY,
                          "checkpoint": "Owner-approved public checkpoint ONLY", **changes})
        return path

    def mark(self):
        self.config.auto_prompt_template = {"authority": AUTHORITY, "text": "Continue from this checkpoint:\n{checkpoint}"}
        return self.engine.mark_public("source-pane", self.checkpoint())

    def receipt(self):
        return self.engine.receipts.load(qh.event_key(self.cli.source))


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def no_control(self, result=None):
        self.assertEqual(self.f.os.kills, [])
        self.assertEqual(self.f.cli.created, [])
        self.assertEqual(self.f.cli.started, [])
        self.assertEqual(self.f.cli.prompts, [])
        if result:
            self.assertFalse(result["mutated"])


class TestDetection(unittest.TestCase):
    def test_observed_contract_notice_and_plan_footer(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "observed-contract.json").read_text())
        self.assertTrue(qh.tail_notice(fixture["terminal_footer"]))
        text = fixture["native_claude_error"]["message"]["content"][0]["text"]
        self.assertTrue(qh.tail_notice(text))
        self.assertTrue(qh.tail_notice("You've hit your weekly limit · resets tomorrow"))
        self.assertFalse(qh.tail_notice('user: "' + text + '"'))

    def test_live_symbol_footer_and_resume(self):
        self.assertTrue(qh.tail_notice(LIVE))
        self.assertTrue(qh.tail_notice(LIVE.replace("⚠", "⚠️")))

    def test_all_anchored_notices(self):
        for text in ("You have hit your limit", "You’ve hit your limit", "You've hit your limit",
                     "Usage limit reached", "You have reached your usage limit", "usage_limit_reached"):
            with self.subTest(text=text):
                self.assertTrue(qh.tail_notice(text))

    def test_quoted_user_shell_text_rejected(self):
        for prefix in ('"', "'", "`", "> ", "$ ", "❯ ", "user: ", "assistant said "):
            with self.subTest(prefix=prefix):
                self.assertFalse(qh.tail_notice(prefix + "Usage limit reached"))

    def test_generic_errors_rejected(self):
        for text in ("API Error 429 rate limit exceeded", "rate_limit", "overloaded", "network failure",
                     "context limit exceeded", "Permission needed", "Too many requests", "timeout"):
            with self.subTest(text=text):
                self.assertFalse(qh.tail_notice(text))

    def test_later_content_not_footer(self):
        for later in ("user follow up", "$ echo shell", "❯ continue", "working on task", "network error"):
            with self.subTest(later=later):
                self.assertFalse(qh.tail_notice(LIVE + later))

    def test_native_auto_mode_footer(self):
        text = (NOTICE + "\nContinuing automatically at 4:40pm · esc to cancel · /usage-credits to continue now"
                + "\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents")
        self.assertTrue(qh.tail_notice(text))
        self.assertFalse(qh.tail_notice(text + "\nuser draft text"))
        self.assertFalse(qh.tail_notice(text + "\n❯ queued shell command"))
        # Footer shape alone never fabricates a notice.
        self.assertFalse(qh.tail_notice("  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"))
        # Near-miss variants stay rejected.
        for near in ("⏵⏵ auto mode on (shift+tab to cycle) · → for agents",
                     "⏵⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
                     "▸▸ auto mode on (shift+tab to cycle) · ← for agentsX"):
            with self.subTest(near=near):
                self.assertFalse(qh.tail_notice(NOTICE + "\n" + near))


class TestQuotaParsing(unittest.TestCase):
    def test_native_iso_and_unused(self):
        cap = qh.validate_claude_payload(claude_payload(five_hour=window(0, None), seven_day=window(35, iso(NOW + 3600))), NOW)
        self.assertTrue(cap["five_hour"]["unused"])
        self.assertEqual(cap["seven_day"]["resets_at"], NOW + 3600)

    def test_invalid_native_windows(self):
        for bad in (window(True), window(float("nan")), window(float("inf")), window(-1), window(100), window(101),
                    window(1, None), window(0, True), window(1, "2027-01-01T00:00:00"), window(1, NOW),
                    window(1, NOW + 400 * 86400), {"utilization": 0}, None):
            with self.subTest(bad=bad):
                self.assertIsNone(qh.validate_claude_payload(claude_payload(five_hour=bad), NOW))

    def test_scoped_probe_required_and_real_shape(self):
        payload = claude_payload()
        payload.pop("weekly_scoped")
        self.assertIsNone(qh.validate_claude_payload(payload, NOW))
        for scoped in ({"opus": window()}, {"scope": "unknown", **window()}, "bad"):
            self.assertIsNone(qh.validate_claude_payload(claude_payload(scoped=scoped), NOW))
        self.assertEqual(qh.validate_claude_payload(claude_payload(scoped={"scope": "Opus", **window(100)}), NOW)["opus_scope"]["utilization"], 100)
        self.assertNotIn("opus_scope", qh.validate_claude_payload(claude_payload(scoped={"scope": "Fable", **window(100)}), NOW))

    def test_other_reported_windows_validated(self):
        self.assertIsNone(qh.validate_claude_payload(claude_payload(seven_day_sonnet=window(True)), NOW))
        self.assertIsNotNone(qh.validate_claude_payload(claude_payload(seven_day_sonnet=window(100)), NOW))

    def test_real_nested_weekly_codex(self):
        cap = qh.validate_codex_snapshot(codex_payload(), NOW)
        self.assertEqual(len(cap), 2)
        self.assertIn("ordinary:primary", cap)

    def test_codex_secondary_all_reported_windows(self):
        payload = codex_payload()
        payload["limits"]["rateLimits"]["secondary"] = codex_window(40)
        self.assertEqual(len(qh.validate_codex_snapshot(payload, NOW)), 3)
        for invalid in ({}, window(), {**codex_window(), "usedPercent": True}, {**codex_window(), "windowDurationMins": False}):
            payload["limits"]["rateLimits"]["secondary"] = invalid
            self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))

    def test_codex_relevant_full_snapshot_buckets(self):
        payload = codex_payload()
        payload["limits"]["rateLimitsByLimitId"]["gpt-6.1-sol"] = snapshot(100)
        self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))
        payload["limits"]["rateLimitsByLimitId"] = {"codex": codex_window()}
        self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))
        payload["limits"]["rateLimitsByLimitId"] = {"grok": snapshot(100)}
        self.assertIsNotNone(qh.validate_codex_snapshot(payload, NOW))

    def test_codex_flags_and_missing_windows(self):
        for key, value in (("spendControlReached", True), ("rateLimitReachedType", "weekly"),
                           ("rateLimitReached", True), ("ordinaryUsageAllowed", False), ("primary", None)):
            payload = codex_payload()
            payload["limits"]["rateLimits"][key] = value
            self.assertIsNone(qh.validate_codex_snapshot(payload, NOW), key)
        payload = codex_payload()
        payload["limits"]["ordinaryUsageAllowed"] = False
        self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))

    def test_codex_only_proven_subscription(self):
        for changes in ({"type": "apiKey"}, {"type": "pro"}, {"planType": None}, {"type": None}):
            payload = codex_payload()
            payload["account"]["account"].update(changes)
            self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))
        for key, value in (("requiresOpenaiAuth", False), ("requiresOpenaiAuth", None), ("authMethod", "apiKey")):
            payload = codex_payload()
            payload["account"][key] = value
            self.assertIsNone(qh.validate_codex_snapshot(payload, NOW))

    def test_pi_auth_exact(self):
        self.assertTrue(qh.validate_pi_auth(OAUTH))
        for payload in ({"ready": True, "oauth": True}, {**OAUTH, "authType": "apiKey"},
                        {**OAUTH, "provider": None}, {**OAUTH, "status": "expired"},
                        {**OAUTH, "provider_override": True}):
            self.assertFalse(qh.validate_pi_auth(payload))


class TestQuotaSelection(FixtureTest):
    def test_healthiest_not_first(self):
        self.f.quota.claude = {"c": claude_payload(98), "c2": claude_payload(52),
                               "c3": claude_payload(five_hour=window(0, None), seven_day=window(35)), "c4": claude_payload(100)}
        self.assertEqual(self.f.engine.choose_destination(NOW)[:2], ("claude", "c3"))
        self.assertEqual([p["name"] for p in self.f.quota.calls], list(qh.CLAUDE_PROFILE_ORDER))

    def test_ties_config_order(self):
        self.f.config.claude_profiles.reverse()
        self.assertEqual(self.f.engine.choose_destination(NOW)[:2], ("claude", "c4"))

    def test_reserve_floor_then_sol(self):
        self.f.quota.claude = {n: claude_payload(96) for n in qh.CLAUDE_PROFILE_ORDER}
        self.assertEqual(self.f.engine.choose_destination(NOW)[:2], ("pi", "sol"))
        self.f.quota.codex = codex_payload(96)
        self.assertIsNone(self.f.engine.choose_destination(NOW))

    def test_exact_five_percent_accepted(self):
        self.f.quota.claude = {"c": claude_payload(95)}
        self.assertEqual(self.f.engine.choose_destination(NOW)[:2], ("claude", "c"))

    def test_opus_cap_not_fable(self):
        self.f.quota.claude["c"] = claude_payload(scoped={"scope": "Opus", **window(100)})
        self.f.quota.claude["c2"] = claude_payload(scoped={"scope": "Fable", **window(100)})
        self.assertEqual(self.f.engine.choose_destination(NOW)[:2], ("claude", "c2"))

    def test_no_capacity_no_control(self):
        self.f.quota.claude = {}
        self.f.quota.codex = codex_payload(100)
        result = self.f.event()
        self.assertEqual(result["status"], "no-available-destination")
        self.no_control(result)

    def test_source_excluded_even_if_stale_healthy(self):
        result = self.f.event()
        self.assertEqual(result["receipt"]["destination"]["profile"], "c2")
        self.assertIn(qh.digest(self.f.profiles[0]["config_dir"]), self.f.engine.exclusions())

    def test_account_exclusion_across_panes(self):
        self.f.engine.exclude_source(self.f.profiles[0]["config_dir"], "claude", NOW)
        other = qh.QuotaHandoffEngine(self.f.config, self.f.engine.herdr, self.f.quota, self.f.os, self.f.state)
        self.assertEqual(other.choose_destination(NOW)[:2], ("claude", "c2"))

    def test_sol_source_known_reset_and_reentry(self):
        self.f.quota.codex = codex_payload(100)
        self.f.quota.codex["limits"]["rateLimits"]["rateLimitReachedType"] = "weekly"
        self.f.engine.exclude_source(None, "pi", NOW)
        self.assertEqual(self.f.engine.exclusions()["sol"]["until"], NOW + 3600)
        self.assertIsNone(qh.validate_codex_snapshot(self.f.quota.codex, NOW))
        self.assertIsNotNone(qh.validate_codex_snapshot(self.f.quota.codex, NOW, exhausted=True))
        self.f.sol()
        self.assertIsNone(self.f.engine.choose_destination(NOW + 10))
        self.f.quota.codex = codex_payload(12)
        for snap in (self.f.quota.codex["limits"]["rateLimits"], self.f.quota.codex["limits"]["rateLimitsByLimitId"]["codex"]):
            snap["primary"]["resetsAt"] = NOW + 7200
        self.assertEqual(self.f.engine.choose_destination(NOW + 3601)[:2], ("pi", "sol"))

    def test_account_known_reset_and_expiry(self):
        self.f.quota.claude["c"] = claude_payload(100)
        self.f.engine.exclude_source(self.f.profiles[0]["config_dir"], "claude", NOW)
        exclusion = self.f.engine.exclusions()[qh.digest(self.f.profiles[0]["config_dir"])]
        self.assertEqual(exclusion["until"], NOW + 3600)
        self.f.quota.claude["c"] = claude_payload(0, five_hour=window(0, None), seven_day=window(0, NOW + 7200))
        self.assertEqual(self.f.engine.choose_destination(NOW + 3601)[:2], ("claude", "c"))

    def test_unknown_source_account_sol_only(self):
        unknown = str(self.f.root / "unknown")
        path = Path(unknown) / "projects" / self.f.path.parent.name / self.f.path.name
        path.parent.mkdir(parents=True)
        path.write_bytes(self.f.path.read_bytes())
        self.f.os.envs[11]["CLAUDE_CONFIG_DIR"] = unknown
        result = self.f.event()
        self.assertEqual(result["receipt"]["destination"]["kind"], "pi")
        self.assertEqual(self.f.quota.calls, [])

    def test_expired_positive_cache_revalidated(self):
        profile = self.f.profiles[1]
        self.assertIsNotNone(self.f.engine.claude_capacity(profile, NOW))
        self.f.os.clock += 10
        path = self.f.engine._cache_path(profile)
        data = json.loads(path.read_text())
        data["payload"]["five_hour"]["resets_at"] = NOW + 5
        write_json(path, data)
        self.assertIsNone(self.f.engine.claude_capacity(profile, NOW + 10))

    def test_positive_cache_ttl_and_configdir_binding(self):
        profile = self.f.profiles[1]
        self.f.engine.claude_capacity(profile, NOW)
        self.f.engine.claude_capacity(profile, NOW + 299)
        self.assertEqual(len(self.f.quota.calls), 1)
        self.f.engine.claude_capacity(profile, NOW + 301)
        self.assertEqual(len(self.f.quota.calls), 2)
        self.f.engine.claude_capacity({**profile, "config_dir": "/other"}, NOW + 302)
        self.assertEqual(len(self.f.quota.calls), 3)

    def test_negative_cache_ttl(self):
        self.f.quota.claude["c2"] = qh.HandoffError("offline")
        for at in (NOW, NOW + 59, NOW + 61):
            self.assertIsNone(self.f.engine.claude_capacity(self.f.profiles[1], at))
        self.assertEqual(len(self.f.quota.calls), 2)

    def test_corrupt_exclusion_refuses_control(self):
        write_json(self.f.state / "exclusions.json", [])
        self.no_control(self.f.event())


class TestTranscript(FixtureTest):
    def proof(self):
        return self.f.engine.source_proof(self.f.cli.source)[4]

    def test_real_native_nested_error(self):
        meta = self.proof()
        self.assertEqual(meta["error"]["timestamp"], NOW)
        self.assertNotIn(CANARY, json.dumps(meta))
        self.assertNotIn(NOTICE, json.dumps(meta))

    def test_native_later_user_supersedes(self):
        self.f.events.append({"type": "user", "sessionId": SID, "message": {"role": "user", "content": "next"}})
        self.f.save_events()
        self.no_control(self.f.event())

    def test_account_on_hold_supersedes_rate_limit_despite_stale_quota_footer(self):
        self.f.events.append({
            "type": "assistant", "sessionId": SID, "timestamp": iso(),
            "isApiErrorMessage": True, "error": "account_on_hold",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Your account is on hold. Contact support."}]},
        })
        self.f.save_events()
        self.f.cli.live = LIVE  # Deliberately stale quota UI, not permission to rotate.
        with patch.object(self.f.quota, "claude_usage", side_effect=AssertionError("hold must not probe another account")), \
                patch.object(self.f.quota, "pi_auth_check", side_effect=AssertionError("hold must not try Sol")):
            result = self.f.event()
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "no-current-source-error")
        self.no_control(result)
        self.assertFalse((self.f.state / "exclusions.json").exists())

    def test_native_later_success_supersedes(self):
        self.f.events.append({"type": "assistant", "sessionId": SID, "message": {"role": "assistant", "content": []}})
        self.f.save_events()
        self.no_control(self.f.event())

    def test_native_flat_imaginary_shape_rejected(self):
        self.f.events = [{"role": "assistant", "timestamp": NOW, "isApiErrorMessage": True, "error": "rate_limit"}]
        self.f.save_events()
        self.no_control(self.f.event())

    def test_native_optional_session_metadata_bound_by_exact_filename(self):
        for event in self.f.events:
            event.pop("sessionId")
        self.f.save_events()
        self.assertIsNotNone(self.proof()["error"])

    def test_sidechain_and_role_mismatch_refused(self):
        self.f.events[-1]["isSidechain"] = True
        self.f.save_events()
        self.no_control(self.f.event())
        self.f.events[-1]["isSidechain"] = False
        self.f.events[-1]["message"]["role"] = "user"
        self.f.save_events()
        self.no_control(self.f.event())

    def test_native_wrong_session_refused(self):
        self.f.events[-1]["sessionId"] = PI_SID
        self.f.save_events()
        self.no_control(self.f.event())

    def test_native_stale_and_future_error_refused(self):
        for at in (NOW - 121, NOW + 6):
            self.f.events[-1]["timestamp"] = iso(at)
            self.f.save_events()
            self.no_control(self.f.event())

    def test_generic_error_not_quota_even_with_ui_notice(self):
        self.f.events[-1]["message"]["content"][0]["text"] = "API Error 429 network overloaded"
        self.f.save_events()
        self.no_control(self.f.event())

    def test_no_recursive_discovery(self):
        self.f.cli.source["agent_session"]["value"] = PI_SID
        self.no_control(self.f.event())
        self.assertTrue(self.f.path.exists())

    def test_exact_native_path_allowed_not_arbitrary_path(self):
        self.f.cli.source["agent_session"].update(kind="path", value=str(self.f.path))
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_wrong_native_path_refused(self):
        other = self.f.root / (SID + ".jsonl")
        other.write_bytes(self.f.path.read_bytes())
        self.f.cli.source["agent_session"].update(kind="path", value=str(other))
        self.no_control(self.f.event())

    def test_malformed_jsonl_refuses_even_with_later_error(self):
        self.f.path.write_text("not-json\n" + self.f.path.read_text())
        self.no_control(self.f.event())

    def test_streamed_transcript_over_eight_mib_supported_and_hashed(self):
        padding = json.dumps({"type": "system", "metadata": "x" * 65536}) + "\n"
        self.f.path.write_text(padding * 130 + self.f.path.read_text())
        meta = self.proof()
        self.assertIsNotNone(meta["error"])
        self.assertEqual(meta["hash"], qh.hashlib.sha256(self.f.path.read_bytes()).hexdigest())
        self.assertNotIn("metadata", meta)

    def test_stream_total_bound_has_fixed_reason(self):
        with patch.object(qh, "MAX_TRANSCRIPT", 10):
            result = self.f.event()
        self.assertEqual(result["reason"], "transcript-size-bound")
        self.no_control(result)

    def test_stream_line_bound_has_fixed_reason(self):
        with patch.object(qh, "MAX_TRANSCRIPT_LINE", 10):
            result = self.f.event()
        self.assertEqual(result["reason"], "transcript-line-bound")
        self.no_control(result)

    def test_stream_deadline_has_fixed_reason(self):
        with patch.object(qh, "TRANSCRIPT_TIMEOUT", 0):
            result = self.f.event()
        self.assertEqual(result["reason"], "transcript-deadline")
        self.no_control(result)

    def test_continued_in_explicitly_unsupported_no_search(self):
        self.f.events.append({"type": "continued-in", "continuedInSessionId": PI_SID})
        self.f.save_events()
        result = self.f.event()
        self.assertEqual(result["reason"], "unsupported-continuation")
        self.no_control(result)

    def test_error_does_not_reactivate_after_later_assistant_failure(self):
        self.f.events.append({"type": "assistant", "sessionId": SID, "timestamp": iso(), "isApiErrorMessage": True,
                              "error": "network", "message": {"role": "assistant", "content": []}})
        self.f.save_events()
        self.no_control(self.f.event())


class TestPiTranscript(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name, "pi")

    def canonical_sdk_error(self):
        self.f.events[-1]["message"]["errorMessage"] = PI_FRIENDLY
        self.f.save_events()
        self.f.cli.live = "Error: " + PI_FRIENDLY + "\n❯\n"

    def assert_no_rotation(self, result):
        self.assertFalse(result["mutated"])
        self.assertEqual(self.f.os.kills, [])
        self.assertEqual(self.f.cli.created, [])
        self.assertEqual(self.f.cli.started, [])
        self.assertEqual(self.f.cli.prompts, [])

    def test_canonical_sdk_and_error_ui_with_official_exhaustion(self):
        self.canonical_sdk_error()
        report = self.f.quota.codex["limits"]
        report["ordinaryUsageAllowed"] = False
        for snap in (report["rateLimits"], report["rateLimitsByLimitId"]["codex"]):
            snap["rateLimitReached"] = True
            snap["rateLimitReachedType"] = "weekly"
        self.assertIsNone(qh.validate_codex_snapshot(self.f.quota.codex, NOW))
        result = self.f.event()
        self.assertEqual(result["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.new["agent"], "claude")
        self.assertEqual(self.f.engine.exclusions()["sol"]["until"], NOW + 3600)
        self.assertEqual(self.f.os.kills, [(10, signal.SIGSTOP)])
        self.assertEqual(self.f.cli.prompts, [])

    def test_same_friendly_generic_429_below100_never_rotates(self):
        self.canonical_sdk_error()
        self.f.quota.codex = codex_payload(99.9)
        result = self.f.event()
        self.assertEqual(result["reason"], "unproven-pi-exhaustion")
        self.assert_no_rotation(result)
        self.assertEqual(self.f.quota.calls, [])

    def test_usage_not_included_or_billing_friendly_text_without100_never_rotates(self):
        self.canonical_sdk_error()
        # The SDK masks these causes with the same friendly sentence.
        for cause in ("usage_not_included", "billing"):
            self.f.quota.codex = codex_payload(12)
            report = self.f.quota.codex["limits"]
            report["ordinaryUsageAllowed"] = False
            report["rateLimits"]["rateLimitReachedType"] = cause
            report["rateLimits"]["spendControlReached"] = cause == "billing"
            self.assert_no_rotation(self.f.event())
        self.assertEqual(self.f.quota.calls, [])

    def test_spend_or_account_restriction_blocks_even100(self):
        self.canonical_sdk_error()
        for cause in ("spend", "usage_not_included", "account_on_hold"):
            self.f.quota.codex = codex_payload(100)
            snap = self.f.quota.codex["limits"]["rateLimits"]
            if cause == "spend":
                snap["spendControlReached"] = True
            else:
                snap["rateLimitReachedType"] = cause
            self.assert_no_rotation(self.f.event())
        self.assertEqual(self.f.quota.calls, [])

    def test_official_proof_rechecked_after_destination_probe(self):
        self.canonical_sdk_error()
        with patch.object(self.f.quota, "codex_windows", side_effect=[codex_payload(100), codex_payload(12)]) as probe:
            result = self.f.event()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(result["reason"], "unproven-pi-exhaustion")
        self.assert_no_rotation(result)

    def test_unknown_or_expired_official_proof_never_rotates(self):
        self.canonical_sdk_error()
        expired = codex_payload(100)
        expired["limits"]["rateLimits"]["primary"]["resetsAt"] = NOW - 1
        for payload in (None, expired):
            self.f.quota.codex = payload
            self.assert_no_rotation(self.f.event())
        self.assertEqual(self.f.quota.calls, [])

    def test_chatgpt_wording_is_pi_only_not_native_corroboration(self):
        for text in (PI_FRIENDLY, "Error: " + PI_FRIENDLY):
            self.assertTrue(qh.tail_notice(text, pi=True))
            self.assertFalse(qh.tail_notice(text))
            self.assertFalse(qh.tail_notice('user: "' + text + '"', pi=True))

    def test_real_pi_approved_route(self):
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.new["agent"], "claude")

    def test_unapproved_latest_model(self):
        for change in ({"provider": "openai"}, {"modelId": "gpt-other"}):
            self.f.events[1] = {**self.f.events[1], **change}
            self.f.save_events()
            self.assertEqual(self.f.event()["status"], "refused")
            self.assertEqual(self.f.os.kills, [])

    def test_pi_success_and_user_supersession(self):
        for message in ({"role": "user", "content": "next"}, {"role": "assistant", "stopReason": "stop"}):
            self.f.events = pi_events(str(self.f.cwd)) + [{"type": "message", "timestamp": iso(), "message": message}]
            self.f.save_events()
            self.assertEqual(self.f.event()["status"], "refused")

    def test_pi_model_change_after_error_supersedes(self):
        self.f.events.append(self.f.events[1])
        self.f.save_events()
        self.assertEqual(self.f.event()["status"], "refused")

    def test_pi_no_model_change_or_wrong_header(self):
        self.f.events = [e for e in self.f.events if e["type"] != "model_change"]
        self.f.save_events()
        self.assertEqual(self.f.event()["status"], "refused")
        self.f.events = pi_events("/other")
        self.f.save_events()
        self.assertEqual(self.f.event()["status"], "refused")

    def test_just_exhausted_sol_not_reused(self):
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "no-available-destination")
        self.assertEqual(self.f.os.kills, [])

    def test_generic_js_runtime_not_agent_proof(self):
        self.f.os.processes[11]["native"] = None
        self.f.os.processes[11]["exe"] = "/usr/bin/node"
        self.assertEqual(self.f.event()["status"], "refused")


class TestEligibility(FixtureTest):
    def test_disabled_zero_calls_no_state(self):
        self.f.config.enabled = False
        self.assertEqual(self.f.event(), {"status": "disabled", "mutated": False})
        self.assertEqual(self.f.engine.scan(), [])
        self.assertEqual(self.f.cli.argvs, [])
        self.assertFalse(self.f.state.exists())

    def test_enabled_requires_owner_and_workspace(self):
        for authority, workspaces in (({}, ["ws1"]), ({"owner": "Terry", "provenance": ""}, ["ws1"]), (AUTHORITY, [])):
            self.f.config.authority, self.f.config.allowed_workspaces = authority, workspaces
            self.assertEqual(self.f.event()["status"], "disabled")
        self.assertEqual(self.f.cli.argvs, [])

    def test_nonlinux_zero_calls(self):
        self.f.os.platform = "darwin"
        self.assertEqual(self.f.event()["status"], "disabled")
        self.assertEqual(self.f.cli.argvs, [])

    def test_workspace_labels_resolved_from_live_list_each_invocation(self):
        self.f.config.allowed_workspaces = ["vivesca"]
        self.assertEqual(self.f.engine.candidates()[0]["workspace_id"], "ws1")
        self.f.cli.workspaces = [{"workspace_id": "ws-after-restart", "label": "vivesca"}]
        self.f.cli.source["workspace_id"] = "ws-after-restart"
        self.assertEqual(self.f.engine.candidates()[0]["workspace_id"], "ws-after-restart")
        calls = [a for a in self.f.cli.argvs if a[1:] == ["workspace", "list"]]
        self.assertEqual(len(calls), 2)

    def test_workspace_id_not_label(self):
        self.f.cli.source["workspace_id"] = "other"
        self.no_control(self.f.event())

    def test_all_named_workers_excluded(self):
        for name in ("worker-1", "researcher", "main", "handoff-x"):
            self.f.cli.source["name"] = name
            self.assertEqual(self.f.engine.candidates(), [])
        self.no_control()

    def test_explicit_test_source_allowlist(self):
        self.f.cli.source["name"] = "researcher"
        self.f.config.test_source_allowlist = ["source-pane"]
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_restricted_native_profile_preserves_empty_argv_values(self):
        restricted = ["--setting-sources", "", "--tools", ""]
        self.f.config.test_source_allowlist = ["source-pane"]
        self.f.config.test_claude_launch_args = restricted
        self.assertTrue(self.f.config.authorized())
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.started[0][-len(restricted):], restricted)

    def test_allowlists_remain_nonempty_strings_and_argv_requires_strings(self):
        for field in ("launch_cwd_allowlist", "test_source_allowlist"):
            setattr(self.f.config, field, [""])
            self.assertFalse(self.f.config.authorized())
            setattr(self.f.config, field, [])
        self.f.config.test_claude_launch_args = ["--tools", None]
        self.assertFalse(self.f.config.authorized())
        self.no_control()

    def test_multipane_refused(self):
        self.f.cli.multi = True
        self.no_control(self.f.event())

    def test_unsupported_agent_route(self):
        self.f.cli.source["agent"] = "codex"
        self.f.cli.source["agent_session"]["agent"] = "codex"
        self.no_control(self.f.event())

    def test_own_event_target_not_skipped(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "source-pane"}):
            self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_event_env_target_fallback(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "source-pane"}):
            self.assertEqual(self.f.engine.handle_event({})["status"], "awaiting-confirmation")

    def test_manual_scan_can_exclude_caller_and_is_read_only(self):
        self.assertEqual(self.f.engine.scan("source-pane"), [])
        self.assertEqual(self.f.engine.scan(), [{"pane_id": "source-pane", "exhausted": True}])
        self.no_control()
        self.assertFalse(self.f.state.exists())

    def test_model_routes_and_reserve_cannot_be_overridden(self):
        for key, value in (("claude_model", "claude-opus-4"), ("pi_provider", "openai"),
                           ("pi_model", "other"), ("minimum_remaining_percent", 0)):
            previous = getattr(self.f.config, key)
            setattr(self.f.config, key, value)
            self.assertEqual(self.f.event()["status"], "disabled")
            setattr(self.f.config, key, previous)
        self.assertEqual(self.f.cli.argvs, [])


class TestParking(FixtureTest):
    def test_park_intent_tokens_before_actual_stop_create_start(self):
        result = self.f.event()
        self.assertEqual(result["status"], "awaiting-confirmation")
        audit = self.f.audit
        self.assertLess(audit.index("SAVE:park-intent"), audit.index("STOP"))
        self.assertLess(audit.index("STOP"), audit.index("CREATE"))
        self.assertLess(audit.index("SAVE:create-intent"), audit.index("CREATE"))
        self.assertLess(audit.index("SAVE:created"), audit.index("START"))
        self.assertLess(audit.index("SAVE:start-intent"), audit.index("START"))
        self.assertEqual(self.f.os.kills, [(10, signal.SIGSTOP)])
        self.assertEqual(result["receipt"]["park"]["members"]["11"]["start"], 1011)

    def test_foreign_uid(self):
        self.f.os.processes[11]["uid"] += 1
        self.no_control(self.f.event())

    def test_actual_wrong_pgid(self):
        self.f.os.processes[11]["pgid"] = 20
        self.no_control(self.f.event())

    def test_shell_inside_foreground_group(self):
        self.f.os.processes[99]["pgid"] = 10
        self.no_control(self.f.event())

    def test_unreported_group_member(self):
        self.f.os.processes[12] = process(12)
        self.no_control(self.f.event())

    def test_outside_descendant_refused(self):
        self.f.os.processes[12] = process(12, pgid=20, ppid=11)
        self.no_control(self.f.event())

    def test_claimed_command_not_native_proof(self):
        self.f.os.processes[11]["native"] = None
        self.no_control(self.f.event())

    def test_transient_rs_does_not_change_identity(self):
        calls = 0
        original = self.f.os.table
        def table():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.f.os.processes[11]["state"] = "R"
            return original()
        self.f.os.table = table
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_starttime_drift_before_stop(self):
        original = self.f.quota.claude_usage
        def quota(profile, now):
            self.f.os.processes[11]["start"] += 1
            return original(profile, now)
        self.f.quota.claude_usage = quota
        self.no_control(self.f.event())

    def test_starttime_drift_after_stop_no_create_no_thaw(self):
        self.f.os.after_stop = lambda: self.f.os.processes[11].update(start=999)
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.created, [])
        self.assertEqual(self.f.os.kills, [(10, signal.SIGSTOP)])
        self.assertEqual(self.f.event()["status"], "duplicate")

    def test_stop_poll_bounded_no_retry_or_thaw(self):
        self.f.os.stop_effect = False
        started = time.monotonic()
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.f.os.kills, [(10, signal.SIGSTOP)])
        self.assertEqual(self.f.cli.created, [])
        self.assertEqual(self.f.event()["status"], "duplicate")

    def test_new_group_member_after_stop(self):
        self.f.os.after_stop = lambda: self.f.os.processes.update({12: process(12, state="T")})
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.created, [])

    def test_member_escapes_pgid_after_stop(self):
        self.f.os.after_stop = lambda: self.f.os.processes[11].update(pgid=20)
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.created, [])

    def test_new_outside_descendant_after_stop(self):
        self.f.os.after_stop = lambda: self.f.os.processes.update({12: process(12, pgid=20, ppid=11)})
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.created, [])

    def test_source_identity_rechecked_after_slow_probe(self):
        original = self.f.quota.claude_usage
        def quota(profile, now):
            self.f.cli.source["terminal_id"] = "other-terminal"
            return original(profile, now)
        self.f.quota.claude_usage = quota
        self.no_control(self.f.event())

    def test_source_error_timestamp_rechecked_after_slow_probe(self):
        original = self.f.quota.claude_usage
        def quota(profile, now):
            self.f.os.clock = NOW + 121
            return original(profile, now)
        self.f.quota.claude_usage = quota
        self.no_control(self.f.event())

    def test_source_new_task_before_stop(self):
        original = self.f.quota.claude_usage
        def quota(profile, now):
            self.f.events.append({"type": "user", "sessionId": SID, "message": {"role": "user", "content": "new task"}})
            self.f.save_events()
            return original(profile, now)
        self.f.quota.claude_usage = quota
        self.no_control(self.f.event())


class TestOwnedLinuxParking(FixtureTest):
    @unittest.skipUnless(sys.platform == "linux", "requires Linux process-group stop semantics")
    def test_synthetic_owned_child_real_stop_verification_and_no_duplicate(self):
        """Only the child is real; native/Herdr/quota metadata are synthetic adapters."""
        fixture = self.f
        real = qh.OsPort()
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import signal; signal.pause()"],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        first = None
        try:
            first = real.process(child.pid)  # Read only this exact owned /proc entry.
            self.assertIsNotNone(first)
            self.assertEqual((first["pid"], first["pgid"], first["uid"]),
                             (child.pid, child.pid, os.getuid()))
            shell_id = child.pid + 1_000_000_000  # Pure adapter metadata, never an OS target.

            def owned_process():
                current = real.process(child.pid)
                if current is None or any(current[k] != first[k] for k in ("pid", "pgid", "uid", "start")):
                    raise qh.HandoffError("owned synthetic child identity drift")
                return current

            class OwnedChildOs(FakeOs):
                def table(self):
                    current = owned_process()
                    # This is NOT a native agent or a report to a live Herdr server.
                    return {child.pid: {**current, "native": "claude"},
                            shell_id: process(shell_id, pgid=shell_id, ppid=1)}

                def killpg(self, pgid, sig):
                    if pgid != child.pid or sig != signal.SIGSTOP:
                        raise AssertionError("signal outside the owned synthetic child")
                    owned_process()  # Bind the actual signal to UID/PGID/PID/starttime.
                    os.killpg(pgid, sig)
                    self.kills.append((pgid, sig))
                    self.audit.append("STOP")

            adapter = OwnedChildOs(fixture.home, fixture.profiles[0]["config_dir"], audit=fixture.audit)
            adapter.envs[child.pid] = copy.deepcopy(adapter.envs[11])
            fixture.engine.os = adapter
            fixture.cli.os = adapter

            def synthetic_cli(argv):
                if argv[1:] == ["pane", "process-info", "--pane", "source-pane"]:
                    fixture.cli.argvs.append(list(argv))
                    return FakeCLI.ok({"process_info": {
                        "foreground_process_group_id": child.pid, "shell_pid": shell_id,
                        "foreground_processes": [{"pid": child.pid, "command": "synthetic-owned-child"}],
                    }})
                return fixture.cli(argv)

            fixture.engine.herdr = qh.HerdrPort("synthetic-herdr-only", synthetic_cli)
            result = fixture.event()  # Existing park-intent -> STOP -> /proc polling path.
            self.assertEqual(result["status"], "awaiting-confirmation")
            self.assertIn(owned_process()["state"], ("T", "t"))
            self.assertEqual(result["receipt"]["park"]["members"][str(child.pid)]["start"], first["start"])
            self.assertEqual(fixture.event()["status"], "duplicate")
            self.assertEqual(adapter.kills, [(child.pid, signal.SIGSTOP)])
            self.assertEqual(len(fixture.cli.created), 1)
            self.assertEqual(len(fixture.cli.started), 1)
            self.assertEqual(fixture.cli.prompts, [])
        finally:
            # Unreaped Popen child ownership prevents PID reuse; never signal a host target.
            if child.poll() is None:
                current = real.process(child.pid)
                if first is not None and current is not None:
                    self.assertEqual((current["start"], current["pgid"], current["uid"]),
                                     (first["start"], child.pid, os.getuid()))
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child.pid, signal.SIGTERM)
                    if current is not None and current["state"] in ("T", "t"):
                        os.kill(child.pid, signal.SIGCONT)
            child.wait(timeout=5)


class TestReceipts(FixtureTest):
    def test_duplicate_exactly_once(self):
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertEqual(len(self.f.cli.started), 1)
        self.assertEqual(len(self.f.os.kills), 1)

    def test_corrupt_empty_and_malformed_receipt_quarantine(self):
        key = qh.event_key(self.f.cli.source)
        path = self.f.engine.receipts.path(key)
        path.parent.mkdir(parents=True)
        for raw in ("", "{", "[]", "{}", '{"phase":"anything"}'):
            path.write_text(raw)
            path.chmod(0o600)
            self.assertEqual(self.f.event()["status"], "quarantined", raw)
        self.no_control()

    def test_every_existing_phase_never_replayed(self):
        key = qh.event_key(self.f.cli.source)
        for phase in ("intent", "selecting", "park-intent", "parked", "create-intent", "created", "start-intent", "started",
                      "prompt-intent", "prompted", "quarantined", "park-failed", "identity-drift", "unknown"):
            self.f.engine.receipts.save(key, {"event_key": key, "phase": phase})
            self.assertEqual(self.f.event()["status"], "duplicate", phase)
        self.no_control()

    def test_global_immutable_nonblocking_lock(self):
        guard = self.f.engine.global_invocation_lock()
        try:
            inode = (self.f.state / "invocation.lock").stat().st_ino
            self.assertEqual(self.f.event()["status"], "busy")
            self.assertEqual((self.f.state / "invocation.lock").stat().st_ino, inode)
            self.no_control()
        finally:
            os.close(guard)

    def test_private_atomic_fsync_receipts_separate_from_lock(self):
        self.f.event()
        path = self.f.engine.receipts.path(qh.event_key(self.f.cli.source))
        lock = self.f.state / "invocation.lock"
        self.assertNotEqual(path.stat().st_ino, lock.stat().st_ino)
        for file in self.f.state.rglob("*"):
            self.assertEqual(file.stat().st_mode & 0o777, 0o700 if file.is_dir() else 0o600, str(file))
        before = path.stat().st_ino
        self.f.engine.receipts.save(qh.event_key(self.f.cli.source), self.f.receipt())
        self.assertNotEqual(path.stat().st_ino, before)

    def test_no_content_or_transcript_path_in_receipts(self):
        self.f.event()
        data = json.dumps(self.f.receipt())
        for banned in (CANARY, NOTICE, str(self.f.path), SID, "accessToken", "checkpoint ONLY"):
            self.assertNotIn(banned, data)

    def test_crash_after_stop_before_state_save_never_replays(self):
        save = self.f.engine.receipts.save
        def crash(key, receipt):
            if receipt["phase"] == "parked":
                raise SystemExit("simulated executor death")
            save(key, receipt)
        self.f.engine.receipts.save = crash
        with self.assertRaises(SystemExit):
            self.f.event()
        self.assertEqual(self.f.receipt()["phase"], "park-intent")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(self.f.cli.created, [])

    def test_crash_after_create_before_id_save_never_replays(self):
        save = self.f.engine.receipts.save
        def crash(key, receipt):
            if receipt["phase"] == "created":
                raise SystemExit("lost create response")
            save(key, receipt)
        self.f.engine.receipts.save = crash
        with self.assertRaises(SystemExit):
            self.f.event()
        self.assertEqual(self.f.receipt()["phase"], "create-intent")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertEqual(self.f.cli.started, [])

    def test_crash_after_start_before_state_save_never_replays(self):
        save = self.f.engine.receipts.save
        def crash(key, receipt):
            if receipt["phase"] == "started":
                raise SystemExit("lost start response")
            save(key, receipt)
        self.f.engine.receipts.save = crash
        with self.assertRaises(SystemExit):
            self.f.event()
        self.assertEqual(self.f.receipt()["phase"], "start-intent")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.started), 1)

    def test_failed_state_save_after_stop_reports_possible_mutation(self):
        save = self.f.engine.receipts.save
        def failed(key, receipt):
            if self.f.os.kills:
                raise OSError("synthetic disk failure")
            save(key, receipt)
        self.f.engine.receipts.save = failed
        result = self.f.event()
        self.assertEqual(result, {"status": "quarantined", "reason": "local-io-failure", "mutated": True})
        self.assertEqual(self.f.receipt()["phase"], "park-intent")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(self.f.cli.created, [])

    def test_failed_intent_save_means_zero_control(self):
        self.f.engine.receipts.save = lambda *a: (_ for _ in ()).throw(OSError("synthetic disk failure"))
        self.no_control(self.f.event())

    def test_reconcile_read_only(self):
        self.f.event()
        before = {p: p.read_bytes() for p in self.f.state.rglob("*") if p.is_file()}
        calls = len(self.f.cli.argvs)
        report = self.f.engine.reconcile("source-pane")
        self.assertEqual(report["receipts"][0]["phase"], "awaiting-confirmation")
        self.assertEqual(before, {p: p.read_bytes() for p in self.f.state.rglob("*") if p.is_file()})
        self.assertEqual(len(self.f.cli.argvs), calls)


class TestStartup(FixtureTest):
    def test_real_cli_argv_and_raw_outputs_in_full_handoff(self):
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        reads = [a for a in self.f.cli.argvs if a[1:3] == ["agent", "read"]]
        self.assertGreaterEqual(len(reads), 3)
        self.assertEqual(reads[0][1:], ["agent", "read", "source-pane", "--source", "recent-unwrapped", "--lines", "30", "--format", "text"])
        create = self.f.cli.created[0]
        self.assertEqual(create[create.index("--cwd") + 1], str(self.f.cwd))
        self.assertEqual(create[create.index("--workspace") + 1], "ws1")
        self.assertIn("--no-focus", create)
        self.assertEqual(create[create.index("--label") + 1], "Continue c2 · source-tab")
        self.assertNotIn("--focus", create)
        self.assertIn("CLAUDE_CONFIG_DIR=" + self.f.profiles[1]["config_dir"], create)
        start = self.f.cli.started[0]
        self.assertEqual(start[start.index("--") + 1:], ["--model", "claude-opus-5-5", "--effort", "high"])
        self.assertNotIn("claude", start[start.index("--") + 1:])
        notifications = [a for a in self.f.cli.argvs if a[1:3] == ["notification", "show"]]
        self.assertEqual(len(notifications), 1)
        self.assertIn("--body", notifications[0])
        body = notifications[0][notifications[0].index("--body") + 1]
        for safe_id in ("new-pane", "new-tab", "source-pane"):
            self.assertIn(safe_id, body)
        self.assertNotIn(CANARY, body)
        self.assertEqual(self.f.cli.prompts, [])

    def test_sol_args_high_and_blank(self):
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        args = self.f.cli.started[0]
        self.assertEqual(args[args.index("--") + 1:], ["--provider", "openai-codex", "--model", "gpt-6.1-sol", "--thinking", "high"])
        create = self.f.cli.created[0]
        self.assertEqual(create[create.index("--label") + 1], "Continue sol · source-tab")
        self.assertEqual(self.f.cli.prompts, [])
        self.assertFalse(any(CANARY in a for argv in self.f.cli.argvs for a in argv))

    def test_ambiguous_create_no_replay(self):
        self.f.cli.fail = "create"
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertEqual(self.f.cli.started, [])

    def test_ambiguous_start_no_replay(self):
        self.f.cli.fail = "start"
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.started), 1)

    def test_malformed_create_envelope_quarantine(self):
        self.f.cli.create_changes = {"root_pane": [], "tab": {"tab_id": "new-tab"}}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.started, [])

    def test_preexisting_pane_id_from_create_never_started(self):
        self.f.cli.create_changes = {"root_pane": {"pane_id": "source-pane"}, "tab": {"tab_id": "source-tab"}}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.started, [])

    def test_missing_create_ids_quarantine(self):
        self.f.cli.create_changes = {}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.started, [])

    def test_destination_wrong_cwd(self):
        self.f.cli.pane_changes = {"cwd": "/elsewhere"}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.started, [])
        self.assertEqual(self.f.receipt()["destination"]["pane_id"], "new-pane")

    def test_start_not_ready_quarantine(self):
        self.f.cli.start_changes = {"interactive_ready": False}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.prompts, [])

    def test_start_wrong_kind_quarantine(self):
        self.f.cli.start_changes = {"agent": "codex"}
        self.assertEqual(self.f.event()["status"], "quarantined")

    def test_start_missing_session_quarantine(self):
        self.f.cli.start_changes = {"agent_session": {}}
        self.assertEqual(self.f.event()["status"], "quarantined")

    def test_live_recipient_drift_quarantine(self):
        self.f.cli.get_changes = {"terminal_id": "another-terminal"}
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.prompts, [])

    def test_tab_shell_overrides_after_creation_block_start(self):
        def hook(args):
            if args[:2] == ["pane", "get"]:
                self.f.os.envs[199]["overrides"] = True
        self.f.cli.hook = hook
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.started, [])

    def test_capacity_lost_after_stop_no_tab(self):
        self.f.sol()
        self.f.os.after_stop = lambda: setattr(self.f.quota, "codex", codex_payload(100))
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.cli.created, [])

    def test_capacity_lost_after_create_no_start(self):
        self.f.sol()
        def hook(args):
            if args[:2] == ["pane", "get"]:
                self.f.quota.pi = {**OAUTH, "authType": "apiKey"}
        self.f.cli.hook = hook
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertEqual(self.f.cli.started, [])

    def test_no_close_resume_focus_or_shell_task_argv(self):
        self.f.event()
        for argv in self.f.cli.argvs:
            self.assertNotIn(tuple(argv[1:3]), {("pane", "run"), ("pane", "close"), ("tab", "close"), ("agent", "focus"), ("tab", "focus"), ("pane", "send-text")})
        self.assertTrue(all(sig == signal.SIGSTOP for _, sig in self.f.os.kills))


class TestLaunchSafety(FixtureTest):
    def test_server_not_client_must_be_proven(self):
        self.f.os.server = None
        self.no_control(self.f.event())

    def test_server_environment_override_refused(self):
        self.f.os.server["env"]["overrides"] = True
        self.no_control(self.f.event())

    def test_native_source_auth_override_refused(self):
        self.f.os.envs[11]["overrides"] = True
        self.no_control(self.f.event())

    def test_invalid_models_config(self):
        path = self.f.home / ".pi" / "agent" / "models.json"
        for value in ({"models": []}, [], {"providers": {"openai-codex": "unknown"}}):
            write_json(path, value)
            self.no_control(self.f.event())

    def test_models_customization_allowed(self):
        write_json(self.f.home / ".pi" / "agent" / "models.json",
                   {"providers": {"openai-codex": {"models": [{"id": "gpt-6.1-sol", "name": "Sol", "contextWindow": 500000}]}}})
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_models_provider_auth_overrides_denied(self):
        for key in ("apiKey", "baseUrl", "headers", "auth", "provider", "apiKeyEnv", "endpoint"):
            write_json(self.f.home / ".pi" / "agent" / "models.json",
                       {"providers": {"openai-codex": {key: "synthetic-not-secret"}}})
            self.no_control(self.f.event())

    def test_nested_models_auth_override_denied(self):
        write_json(self.f.home / ".pi" / "agent" / "models.json",
                   {"providers": {"openai-codex": {"models": [{"id": "gpt-6.1-sol", "headers": {"x": "synthetic"}}]}}})
        self.no_control(self.f.event())

    def test_real_benign_native_settings_env_allows_handoff(self):
        env = {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "100000", "DISABLE_ERROR_REPORTING": "1",
               "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1"}
        for profile in self.f.profiles:
            write_json(Path(profile["config_dir"]) / "settings.json", {"env": env})
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")

    def test_settings_env_credential_override_denied_by_presence(self):
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "NODE_OPTIONS"):
            write_json(Path(self.f.profiles[0]["config_dir"]) / "settings.json", {"env": {key: ""}})
            self.no_control(self.f.event())

    def test_settings_overrides_denied(self):
        write_json(self.f.cwd / ".pi" / "settings.json", {"provider": "openai"})
        self.no_control(self.f.event())

    def test_destination_profile_settings_denied(self):
        write_json(Path(self.f.profiles[1]["config_dir"]) / "settings.json", {"apiKeyHelper": "not-run"})
        self.no_control(self.f.event())

    def test_absent_models_uses_defaults(self):
        self.assertTrue(qh.launch_settings_safe({"HOME": str(self.f.home)}, str(self.f.cwd)))

    def test_expanduser_profile(self):
        cfg = qh.HandoffConfig(claude_profiles=[{"name": "c", "config_dir": "~/cfg"}])
        self.assertEqual(cfg.profile_config_dir("c"), os.path.expanduser("~/cfg"))

    def test_helper_probes_scoped_and_isolated_env(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Completed", (), {"returncode": 0, "stdout": json.dumps(None if "--scoped" in argv else claude_payload())})()
        quota = qh.QuotaPort(self.f.state)
        with patch.dict(os.environ, {k: "synthetic" for k in qh.CHILD_CLEAR_ENV_KEYS}), patch.object(qh.subprocess, "run", side_effect=run):
            payload = quota.claude_usage(self.f.profiles[1], NOW)
        self.assertIn("weekly_scoped", payload)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0][-1], "--scoped")
        for argv, kwargs in calls:
            self.assertNotIn("synthetic", argv)
            self.assertFalse(set(kwargs["env"]) & qh.OVERRIDE_KEYS)
            self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], self.f.profiles[1]["config_dir"])
            self.assertEqual(kwargs["env"]["MODEL_LANES_STATE_DIR"], kwargs["env"]["HERDR_PLUGIN_STATE_DIR"])
        self.assertEqual(calls[0][1]["env"], calls[1][1]["env"])

    def test_pi_check_no_refresh_exact_argv(self):
        quota = qh.QuotaPort(self.f.state)
        with patch.object(qh.subprocess, "run", return_value=type("Completed", (), {"returncode": 0, "stdout": json.dumps(OAUTH)})()) as run:
            self.assertTrue(qh.validate_pi_auth(quota.pi_auth_check()))
        self.assertEqual(run.call_args.args[0], ["pi", "auth", "check", "--provider", "openai-codex", "--model", "gpt-6.1-sol", "--json", "--no-refresh"])


class TestCheckpoint(FixtureTest):
    def test_checkpoint_required_not_native_transcript(self):
        self.assertEqual(self.f.engine.mark_public("source-pane")["status"], "checkpoint-required")
        self.assertEqual(self.f.engine.mark_public("source-pane", self.f.path)["status"], "refused")
        self.assertEqual(self.f.cli.prompts, [])

    def test_owner_checkpoint_only_to_verified_sol(self):
        self.assertEqual(self.f.mark()["status"], "marked")
        self.f.sol()
        result = self.f.event()
        self.assertEqual(result["status"], "prompted")
        self.assertEqual(len(self.f.cli.prompts), 1)
        prompt = self.f.cli.prompts[0]
        self.assertEqual(prompt[2], "new-pane")
        self.assertEqual(prompt[3], "Continue from this checkpoint:\nOwner-approved public checkpoint ONLY")
        self.assertNotIn(CANARY, prompt[3])
        self.assertNotIn(str(self.f.path), prompt[3])
        self.assertLess(self.f.audit.index("SAVE:prompt-intent"), self.f.audit.index("PROMPT"))
        self.assertNotIn("checkpoint ONLY", json.dumps(self.f.receipt()))

    def test_native_always_awaits_even_when_public(self):
        self.assertEqual(self.f.mark()["status"], "marked")
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_malformed_private_authority_and_classification(self):
        for change in ({"classification": "protected"}, {"authority": {}}, {"checkpoint": ""}):
            path = self.f.checkpoint(**change)
            self.assertEqual(self.f.engine.mark_public("source-pane", path)["status"], "refused")
        path = self.f.checkpoint()
        path.chmod(0o644)
        self.assertEqual(self.f.engine.mark_public("source-pane", path)["status"], "refused")

    def test_checkpoint_hash_change_leaves_blank(self):
        self.f.mark()
        self.f.checkpoint(checkpoint="changed")
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_last_user_token_change_does_not_inherit(self):
        self.f.mark()
        self.f.events[0]["uuid"] = "new-task"
        self.f.save_events()
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_terminal_change_does_not_inherit(self):
        self.f.mark()
        self.f.cli.source["terminal_id"] = "new-source-terminal"
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_session_change_does_not_inherit(self):
        self.f.mark()
        self.f.cli.source["agent_session"]["source"] = "new-session-origin"
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_missing_malformed_template_leaves_blank(self):
        self.f.mark()
        self.f.config.auto_prompt_template = {"authority": AUTHORITY, "text": "read {source_transcript}"}
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_no_template_leaves_blank(self):
        self.f.mark()
        self.f.config.auto_prompt_template = None
        self.f.sol()
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_source_changes_after_stop_before_prompt_leaves_blank(self):
        self.f.mark()
        self.f.sol()
        def hook(args):
            if args[:2] == ["agent", "start"]:
                self.f.events[0]["uuid"] = "later-user-task"
                self.f.save_events()
        self.f.cli.hook = hook
        self.assertEqual(self.f.event()["status"], "awaiting-confirmation")
        self.assertEqual(self.f.cli.prompts, [])

    def test_prompt_timeout_no_retry(self):
        self.f.mark()
        self.f.sol()
        self.f.cli.fail = "prompt"
        self.assertEqual(self.f.event()["status"], "quarantined")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.prompts), 1)

    def test_crash_after_prompt_before_save_no_retry(self):
        self.f.mark()
        self.f.sol()
        save = self.f.engine.receipts.save
        def crash(key, receipt):
            if receipt["phase"] == "prompted":
                raise SystemExit("lost prompt response")
            save(key, receipt)
        self.f.engine.receipts.save = crash
        with self.assertRaises(SystemExit):
            self.f.event()
        self.assertEqual(self.f.receipt()["phase"], "prompt-intent")
        self.assertEqual(self.f.event()["status"], "duplicate")
        self.assertEqual(len(self.f.cli.prompts), 1)


class PipeRPC:
    """Popen-shaped in-process server using real nonblocking selectable pipes."""
    def __init__(self, argv, *, silent=False, **kwargs):
        self.requests = []
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
        self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
        self.reader = os.fdopen(stdin_read, "rb", buffering=0)
        self.writer = os.fdopen(stdout_write, "wb", buffering=0)
        self.silent = silent
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            for line in self.reader:
                request = json.loads(line)
                self.requests.append(request)
                if self.silent or "id" not in request:
                    continue
                payload = codex_payload()
                result = {1: {}, 2: payload["account"], 3: payload["limits"]}[request["id"]]
                self.writer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            self.reader.close()
            self.writer.close()

    def terminate(self):
        pass

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise qh.subprocess.TimeoutExpired("fake-rpc", timeout)
        return 0

    def kill(self):
        pass


class TestBoundedRPC(unittest.TestCase):
    def test_exact_rpc_and_official_shapes(self):
        instances = []
        def popen(argv, **kwargs):
            instance = PipeRPC(argv, **kwargs)
            instances.append(instance)
            self.assertEqual(argv, ["codex", "app-server", "--listen", "stdio://"])
            return instance
        with TemporaryDirectory() as tmp, patch.object(qh.subprocess, "Popen", side_effect=popen):
            payload = qh.QuotaPort(Path(tmp)).codex_windows(NOW, timeout=1)
        self.assertIsNotNone(qh.validate_codex_snapshot(payload, NOW))
        requests = instances[0].requests
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized", "account/read", "account/rateLimits/read"])
        self.assertEqual(requests[2]["params"], {"refreshToken": False})

    def test_silent_server_absolute_deadline(self):
        with TemporaryDirectory() as tmp, patch.object(qh.subprocess, "Popen", side_effect=lambda *a, **k: PipeRPC(*a, silent=True, **k)):
            start = time.monotonic()
            with self.assertRaisesRegex(qh.HandoffError, "deadline"):
                qh.QuotaPort(Path(tmp)).codex_windows(NOW, timeout=0.05)
            self.assertLess(time.monotonic() - start, 1)


class TestCLI(FixtureTest):
    def call(self, command, config=None, env=None):
        path = self.f.root / "config.json"
        write_json(path, config or {})
        output = io.StringIO()
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(output):
            rc = qh.main(["--config", str(path), "--state-dir", str(self.f.state), *command])
        return rc, json.loads(output.getvalue())

    def test_help_exit_zero(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            qh.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_disabled_status_scan_event_json_zero_calls(self):
        with patch.object(qh.HerdrPort, "_execute", side_effect=AssertionError("disabled called Herdr")):
            for command in (["status"], ["scan"], ["event"]):
                rc, value = self.call(command, env={"HERDR_PLUGIN_EVENT_JSON": "not-json"})
                self.assertEqual(rc, 0)
                self.assertIsInstance(value, dict)
        self.assertFalse(self.f.state.exists())

    def test_event_never_reads_stdin(self):
        class ForbiddenInput:
            def read(self, *args):
                raise AssertionError("stdin read")
        with patch.object(sys, "stdin", ForbiddenInput()):
            self.assertEqual(self.call(["event"])[0], 0)

    def test_enabled_malformed_event_json_exit_one(self):
        cfg = {"enabled": True, "allowed_workspaces": ["ws1"], "authority": AUTHORITY}
        rc, value = self.call(["event"], cfg, {"HERDR_PLUGIN_EVENT_JSON": "[broken"})
        self.assertEqual(rc, 1)
        self.assertEqual(value["status"], "error")

    def test_enabled_event_json_end_to_end_with_safe_cli_and_os(self):
        cfg = {"enabled": True, "allowed_workspaces": ["ws1"], "authority": AUTHORITY, "claude_profiles": self.f.profiles}
        with patch.object(qh, "OsPort", return_value=self.f.os), \
                patch.object(qh, "HerdrPort", return_value=self.f.engine.herdr), \
                patch.object(qh, "QuotaPort", return_value=self.f.quota):
            rc, value = self.call(["event"], cfg, {"HERDR_PLUGIN_EVENT_JSON": json.dumps({"pane_id": "source-pane"}), "HERDR_PANE_ID": "source-pane"})
        self.assertEqual(rc, 0)
        self.assertEqual(value["status"], "awaiting-confirmation")
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertNotIn(CANARY, json.dumps(value))

    def test_adapter_rejects_wrong_result_type(self):
        for key, args, result in (("agent", ["agent", "get", "p"], []), ("process_info", ["pane", "process-info", "--pane", "p"], []),
                                  ("agents", ["agent", "list"], {}), ("panes", ["pane", "list", "--workspace", "w"], {})):
            port = qh.HerdrPort("fake", lambda argv, key=key, result=result: FakeCLI.ok({key: result}))
            with self.assertRaises(qh.HandoffError):
                port._result(args, key)

    def test_mark_requires_checkpoint_parser(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            qh.build_parser().parse_args(["mark-public", "--pane", "p"])
        self.assertEqual(ctx.exception.code, 2)

    def test_config_load_and_default_no_plugin_dirs(self):
        path = self.f.root / "config.json"
        write_json(path, {"enabled": True, "allowed_workspaces": ["ws1"], "authority": AUTHORITY, "claude_profiles": self.f.profiles})
        cfg = qh.HandoffConfig.load(path)
        self.assertTrue(cfg.authorized())
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(qh.default_config_path())
            self.assertIsNone(qh.default_state_dir())


class TestProcAdapter(unittest.TestCase):
    def stat_text(self):
        fields = ["S", "99", "10"] + ["0"] * 17
        fields[19] = "123456"
        return "11 (a name with ) parentheses) " + " ".join(fields)

    def parse(self, exe, argv=None):
        from types import SimpleNamespace
        with patch.object(qh.Path, "read_text", return_value=self.stat_text()), \
                patch.object(qh.os, "readlink", return_value=exe), \
                patch.object(qh.os, "stat", return_value=SimpleNamespace(st_uid=os.getuid())), \
                patch("builtins.open", return_value=io.BytesIO(argv or b"")):
            return qh.OsPort().process(11)

    def test_real_proc_stat_indices_and_comm_parentheses(self):
        p = self.parse("/usr/bin/claude")
        self.assertEqual((p["pid"], p["pgid"], p["start"], p["native"]), (11, 10, 123456, "claude"))

    def test_native_versioned_claude_executable(self):
        self.assertEqual(self.parse("/home/owner/.local/share/claude/versions/2.1.100")["native"], "claude")
        self.assertIsNone(self.parse("/tmp/not-claude")["native"])

    def test_pi_actual_node_script_identity(self):
        installed = Path("/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js")
        with patch.object(qh.Path, "resolve", return_value=installed):
            p = self.parse("/usr/bin/node", b"node\x00/usr/bin/pi\x00--provider\x00openai-codex\x00")
        self.assertEqual(p["native"], "pi")
        with patch.object(qh.Path, "resolve", return_value=Path("/usr/lib/unrelated.js")):
            self.assertIsNone(self.parse("/usr/bin/node", b"node\x00/usr/lib/unrelated.js\x00")["native"])

    def test_foreign_member_not_hidden_when_exe_unreadable(self):
        from types import SimpleNamespace
        with patch.object(qh.Path, "read_text", return_value=self.stat_text()), \
                patch.object(qh.os, "readlink", side_effect=PermissionError), \
                patch.object(qh.os, "stat", return_value=SimpleNamespace(st_uid=os.getuid() + 1)):
            p = qh.OsPort().process(11)
        self.assertEqual(p["pgid"], 10)
        self.assertEqual(p["uid"], os.getuid() + 1)
        self.assertIsNone(p["native"])

    def test_unreadable_proc_stat_fails_closed(self):
        with patch.object(qh.Path, "read_text", side_effect=PermissionError), self.assertRaises(qh.HandoffError):
            qh.OsPort().process(11)

    def test_environment_returns_paths_and_presence_never_credentials(self):
        raw = b"HOME=/fake/home\x00CLAUDE_CONFIG_DIR=/fake/config\x00OPENAI_API_KEY=SECRET-CANARY\x00UNRELATED=value\x00"
        with patch("builtins.open", return_value=io.BytesIO(raw)):
            env = qh.OsPort().environment(11)
        self.assertEqual(env, {"HOME": "/fake/home", "CLAUDE_CONFIG_DIR": "/fake/config", "overrides": True})
        self.assertNotIn("SECRET-CANARY", json.dumps(env))

    def test_real_server_ancestor_not_cli_environment(self):
        port = qh.OsPort()
        shell = process(400, pgid=400, ppid=500)
        server = process(500, pgid=500, ppid=1)
        server["exe"] = "/usr/bin/herdr"
        env = {"HOME": "/server/home", "overrides": False}
        with patch.object(qh.os, "getppid", return_value=400), \
                patch.object(port, "process", side_effect=lambda pid: {400: shell, 500: server}[pid]), \
                patch.object(port, "environment", return_value=env), \
                patch.object(port, "server_peer", return_value=500):
            self.assertEqual(port.launch_environment()["env"], env)
            env["overrides"] = True
            self.assertIsNone(port.launch_environment())

    def test_deleted_herdr_inode_accepted_only_for_authenticated_ancestor(self):
        port = qh.OsPort()
        server = process(500, pgid=500, ppid=1)
        server["exe"] = "/home/owner/.local/bin/herdr (deleted)"
        env = {"HOME": "/server/home", "overrides": False}
        with patch.object(qh.os, "getppid", return_value=500), \
                patch.object(port, "process", return_value=server), \
                patch.object(port, "environment", return_value=env), \
                patch.object(port, "server_peer", return_value=500) as peer:
            proof = port.launch_environment()
            self.assertEqual((proof["pid"], proof["start"]), (500, 1500))
            peer.return_value = 501
            self.assertIsNone(port.launch_environment())
            peer.return_value = 500
            server["exe"] += "evil"
            self.assertIsNone(port.launch_environment())
            server["exe"] = "/home/owner/.local/bin/herdr (deleted)"
            server["uid"] += 1
            self.assertIsNone(port.launch_environment())

    def test_socket_peer_credentials_checked_without_application_request(self):
        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/fake/herdr.sock"}), \
                patch.object(qh.socket, "socket") as factory:
            connection = factory.return_value.__enter__.return_value
            connection.getsockopt.return_value = qh.struct.pack("3i", 500, os.getuid(), 0)
            self.assertEqual(qh.OsPort().server_peer(), 500)
            connection.connect.assert_called_once_with("/fake/herdr.sock")
            connection.send.assert_not_called()
            connection.getsockopt.return_value = qh.struct.pack("3i", 500, os.getuid() + 1, 0)
            self.assertIsNone(qh.OsPort().server_peer())

    def test_missing_socket_proof_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(qh.socket, "socket") as factory:
            self.assertIsNone(qh.OsPort().server_peer())
            factory.assert_not_called()


class TestPiTerminalFooter(FixtureTest):
    def footer(self, cwd, stats="2.1k · 98% (openai-codex) gpt-6.1-sol • high"):
        return PI_FRIENDLY + "\n────────────────\n" + cwd + "\n" + stats

    def test_exact_cwd_and_stats_footer_accepted(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        cwd = str(fixture.cwd)
        self.assertTrue(qh.pi_tail_notice(self.footer(cwd), cwd))
        fixture.cli.live = self.footer(cwd)
        self.assertIsNotNone(fixture.engine.detect_exhausted(fixture.cli.source))

    def test_wrong_cwd_or_stats_refused(self):
        cwd = str(self.f.cwd)
        for text in (self.footer(cwd + "/nested"), self.footer("/elsewhere" + cwd),
                     self.footer(cwd, stats="2.1k · 98% (openai-codex) gpt-6.1-pro • high"),
                     self.footer(cwd, stats="(openai-codex) gpt-6.1-sol • medium"),
                     self.footer(cwd, stats="2.1k · 98% (openai-codex) gpt-6.1-sol")):
            with self.subTest(text=text.splitlines()[-1]):
                self.assertFalse(qh.pi_tail_notice(text, cwd))

    def test_appended_draft_or_queued_text_refused(self):
        cwd = str(self.f.cwd)
        for extra in ("queued user text", "❯ half-typed draft", "user follow up", "$ echo hi"):
            with self.subTest(extra=extra):
                self.assertFalse(qh.pi_tail_notice(self.footer(cwd) + "\n" + extra, cwd))
        # Non-absolute/odd cwd never authorizes tolerating a line.
        self.assertFalse(qh.pi_tail_notice(self.footer("relative/path"), "relative/path"))

    def widget(self, label=None, middle=None, bottom="╰ gpt-6.1-sol • high ────────────────────────────────────╯"):
        return ["╭────────────────────────────────────────────── " + (label or "work") + " ╮",
                middle if middle is not None else "│❯                                                      │", bottom]

    def test_input_widget_frame_accepted(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        self.assertEqual(os.path.basename(str(fixture.cwd)), "work")
        text = PI_FRIENDLY + "\n" + "\n".join(self.widget())
        self.assertTrue(qh.pi_tail_notice(text, str(fixture.cwd)))
        fixture.cli.live = text
        self.assertIsNotNone(fixture.engine.detect_exhausted(fixture.cli.source))
        fixture.config.enabled, fixture.config.observe_only = False, True
        fixture.quota.codex = codex_payload(100)
        self.assertEqual(fixture.engine.handle_event({"pane_id": "source-pane"})["status"], "observed")
        # Border-length variation is fine; label must be the exact basename.
        self.assertTrue(qh.pi_tail_notice(PI_FRIENDLY + "\n" + "\n".join(
            self.widget()[:1] + ["│❯    │", "╰ gpt-6.1-sol • medium ─╯"]), str(fixture.cwd)))

    def test_input_widget_draft_or_wrong_frame_refused(self):
        cwd = str(self.f.cwd)
        for text in (PI_FRIENDLY + "\n" + "\n".join(self.widget(middle="│❯ new task text                                │")),
                     PI_FRIENDLY + "\n" + "\n".join(self.widget(label="vivesca")),
                     PI_FRIENDLY + "\n" + "\n".join(self.widget(label="..")),
                     PI_FRIENDLY + "\n" + "\n".join(self.widget(bottom="╰ gpt-6.1-pro • high ───────────────────────────────────╯")),
                     PI_FRIENDLY + "\n" + "\n".join(self.widget(bottom="╰ gpt-6.1-sol • ultra ───────────────────────────────╯")),
                     PI_FRIENDLY + "\n" + "\n".join(self.widget() + ["queued text"])):
            with self.subTest(text=text.splitlines()[-1][:40]):
                self.assertFalse(qh.pi_tail_notice(text, cwd))

    def test_stats_footer_rejects_prose_prefix(self):
        cwd = str(self.f.cwd)
        for stats in ("please wait, queued (openai-codex) gpt-6.1-sol • high",
                      "❯ draft input (openai-codex) gpt-6.1-sol • high",
                      "user said retry later (openai-codex) gpt-6.1-sol • high",
                      "4 tries left until reset (openai-codex) gpt-6.1-sol • high"):
            with self.subTest(stats=stats):
                self.assertFalse(qh.pi_tail_notice(PI_FRIENDLY + "\n" + stats, cwd))
        for stats in ("2.1k · 98% (openai-codex) gpt-6.1-sol • high",
                      "$1.23 (openai-codex) gpt-6.1-sol • high",
                      "45k tok · 2.1k · 98% (openai-codex) gpt-6.1-sol • high"):
            with self.subTest(stats=stats):
                self.assertTrue(qh.pi_tail_notice(PI_FRIENDLY + "\n" + stats, cwd))

    def test_pi_observe_with_real_tui_footer(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        fixture.config.enabled, fixture.config.observe_only = False, True
        fixture.quota.codex = codex_payload(100)
        fixture.cli.live = self.footer(str(fixture.cwd))
        result = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "observed")
        self.assertEqual(fixture.os.kills + fixture.cli.created + fixture.cli.started + fixture.cli.prompts, [])


class TestObservation(FixtureTest):
    def observe(self):
        self.f.config.enabled = False
        self.f.config.observe_only = True
        return self.f.event()

    def observation_path(self):
        return self.f.state / "observations" / (qh.event_key(self.f.cli.source) + ".json")

    def test_mode_matrix(self):
        cfg = self.f.config
        self.assertTrue(self.f.engine.can_control())
        self.assertTrue(cfg.authorized())
        cfg.enabled, cfg.observe_only = False, True
        self.assertFalse(cfg.authorized())
        self.assertTrue(cfg.observation_authorized())
        self.assertTrue(self.f.engine.can_observe())
        self.assertFalse(self.f.engine.can_control())
        for enabled, observe in ((False, False), (True, True), (True, "yes"), (False, 1), ("true", False), (None, None)):
            with self.subTest(mode=(enabled, observe)):
                cfg.enabled, cfg.observe_only = enabled, observe
                self.assertFalse(cfg.authorized())
                self.assertFalse(cfg.observation_authorized())
                self.assertFalse(self.f.engine.operational())
                self.assertFalse(self.f.engine.can_control())
                self.assertFalse(self.f.engine.can_observe())

    def test_disabled_fast_path_makes_no_herdr_calls(self):
        self.f.config.enabled = self.f.config.observe_only = False
        result = self.f.engine.handle_event({"bogus": 1})
        self.assertEqual(result, {"status": "disabled", "mutated": False})
        self.assertEqual(self.f.cli.argvs, [])

    def test_observe_native_records_content_free_witness(self):
        result = self.observe()
        self.assertEqual(result["status"], "observed")
        self.assertFalse(result["mutated"])
        self.assertTrue(result["observed"])
        self.assertEqual(result["source_pane"], "source-pane")
        self.no_control(result)
        self.assertEqual(self.f.quota.calls, [])
        self.assertIsNone(self.f.receipt())
        self.assertFalse((self.f.state / "exclusions.json").exists())
        self.assertFalse((self.f.state / "markers").exists())
        path = self.observation_path()
        record = json.loads(path.read_text())
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["event_key"], qh.event_key(self.f.cli.source))
        self.assertEqual(record["source"], {"pane_id": "source-pane", "tab_id": "source-tab",
                                           "workspace_id": "ws1", "agent": "claude"})
        self.assertEqual(record["source_token"], qh.digest(qh.identity(self.f.cli.source)))
        self.assertEqual(record["error_timestamp"], qh._to_epoch(iso()))
        raw = path.read_text()
        self.assertNotIn(CANARY, raw)
        self.assertNotIn(NOTICE, raw)
        self.assertNotIn(str(self.f.path), raw)
        self.assertNotIn(str(self.f.home), raw)

    def test_observe_excluded_profile_never_probed_or_recorded(self):
        self.f.config.claude_profiles = self.f.profiles[1:]  # c deliberately absent: excluded profile
        result = self.observe()
        self.assertEqual(result, {"status": "refused", "reason": "unproven-source-environment", "mutated": False})
        self.assertEqual(self.f.quota.calls, [])
        self.assertFalse((self.f.state / "observations").exists())

    def test_observe_pi_friendly_error_with_100_proves_witness(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        fixture.config.enabled, fixture.config.observe_only = False, True
        fixture.quota.codex = codex_payload(100)
        result = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "observed")
        self.assertEqual(fixture.os.kills, [])
        self.assertEqual(fixture.cli.created + fixture.cli.started + fixture.cli.prompts, [])
        self.assertEqual(fixture.quota.calls, [])

    def test_observe_pi_same_429_wording_under_100_no_record(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        fixture.config.enabled, fixture.config.observe_only = False, True
        fixture.quota.codex = codex_payload(40)  # same friendly text, not genuine exhaustion
        result = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "unproven-pi-exhaustion")
        self.assertEqual(fixture.quota.calls, [])
        self.assertFalse((fixture.state / "observations").exists())

    def test_observe_account_hold_or_stale_error_is_no_evidence(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        fixture.quota.codex = codex_payload(100)
        fixture.events[-1]["message"]["errorMessage"] = "Your account is currently on hold"
        fixture.save_events()
        fixture.config.enabled, fixture.config.observe_only = False, True
        result = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "refused")
        self.assertFalse((fixture.state / "observations").exists())
        stale = copy.deepcopy(fixture.events)
        stale[-1]["message"]["errorMessage"] = PI_FRIENDLY
        stale[-1]["timestamp"] = iso(NOW - 3600)
        fixture.events = stale
        fixture.save_events()
        result = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "refused")
        self.assertFalse((fixture.state / "observations").exists())

    def test_malformed_json_observation_fails_closed(self):
        self.observe()
        path = self.observation_path()
        path.write_text("{not json")
        result = self.f.event()
        self.assertEqual(result, {"status": "refused", "reason": "corrupt-receipt", "mutated": False})
        self.assertEqual(path.read_text(), "{not json")

    def test_corrupt_observation_fails_closed(self):
        self.observe()
        path = self.observation_path()
        for corrupt in ("{not json", "[]", "null", "0", "3.14", '"text"', "true",
                        '{}', '{"version": 1}', '{"version": 2, "event_key": "x"}',
                        '{"version": 1, "event_key": "x", "source_token": 1, "evidence_token": null}'):
            with self.subTest(corrupt=corrupt):
                path.write_text(corrupt)
                result = self.f.engine.handle_event({"pane_id": "source-pane"})
                self.assertEqual(result, {"status": "refused", "reason": "corrupt-receipt", "mutated": False})
                self.assertEqual(path.read_text(), corrupt)  # never overwritten
        # Exact-schema mutations: a dict with only version/key/tokens, missing or
        # extra fields, wrong types and non-hex tokens must all fail closed.
        self.observation_path().unlink()  # clear the raw-corruption leftovers
        self.observe()  # write a genuine record to mutate
        record = json.loads(self.observation_path().read_text())
        def broken(change):
            value = copy.deepcopy(record)
            change(value)
            return json.dumps(value)
        mutations = {
            "tokens-only": lambda v: [v.pop(k) for k in ("observed_at", "error_timestamp", "source")] and None,
            "missing-version": lambda v: v.pop("version") and None,
            "extra-field": lambda v: v.update(extra=1) or None,
            "version-bool": lambda v: v.update(version=True) or None,
            "version-float": lambda v: v.update(version=1.0) or None,
            "observed-at-string": lambda v: v.update(observed_at="soon") or None,
            "observed-at-bool": lambda v: v.update(observed_at=True) or None,
            "error-timestamp-null": lambda v: v.update(error_timestamp=None) or None,
            "source-missing": lambda v: v.pop("source") and None,
            "source-extra": lambda v: v["source"].update(cwd="/x") or None,
            "source-id-number": lambda v: v["source"].update(pane_id=7) or None,
            "source-agent-empty": lambda v: v["source"].update(agent="") or None,
            "token-not-hex": lambda v: v.update(source_token="z" * 64) or None,
            "token-short": lambda v: v.update(evidence_token="ab" * 31) or None,
            "wrong-event-key": lambda v: v.update(event_key="f" * 64) or None,
        }
        for name, change in mutations.items():
            with self.subTest(mutation=name):
                path.write_text(broken(change))
                result = self.f.engine.handle_event({"pane_id": "source-pane"})
                self.assertEqual(result, {"status": "refused", "reason": "corrupt-receipt", "mutated": False})

    def test_observe_guard_refuses_without_observation_mode(self):
        det = self.f.engine.detect_exhausted(self.f.cli.source)
        with self.assertRaises(qh.HandoffError):
            self.f.engine._observe(det, qh.event_key(self.f.cli.source))
        self.no_control()

    def test_duplicate_pi_observation_zero_additional_quota_calls(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fixture = Fixture(temp.name, route="pi")
        fixture.config.enabled, fixture.config.observe_only = False, True
        fixture.quota.codex = codex_payload(100)
        fixture.cli.live = PI_FRIENDLY + "\n────────────────\n" + str(fixture.cwd) + "\n2.1k · 98% (openai-codex) gpt-6.1-sol • high"
        auth_calls, codex_calls = [], []
        real_auth, real_codex = fixture.quota.pi_auth_check, fixture.quota.codex_windows
        fixture.quota.pi_auth_check = lambda: (auth_calls.append(1), real_auth())[1]
        fixture.quota.codex_windows = lambda now: (codex_calls.append(1), real_codex(now))[1]
        first = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(first["status"], "observed")
        self.assertEqual((len(auth_calls), len(codex_calls)), (1, 1))
        second = fixture.engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(second["status"], "already-observed")
        self.assertFalse(second["mutated"])
        self.assertEqual((len(auth_calls), len(codex_calls)), (1, 1))  # unchanged
        self.assertEqual(fixture.quota.calls, [])

    def test_duplicate_observation_idempotent(self):
        self.assertEqual(self.observe()["status"], "observed")
        path = self.observation_path()
        before, calls = path.read_text(), len(self.f.quota.calls)
        second = self.f.event()
        self.assertEqual(second["status"], "already-observed")
        self.assertFalse(second["mutated"])
        self.assertEqual(path.read_text(), before)
        self.assertEqual(len(self.f.quota.calls), calls)
        self.no_control(second)

    def test_control_legacy_exactly_one_stop_and_create(self):
        result = self.f.event()
        self.assertTrue(result["mutated"])
        self.assertEqual(self.f.os.kills, [(10, signal.SIGSTOP)])
        self.assertEqual(len(self.f.cli.created), 1)
        self.assertEqual(len(self.f.cli.started), 1)

    def test_observer_branch_proves_no_control_path_runs(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("control path executed in observer mode")
        engine, os_port, quota = self.f.engine, self.f.os, self.f.quota
        for target in (engine._handoff, engine.exclude_source, engine.choose_destination,
                       engine.capacity_at_use, os_port.killpg, quota.claude_usage):
            object.__setattr__(engine if target in (engine._handoff, engine.exclude_source,
                                                    engine.choose_destination, engine.capacity_at_use) else
                               (os_port if target is os_port.killpg else quota),
                               target.__name__, forbidden)
        result = self.observe()
        self.assertEqual(result["status"], "observed")
        mutating = [a for a in self.f.cli.argvs if a[1:3] in (("tab", "create"), ("agent", "start"), ("agent", "prompt")) or a[1] == "notification"]
        self.assertEqual(mutating, [])

    def test_handoff_direct_call_cannot_bypass_mask(self):
        key = qh.event_key(self.f.cli.source)
        for enabled, observe in ((False, False), (False, True)):
            with self.subTest(mode=(enabled, observe)):
                self.f.config.enabled, self.f.config.observe_only = enabled, observe
                with self.assertRaises(qh.HandoffError):
                    self.f.engine._handoff(self.f.cli.source, key)
                self.no_control()

    def test_mark_public_blocked_in_observation_mode(self):
        self.f.config.auto_prompt_template = {"authority": AUTHORITY, "text": "{checkpoint}"}
        self.observe()
        result = self.f.engine.mark_public("source-pane", self.f.checkpoint())
        self.assertEqual(result, {"status": "disabled", "mutated": False})
        self.assertFalse((self.f.state / "markers").exists())

    def test_status_dicts_exact_per_mode(self):
        self.assertEqual(self.f.engine.status(), {"enabled": True, "status": "enabled"})
        self.assertEqual(self.observe()["status"], "observed")
        self.assertEqual(self.f.engine.status(), {"enabled": False, "status": "observing",
                                                  "observe_only": True, "observation_count": 1})
        self.f.config.enabled = self.f.config.observe_only = False
        self.assertEqual(self.f.engine.status(), {"enabled": False, "status": "disabled"})

    def test_old_config_fallback_enables_no_controls(self):
        path = self.f.root / "handoff.json"
        profiles = [{"name": p["name"], "config_dir": p["config_dir"]} for p in self.f.profiles]
        write_json(path, {"enabled": False, "observe_only": True, "allowed_workspaces": ["ws1"],
                          "claude_profiles": profiles, "authority": AUTHORITY})
        engine = qh.QuotaHandoffEngine(qh.HandoffConfig.load(path), self.f.engine.herdr, self.f.quota,
                                       self.f.os, self.f.state)
        result = engine.handle_event({"pane_id": "source-pane"})
        self.assertEqual(result["status"], "observed")
        self.assertFalse(result["mutated"])
        self.assertEqual(self.f.os.kills, [])
        self.assertEqual(self.f.cli.created + self.f.cli.started + self.f.cli.prompts, [])
        self.assertEqual(self.f.receipt(), None)


if __name__ == "__main__":
    unittest.main()

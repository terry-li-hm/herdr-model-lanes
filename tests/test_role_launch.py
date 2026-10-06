import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import proxy_capacity as proxy
import role_launch as role

NOW = 1_800_000_000


def lane(name, kind="claude", model=None):
    return {
        "name": name,
        "kind": kind,
        "model": model or name,
        "command": [kind],
        "args": ["--model", model or name],
    }


def quota(remaining, reset=NOW + 1000):
    return proxy.result("ok", reset, remaining)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.lanes = [lane("primary"), lane("alternate"), lane("codex", "codex")]
        self.capacity = {
            "claude/primary": [quota(40)],
            "claude/alternate": [quota(99)],
            "codex/codex": [quota(95)],
        }

    def test_priority_not_surplus(self):
        selected, _ = role.choose(self.lanes, self.capacity)
        self.assertEqual(selected["name"], "primary")

    def test_depleted_claude_uses_native_codex(self):
        self.capacity["claude/primary"] = [quota(0)]
        self.capacity["claude/alternate"] = [proxy.result("exhausted", NOW + 1000, 0)]
        selected, _ = role.choose(self.lanes, self.capacity)
        self.assertEqual(selected["kind"], "codex")

    def test_one_exhausted_account_does_not_block_provider(self):
        self.capacity["claude/primary"] = [quota(0), quota(30)]
        self.assertEqual(role.choose(self.lanes, self.capacity)[0]["name"], "primary")

    def test_headroom_boundary_and_configurable_threshold(self):
        self.capacity["claude/primary"] = [quota(5)]
        self.assertEqual(role.choose(self.lanes, self.capacity)[0]["name"], "alternate")
        self.assertEqual(
            role.choose(self.lanes, self.capacity, headroom=4)[0]["name"], "primary"
        )

    def test_unknown_stale_and_failures_skip_or_hold(self):
        for state in ("unknown", "stale", "unavailable"):
            with self.subTest(state=state):
                self.capacity["claude/primary"] = [proxy.result(state)]
                self.assertEqual(
                    role.choose(self.lanes, self.capacity)[0]["name"], "alternate"
                )
                self.assertIsNone(
                    role.choose(self.lanes, self.capacity, unknown="hold")[0]
                )

    def test_authentication_is_not_exhaustion(self):
        selected, decisions = role.choose(
            [self.lanes[0]], {"claude/primary": [proxy.result("auth_error")]}
        )
        self.assertIsNone(selected)
        self.assertEqual(decisions[0]["state"], "auth_error")

    def test_all_exhausted_keeps_resets(self):
        selected, decisions = role.choose(
            self.lanes, {k: [quota(0)] for k in self.capacity}
        )
        self.assertIsNone(selected)
        self.assertTrue(
            all(
                d["state"] == "exhausted" and d["retry_at"] == NOW + 1000
                for d in decisions
            )
        )


class PolicyAndLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "roles": {
                        "lead": {
                            "primary": "fable",
                            "fallback": ["sonnet", "sol"],
                            "maxTier": 2,
                        }
                    },
                    "routes": {
                        "fable": {
                            "qualified": False,
                            "tier": 1,
                            "adapterType": "claude_local",
                            "adapterConfig": {"model": "claude-fable"},
                        },
                        "sonnet": {
                            "qualified": True,
                            "tier": 2,
                            "adapterType": "claude_local",
                            "adapterConfig": {
                                "model": "claude-sonnet",
                                "effort": "high",
                                "dangerouslySkipPermissions": True,
                            },
                        },
                        "sol": {
                            "qualified": True,
                            "tier": 2,
                            "adapterType": "paperclip_runner",
                            "adapterConfig": {
                                "provider": "codex",
                                "model": "gpt-sol",
                                "reasoningEffort": "high",
                                "codexPermissionMode": "never",
                            },
                        },
                    },
                }
            )
        )
        self.config = {
            "proxy": {},
            "policy_file": str(self.policy),
            "launchers": {
                kind: {"command": [kind], "proxy_routed": True}
                for kind in ("claude", "codex")
            },
        }

    def test_existing_policy_order_qualification_effort_and_permissions(self):
        lanes = role.lanes_for_role(self.config, "lead")
        self.assertEqual([l["name"] for l in lanes], ["sonnet", "sol"])
        self.assertIn('model_reasoning_effort="high"', lanes[1]["args"])
        self.assertNotIn("never", lanes[1]["args"])
        self.assertNotIn("--dangerously-skip-permissions", lanes[0]["args"])

    def test_disallow_tier_upgrade_and_max_tier(self):
        document = json.loads(self.policy.read_text())
        document["routes"]["sol"]["tier"] = 1
        self.policy.write_text(json.dumps(document))
        self.assertEqual(len(role.lanes_for_role(self.config, "lead")), 1)
        document["routes"]["sonnet"]["tier"] = 3
        document["routes"]["sol"]["qualified"] = False
        self.policy.write_text(json.dumps(document))
        with self.assertRaises(role.PolicyError):
            role.lanes_for_role(self.config, "lead")

    def test_missing_proxy_attestation_is_error(self):
        self.config["launchers"]["claude"]["proxy_routed"] = False
        with self.assertRaises(role.PolicyError):
            role.lanes_for_role(self.config, "lead")

    def test_instruction_task_args_cwd_and_shell_quoting(self):
        instructions = self.root / "role's instructions.md"
        content = "Keep this exact.\nRead ../shared.md\n$(touch /tmp/never)\n"
        instructions.write_text(content)
        task = "Task\nwith 'quotes'; $HOME and `backticks`"
        prompt = role.initial_prompt(self.config, "lead", [instructions], task)
        self.assertIn(content, prompt)
        self.assertTrue(prompt.endswith(task))
        self.assertIn(str(instructions.parent), prompt)
        lanes = role.lanes_for_role(self.config, "lead")
        for selected in lanes:
            extra = {
                "codex": ["--sandbox", "workspace-write"],
                "claude": ["--permission-mode", "default"],
            }
            command = role.command_for(selected, prompt, extra)
            call = mock.Mock(
                side_effect=[
                    {"root_pane": {"pane_id": "w1:p9"}, "tab": {"tab_id": "w1:t9"}},
                    {},
                ]
            )
            role.launch("w1", self.root, command, call)
            create = call.call_args_list[0].args[0]
            self.assertIn(str(self.root), create)
            self.assertNotIn("--label", create)
            self.assertIn("--no-focus", create)
            run = call.call_args_list[1].args[0]
            self.assertEqual(run[:3], ["pane", "run", "w1:p9"])
            self.assertEqual(shlex.split(run[3]), ["exec", *command])
            self.assertEqual(command[-1], prompt)

    def test_native_args_cannot_override_selection_or_resume(self):
        for args in (
            ["--model", "other"],
            ["-mother"],
            ["resume", "session"],
            ["--remote", "host"],
            ["-c", "model_provider=other"],
            ["--dangerously-bypass-approvals-and-sandbox"],
            ["second prompt"],
        ):
            with self.subTest(args=args), self.assertRaises(role.PolicyError):
                role.native_args(args)

    def test_blocked_launch_creates_no_tab(self):
        lanes = role.lanes_for_role(self.config, "lead")
        output = io.StringIO()
        with (
            mock.patch.object(role, "load_config", return_value=self.config),
            mock.patch.object(
                proxy,
                "snapshot",
                return_value={
                    "fetched_at": NOW,
                    "capacity": {
                        f"{l['kind']}/{l['model']}": [quota(0)] for l in lanes
                    },
                },
            ),
            mock.patch.object(role, "launch") as launch,
            contextlib.redirect_stdout(output),
        ):
            code = role.main(
                ["lead", "--launch", "--workspace", "w1", "--cwd", str(self.root)]
            )
        self.assertEqual(code, 75)
        launch.assert_not_called()
        self.assertIsNone(json.loads(output.getvalue())["selected"])

    def test_exec_preserves_cwd_and_does_not_spawn_a_wrapper(self):
        with (
            mock.patch.object(role, "load_config", return_value=self.config),
            mock.patch.object(
                proxy,
                "snapshot",
                return_value={
                    "fetched_at": NOW,
                    "capacity": {"codex/gpt-sol": [quota(60)]},
                },
            ),
            mock.patch.object(os, "chdir") as chdir,
            mock.patch.object(os, "execvp") as execute,
        ):
            self.assertEqual(
                role.main(
                    ["lead", "--exec", "--cwd", str(self.root), "--prompt", "do work"]
                ),
                0,
            )
        chdir.assert_called_once_with(self.root)
        self.assertEqual(execute.call_args.args[0], "codex")
        self.assertEqual(execute.call_args.args[1][-2:], ["--", "do work"])

    def test_plan_exposes_native_profile_without_task_or_credentials(self):
        output = io.StringIO()
        with (
            mock.patch.object(role, "load_config", return_value=self.config),
            mock.patch.object(
                proxy,
                "snapshot",
                return_value={
                    "fetched_at": NOW,
                    "capacity": {"codex/gpt-sol": [quota(60)]},
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                role.main(["lead", "--plan", "--prompt", "private task"]), 0
            )
        plan = json.loads(output.getvalue())["plan"]
        self.assertEqual(plan["kind"], "codex")
        self.assertEqual(plan["command"], ["codex"])
        self.assertEqual(
            plan["args"], ["--model", "gpt-sol", "-c", 'model_reasoning_effort="high"']
        )
        self.assertNotIn("private task", output.getvalue())

    def test_explain_does_not_print_task(self):
        output = io.StringIO()
        with (
            mock.patch.object(role, "load_config", return_value=self.config),
            mock.patch.object(
                proxy,
                "snapshot",
                return_value={
                    "fetched_at": NOW,
                    "capacity": {"codex/gpt-sol": [quota(60)]},
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                role.main(["lead", "--dry-run", "--prompt", "private prompt"]), 0
            )
        self.assertNotIn("private prompt", output.getvalue())

    def test_process_exec_receives_exact_task_and_cwd_without_management_key(self):
        import subprocess
        import sys

        native = self.root / "fake-native"
        receipt = self.root / "receipt.json"
        native.write_text(
            f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\nPath({str(receipt)!r}).write_text(json.dumps([sys.argv[1:], os.getcwd(), 'CLIPROXYAPI_MANAGEMENT_KEY' in os.environ]))\n"
        )
        native.chmod(0o755)
        config = self.root / "roles.toml"
        config.write_text(
            f"policy_file = {json.dumps(str(self.policy))}\n[proxy]\n[launchers.claude]\ncommand = [{json.dumps(str(native))}]\nproxy_routed = true\n[launchers.codex]\ncommand = [{json.dumps(str(native))}]\nproxy_routed = true\n"
        )
        task = 'exact task\n"quotes" $(not-executed) ; `literal`'
        script = "import role_launch as r; r.proxy.snapshot=lambda *a, **k: {'fetched_at': 1800000000, 'capacity': {'codex/gpt-sol': [{'state':'ok','remaining_percent':70,'reset_at':1800000100}]}}; r.main()"
        env = dict(os.environ, CLIPROXYAPI_MANAGEMENT_KEY="must-not-reach-native")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                "lead",
                "--config",
                str(config),
                "--exec",
                "--cwd",
                str(self.root),
                "--prompt",
                task,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual, cwd, has_key = json.loads(receipt.read_text())
        self.assertEqual(
            actual,
            ["--model", "gpt-sol", "-c", 'model_reasoning_effort="high"', "--", task],
        )
        self.assertEqual(cwd, str(self.root))
        self.assertFalse(has_key)

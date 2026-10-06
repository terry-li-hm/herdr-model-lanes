"""Strict-priority role selection, once before starting a native CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import holds as holdlib
import proxy_capacity as proxy


class PolicyError(Exception):
    pass


def load_config(path):
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("selection", "strict-priority") != "strict-priority":
        raise PolicyError("role selection must be strict-priority")
    if config.get("unknown_capacity", "skip") not in ("skip", "hold"):
        raise PolicyError("unknown_capacity must be skip or hold")
    headroom = config.get("headroom_percent", 5)
    if (
        isinstance(headroom, bool)
        or not isinstance(headroom, (int, float))
        or not 0 <= headroom < 100
    ):
        raise PolicyError("headroom_percent must be in [0, 100)")
    for name in ("policy_file", "instructions_root", "holds_file"):
        if config.get(name):
            config[name] = str(resolve_path(path.parent, config[name]))
    if config.get("proxy", {}).get("key_file"):
        config["proxy"]["key_file"] = str(
            resolve_path(path.parent, config["proxy"]["key_file"])
        )
    return config


def resolve_path(parent, value):
    path = Path(value).expanduser()
    return (parent / path).resolve() if not path.is_absolute() else path


def strings(value):
    if not isinstance(value, list) or not all(
        isinstance(v, str) and "\0" not in v for v in value
    ):
        raise PolicyError("expected an array of argument strings")
    return value


def native_args(values):
    """Accept additive native flags, never a second prompt, resume or routing override.

    Config has dedicated model/effort/command fields. An allowlist avoids subtle
    short-flag and config-file overrides that would invalidate the quota check.
    """
    values = strings(values)
    value_flags = {
        "--sandbox",
        "-s",
        "--ask-for-approval",
        "-a",
        "--permission-mode",
        "--add-dir",
        "--allowedTools",
        "--disallowedTools",
    }
    switches = {"--no-alt-screen", "--verbose", "--debug"}
    i = 0
    while i < len(values):
        flag = values[i]
        if flag in switches:
            i += 1
        elif flag in value_flags and i + 1 < len(values):
            i += 2
        else:
            raise PolicyError(
                "unsupported native argument; use dedicated model, effort, instructions and prompt fields"
            )
    return values


def lanes_for_role(config, role):
    document = json.loads(Path(config["policy_file"]).read_text())
    spec = document["roles"].get(role)
    if not isinstance(spec, dict):
        raise PolicyError("unknown role")
    if "primary" not in spec:
        spec = document.get("policies", {})[spec["policy"]]
    order = [spec["primary"], *spec.get("fallback", [])]
    if not order or len(order) != len(set(order)):
        raise PolicyError("role chain is empty or repeats a route")
    lanes = []
    previous_tier = 0
    for name in order:
        route = document["routes"][name]
        if route.get("qualified") is not True:
            continue
        tier = route.get("tier", 1)
        if tier > spec.get("maxTier", 999) or tier < previous_tier:
            continue
        previous_tier = tier
        # Read existing studio adapter records without importing their lifecycle
        # or permission modes into ordinary Herdr agents.
        adapter = route.get("adapterConfig", {})
        kind = route.get("kind") or adapter.get("provider")
        if route.get("adapterType") == "claude_local":
            kind = "claude"
        if kind not in proxy.URLS:
            raise PolicyError("qualified route uses an unsupported native runtime")
        model = route.get("model") or adapter.get("model")
        if not isinstance(model, str) or not model or model.startswith("-"):
            raise PolicyError("route needs an explicit model")
        launch = config["launchers"][kind]
        if launch.get("proxy_routed") is not True:
            raise PolicyError("launcher must explicitly declare proxy_routed = true")
        command = strings(launch.get("command", [kind]))
        if len(command) != 1 or not command[0]:
            raise PolicyError(
                "launcher command must contain one executable; put options in args"
            )
        args = native_args(launch.get("args", [])) + native_args(route.get("args", []))
        effort = (
            route.get("effort")
            or adapter.get("reasoningEffort")
            or adapter.get("effort")
        )
        args += ["--model", model]
        if effort:
            if kind == "claude":
                args += ["--effort", effort]
            else:
                args += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        lanes.append(
            {
                "name": name,
                "kind": kind,
                "model": model,
                "command": command,
                "args": args,
            }
        )
    if not lanes:
        raise PolicyError("role has no qualified allowed routes")
    return lanes


def choose(lanes, capacity, headroom=5, unknown="skip", holds=(), now=None):
    decisions = []
    selected = None
    for lane in lanes:
        hold = holdlib.lane_hold(holds, lane["kind"], lane["model"], now)
        if hold:
            decisions.append(
                {
                    "route": lane["name"],
                    "kind": lane["kind"],
                    "model": lane["model"],
                    "state": "held",
                    "hold": holdlib.label(hold),
                    "reason": hold.get("reason"),
                    "accounts": [],
                    "retry_at": hold.get("until"),
                }
            )
            continue
        accounts = capacity.get(f"{lane['kind']}/{lane['model']}", [])
        if accounts and all(a["state"] == "held" for a in accounts):
            decisions.append(
                {
                    "route": lane["name"],
                    "kind": lane["kind"],
                    "model": lane["model"],
                    "state": "held",
                    "accounts": accounts,
                    "retry_at": min((a["reset_at"] or 0) for a in accounts) or None,
                }
            )
            continue
        accounts = [a for a in accounts if a["state"] != "held"]
        usable = [
            a
            for a in accounts
            if a["state"] == "ok" and a["remaining_percent"] > headroom
        ]
        uncertain = [
            a for a in accounts if a["state"] in ("unknown", "stale", "unavailable")
        ]
        if usable:
            state = "eligible"
        elif uncertain:
            state = "unknown"
        elif any(a["state"] == "auth_error" for a in accounts):
            state = "auth_error"
        elif accounts:
            state = (
                "exhausted"
                if all(
                    a["state"] == "exhausted"
                    or (a["state"] == "ok" and a["remaining_percent"] == 0)
                    for a in accounts
                )
                else "headroom"
            )
        else:
            state = "no_accounts"
        resets = [a["reset_at"] for a in accounts if a.get("reset_at")]
        decisions.append(
            {
                "route": lane["name"],
                "kind": lane["kind"],
                "model": lane["model"],
                "state": state,
                "accounts": accounts,
                "retry_at": min(resets) if resets else None,
            }
        )
        if state == "eligible":
            selected = lane
            break
        if state == "unknown" and unknown == "hold":
            break
    return selected, decisions


def initial_prompt(config, role, instructions, task):
    files = list(instructions)
    if config.get("instructions_root"):
        root = Path(config["instructions_root"]).resolve()
        candidate = (root / role / "AGENTS.md").resolve()
        if not candidate.is_relative_to(root):
            raise PolicyError("invalid role instruction path")
        files.insert(0, candidate)
    parts = []
    for path in files:
        path = Path(path).resolve()
        contents = path.read_text()
        parts.append(
            f"Role instructions from {path}. Resolve relative references against {path.parent}.\n\n"
            + contents
        )
    if task is not None:
        parts.append(task)
    return "\n\n".join(parts) if parts else None


def command_for(lane, prompt, extra):
    argv = [*lane["command"], *lane["args"], *native_args(extra.get(lane["kind"], []))]
    if prompt is not None:
        argv.extend(["--", prompt])
    return argv


def herdr_call(args):
    try:
        proc = subprocess.run(
            ["herdr", *args], capture_output=True, text=True, timeout=30, check=False
        )
        if proc.returncode:
            raise PolicyError(
                "Herdr command failed; inspect the created pane before retrying"
            )
        return json.loads(proc.stdout)["result"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        raise PolicyError(
            "Herdr command failed; inspect the created pane before retrying"
        ) from None


def launch(workspace, cwd, argv, call=herdr_call):
    # No custom label: Herdr and activity-title plugins retain naming ownership.
    created = call(
        ["tab", "create", "--workspace", workspace, "--cwd", str(cwd), "--no-focus"]
    )
    pane = created["root_pane"]["pane_id"]
    # Use the declared managed executable. The exec'd native CLI remains visible
    # to Herdr detection/hooks. Shell quoting preserves every argument verbatim.
    try:
        call(["pane", "run", pane, "exec " + shlex.join(argv)])
    except PolicyError:
        raise PolicyError(
            f"launch failed in {pane}; inspect it, do not automatically retry"
        ) from None
    return {"pane_id": pane, "tab_id": created["tab"]["tab_id"]}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("role")
    p.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "MODEL_LANES_ROLE_CONFIG",
                Path.home() / ".config/herdr-model-lanes/roles.toml",
            )
        ),
    )
    action = p.add_mutually_exclusive_group()
    action.add_argument(
        "--launch", action="store_true", help="create one ordinary Herdr tab"
    )
    action.add_argument(
        "--exec", action="store_true", help="replace this process with the native CLI"
    )
    action.add_argument(
        "--plan",
        action="store_true",
        help="emit structured selection and native launch fields, without prompt",
    )
    action.add_argument(
        "--argv",
        action="store_true",
        help="emit exact argv JSON, including supplied prompt",
    )
    action.add_argument(
        "--explain",
        "--dry-run",
        action="store_true",
        help="sanitized decision JSON (default)",
    )
    p.add_argument(
        "--workspace", help="explicit Herdr workspace ID, required for --launch"
    )
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    task = p.add_mutually_exclusive_group()
    task.add_argument("--prompt")
    task.add_argument("--prompt-file", type=Path)
    p.add_argument("--instructions-file", type=Path, action="append", default=[])
    p.add_argument(
        "--args-file",
        type=Path,
        help="JSON native argument arrays keyed by claude/codex",
    )
    p.add_argument(
        "--refresh", action="store_true", help="bypass the short normalized quota cache"
    )
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.launch and not args.workspace:
            raise PolicyError("--launch requires an explicit --workspace")
        cwd = args.cwd.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise PolicyError("cwd must be a directory")
        config = load_config(args.config.expanduser())
        lanes = lanes_for_role(config, args.role)
        task = args.prompt_file.read_text() if args.prompt_file else args.prompt
        prompt = initial_prompt(config, args.role, args.instructions_file, task)
        extra = json.loads(args.args_file.read_text()) if args.args_file else {}
        if not isinstance(extra, dict) or set(extra) - {"claude", "codex"}:
            raise PolicyError("args file must map native runtimes to argument arrays")
        for value in extra.values():
            native_args(value)
        cache_dir = Path(
            os.environ.get(
                "HERDR_PLUGIN_STATE_DIR",
                os.environ.get(
                    "MODEL_LANES_STATE_DIR",
                    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                    / "herdr/plugins/terry.herdr-model-lanes",
                ),
            )
        )
        holds = holdlib.load(config.get("holds_file"))
        snap = proxy.snapshot(
            config["proxy"],
            [(l["kind"], l["model"]) for l in lanes],
            cache_dir,
            args.refresh,
            holds=holds,
        )
        selected, decisions = choose(
            lanes,
            snap["capacity"],
            config.get("headroom_percent", 5),
            config.get("unknown_capacity", "skip"),
            holds,
        )
        report = {
            "role": args.role,
            "selected": selected["name"] if selected else None,
            "decisions": decisions,
            "fetched_at": snap["fetched_at"],
            "age_seconds": round(max(0, time.time() - snap["fetched_at"]), 2),
        }
        if not selected:
            print(json.dumps(report))
            return 75
        command = command_for(selected, prompt, extra)
        if args.plan:
            report["plan"] = selected
            print(json.dumps(report))
        elif args.argv:
            print(json.dumps(command))
        elif args.exec:
            os.environ.pop(
                config["proxy"].get("key_env", "CLIPROXYAPI_MANAGEMENT_KEY"), None
            )
            os.chdir(cwd)
            os.execvp(command[0], command)
        elif args.launch:
            report["launch"] = launch(args.workspace, cwd, command)
            print(json.dumps(report))
        else:
            print(json.dumps(report))
        return 0
    except (PolicyError, proxy.CapacityError) as exc:
        print(f"role: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Do not leak config contents, upstream response bodies or paths through
        # an exception repr. Configuration files may reference credential files.
        print(
            "role: invalid or unreadable configuration, instructions, or capacity response",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())

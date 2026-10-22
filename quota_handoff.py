"""Linux, opt-in, one-shot quota handoff. No replay, thaw, daemon or shell tasks.

Only quota helpers cross the credential boundary. Terminal text is corroborated
locally against one exact session file; neither text nor transcripts leave here.
A lost mutation response is deliberately unrecoverable without owner review.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import signal
import socket
import stat
import string
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PLUGIN_ID = "terry.herdr-quota-handoff"
CLAUDE_PROFILE_ORDER = ("c", "c2", "c3", "c4")
CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_PI_PROVIDER = "openai-codex"
DEFAULT_PI_MODEL = "gpt-6.1-sol"
POSITIVE_CACHE_TTL, NEGATIVE_CACHE_TTL = 300, 60
SUBPROCESS_TIMEOUT = 20
MAX_FILE = 8 * 1024 * 1024
MAX_TRANSCRIPT = 128 * 1024 * 1024
MAX_TRANSCRIPT_LINE = 4 * 1024 * 1024
TRANSCRIPT_TIMEOUT = 5.0
MAX_AGE = 120
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
NOTICE = re.compile(r"^\s*(?:⚠[\ufe0e\ufe0f]?\s*)?(?:you have hit your (?:(?:session|weekly) )?limit\b|you['’]ve hit your (?:(?:session|weekly) )?limit\b|usage limit reached\b|you have reached your (?:(?:session|weekly|usage) )?limit\b|usage_limit_reached\b)", re.IGNORECASE)
PI_NOTICE = re.compile(r"^\s*(?:Error:\s*)?you have hit your ChatGPT usage limit\b", re.IGNORECASE)
FOOTER = re.compile(r"^\s*(?:Continuing automatically at .+ · esc to cancel · /usage-credits to continue now|Automatically resuming .+|[─━\-]{3,}|[❯>]\s*|\? for shortcuts|(?:Opus|Sonnet|Fable) [\d.]+(?:\s*[·|].*)?|(?:⏵⏵|▸▸) (?:accept edits|bypass permissions).*|⏸[\ufe0e\ufe0f]? plan mode on \(shift\+tab to cycle\))\s*$", re.IGNORECASE)
# Values of these keys are NEVER returned/read as credential values.
OVERRIDE_KEYS = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_FD",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "CODEX_API_KEY", "CODEX_HOME",
    "PI_API_KEY", "PI_PROVIDER", "PI_MODEL", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR",
    "CLAUDE_CODE_SETTINGS", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "NODE_OPTIONS", "BASH_ENV", "ENV", "LD_PRELOAD",
})
CHILD_CLEAR_ENV_KEYS = tuple(OVERRIDE_KEYS)
SAFE_ENV_KEYS = frozenset({"HOME", "CLAUDE_CONFIG_DIR"})
AUTH_CONFIG_KEYS = frozenset({
    "apikey", "apikeyenv", "baseurl", "base_url", "headers", "auth", "authmethod",
    "authtype", "provider", "providerid", "endpoint", "url", "env", "envoverrides",
    "runtimetoken", "modelprovider", "modelproviders", "apikeyhelper", "providers", "api", "api_key",
})


class HandoffError(Exception):
    """Errors use fixed metadata-only messages, never external output."""


REASON_CODES = {
    "transcript size bound exceeded": "transcript-size-bound",
    "transcript line bound exceeded": "transcript-line-bound",
    "transcript deadline exceeded": "transcript-deadline",
    "unsupported native session continuation": "unsupported-continuation",
    "transcript changed during read": "transcript-changed",
    "no current exact-source quota error": "no-current-source-error",
    "source identity drift": "source-identity-drift",
    "source or server identity drift": "source-or-server-drift",
    "unproven server launch environment": "unproven-server-environment",
    "unsafe destination settings": "unsafe-destination-settings",
    "unproven native source environment": "unproven-source-environment",
    "unproven Pi source environment": "unproven-source-environment",
    "unproven Pi subscription": "unproven-pi-subscription",
    "unproven Pi subscription exhaustion": "unproven-pi-exhaustion",
    "active descendants outside source group": "outside-descendants",
    "active descendant after stop": "outside-descendants-after-stop",
    "ambiguous foreground membership": "ambiguous-process-group",
    "unsafe foreground member": "unsafe-process-member",
    "replacement not exactly ready": "replacement-not-ready",
    "destination pane mismatch": "destination-identity-mismatch",
    "destination session drift": "destination-session-drift",
    "destination capacity no longer known": "destination-capacity-lost",
    "stop verification timed out": "stop-not-verified",
    "stopped identity drift": "stopped-identity-drift",
    "corrupt receipt": "corrupt-receipt",
    "corrupt account exclusions": "corrupt-account-exclusions",
}


def refusal_reason(exc):
    if isinstance(exc, HandoffError):
        return REASON_CODES.get(str(exc), "boundary-refused")
    if isinstance(exc, subprocess.TimeoutExpired):
        return "operation-timeout"
    if isinstance(exc, OSError):
        return "local-io-failure"
    return "malformed-boundary-data"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _to_epoch(value):
    if _finite(value):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.timestamp() if dt.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def is_limit_notice(text):
    return isinstance(text, str) and any(NOTICE.match(line) for line in text.splitlines())


def tail_notice(text, *, pi=False):
    if not isinstance(text, str):
        return False
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if NOTICE.match(line) or (pi and PI_NOTICE.match(line))]
    return bool(hits) and all(not line.strip() or FOOTER.fullmatch(line)
                              for line in lines[hits[-1] + 1:])


def parse_window(value, now, *, codex=False, unused=False, exhausted=False):
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent" if codex else "utilization")
    reset = value.get("resetsAt" if codex else "resets_at")
    if not _finite(used) or not 0 <= used <= (100 if exhausted else 99.999999999):
        return None
    if unused and used == 0 and "resets_at" in value and reset is None:
        return {"utilization": 0.0, "resets_at": None, "unused": True}
    reset = _to_epoch(reset)
    if reset is None or not now < reset <= now + 366 * 86400:
        return None
    if codex and (not _finite(value.get("windowDurationMins")) or value["windowDurationMins"] <= 0):
        return None
    return {"utilization": float(used), "resets_at": reset}


def validate_claude_payload(payload, now, *, exhausted=False):
    """weekly_scoped is the separately probed --scoped result, including null."""
    if not isinstance(payload, dict) or "weekly_scoped" not in payload:
        return None
    result = {}
    for name in ("five_hour", "seven_day"):
        window = parse_window(payload.get(name), now, unused=name == "five_hour", exhausted=exhausted)
        if window is None:
            return None
        result[name] = window
    # Validate all non-null reported windows, even if not applicable to Opus.
    for name, raw in payload.items():
        if name.startswith("seven_day_") and raw is not None:
            window = parse_window(raw, now, exhausted=True)
            if window is None:
                return None
            if name == "seven_day_opus":
                result[name] = window
    scoped = payload["weekly_scoped"]
    if scoped is not None:
        if not isinstance(scoped, dict) or scoped.get("scope") not in ("Opus", "Fable"):
            return None
        window = parse_window(scoped, now, exhausted=True)
        if window is None:
            return None
        if scoped["scope"] == "Opus":
            result["opus_scope"] = window
    return result


def _bucket_relevant(name, model):
    name = name.lower()
    return "codex" in name or "sol" in name or "gpt-6" in name or name == model.lower()


def validate_codex_snapshot(payload, now, model=DEFAULT_PI_MODEL, *, exhausted=False):
    """Official account/read and account/rateLimits/read results, unflattened."""
    if not isinstance(payload, dict):
        return None
    account_result = payload.get("account")
    if not isinstance(account_result, dict):
        return None
    account = account_result.get("account")
    if (not isinstance(account, dict) or account.get("type") != "chatgpt"
            or account.get("planType") not in ("plus", "pro", "team", "business", "enterprise", "edu")
            or account_result.get("requiresOpenaiAuth") is not True
            or account_result.get("authMethod") not in (None, "chatgpt")):
        return None
    report = payload.get("limits")
    if (not isinstance(report, dict) or type(report.get("ordinaryUsageAllowed")) is not bool
            or (not exhausted and report["ordinaryUsageAllowed"] is not True)):
        return None
    buckets = report.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict):
        return None
    snapshots = [("ordinary", report.get("rateLimits"))]
    snapshots.extend((name, bucket) for name, bucket in buckets.items()
                     if isinstance(name, str) and _bucket_relevant(name, model))
    result = {}
    for name, snapshot in snapshots:
        if (not isinstance(snapshot, dict) or type(snapshot.get("spendControlReached")) is not bool
                or type(snapshot.get("rateLimitReached", False)) is not bool
                or type(snapshot.get("ordinaryUsageAllowed", True)) is not bool
                or snapshot.get("rateLimitReachedType") is not None and not isinstance(snapshot["rateLimitReachedType"], str)):
            return None
        if snapshot["spendControlReached"]:
            return None  # Billing/spend restrictions never authorize source rotation either.
        reached = snapshot.get("rateLimitReachedType")
        if exhausted and reached not in (None, "primary", "secondary", "weekly", "five_hour",
                                        "usage_limit_reached", "rate_limit_exceeded"):
            return None
        if not exhausted and (reached is not None or snapshot.get("rateLimitReached", False)
                              or not snapshot.get("ordinaryUsageAllowed", True)):
            return None
        if "secondary" not in snapshot:
            return None
        for key in ("primary", "secondary"):
            raw = snapshot.get(key)
            if key == "secondary" and raw is None:
                continue
            window = parse_window(raw, now, codex=True, exhausted=exhausted)
            if window is None:
                return None
            result[f"{name}:{key}"] = window
    if exhausted and not any(w["utilization"] == 100 for w in result.values()):
        return None  # The SDK's friendly message also represents generic HTTP 429s.
    return result


def validate_pi_auth(payload):
    return (isinstance(payload, dict) and payload.get("status") == "ready"
            and payload.get("provider") == DEFAULT_PI_PROVIDER and payload.get("authType") == "oauth"
            and not any(payload.get(k) for k in ("api_key_override", "endpoint_override", "provider_override")))


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise HandoffError("unsafe state directory")
    os.chmod(path, 0o700)


def read_file(path, limit=MAX_FILE, *, private=False):
    """Bounded regular-file read; no symlinks or mid-read replacement."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise HandoffError("unsafe or oversized file")
        if private and (before.st_uid != os.getuid() or before.st_mode & 0o077 or before.st_nlink != 1):
            raise HandoffError("file requires private owner authority")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(data) > limit or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise HandoffError("file changed during read")
        return data


def atomic_json(path, value):
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    # On failure leave the private temp file for owner inspection, not replay.
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class HandoffConfig:
    enabled: bool = False
    allowed_workspaces: list = field(default_factory=list)
    claude_profiles: list = field(default_factory=list)
    claude_model: str = CLAUDE_MODEL
    pi_provider: str = DEFAULT_PI_PROVIDER
    pi_model: str = DEFAULT_PI_MODEL
    authority: dict = field(default_factory=dict)
    auto_prompt_template: dict | None = None
    minimum_remaining_percent: float = 5.0
    launch_cwd_allowlist: list = field(default_factory=list)
    test_source_allowlist: list = field(default_factory=list)
    test_claude_launch_args: list = field(default_factory=list)

    @classmethod
    def load(cls, path):
        try:
            raw = json.loads(read_file(Path(path).expanduser(), 65536))
            if not isinstance(raw, dict):
                return cls()
            return cls(**{key: value for key, value in raw.items() if key in cls.__dataclass_fields__})
        except (OSError, ValueError, HandoffError, TypeError):
            return cls()

    def authorized(self):
        return (self.enabled is True and isinstance(self.allowed_workspaces, list)
                and bool(self.allowed_workspaces) and all(isinstance(x, str) and x for x in self.allowed_workspaces)
                and isinstance(self.authority, dict)
                and all(isinstance(self.authority.get(k), str) and self.authority[k].strip()
                        for k in ("owner", "provenance"))
                and isinstance(self.claude_profiles, list) and len(self.profiles()) == len(self.claude_profiles)
                and all(isinstance(items, list) and all(isinstance(x, str) and x for x in items)
                        for items in (self.launch_cwd_allowlist, self.test_source_allowlist))
                and isinstance(self.test_claude_launch_args, list)
                and all(isinstance(x, str) for x in self.test_claude_launch_args)
                and self.claude_model == CLAUDE_MODEL and self.pi_provider == DEFAULT_PI_PROVIDER
                and self.pi_model == DEFAULT_PI_MODEL and _finite(self.minimum_remaining_percent)
                and 5 <= self.minimum_remaining_percent <= 100)

    def profiles(self):
        if not isinstance(self.claude_profiles, list):
            return []
        out, names, dirs = [], set(), set()
        for entry in self.claude_profiles:
            if not isinstance(entry, dict) or entry.get("name") not in CLAUDE_PROFILE_ORDER:
                return []
            cd = entry.get("config_dir")
            if not isinstance(cd, str) or not os.path.isabs(os.path.expanduser(cd)):
                return []
            cd = os.path.abspath(os.path.expanduser(cd))
            if entry["name"] in names or cd in dirs:
                return []
            names.add(entry["name"])
            dirs.add(cd)
            out.append({"name": entry["name"], "config_dir": cd})
        return out

    def profile_config_dir(self, name):
        return next((p["config_dir"] for p in self.profiles() if p["name"] == name), None)


def default_config_path():
    base = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(base).expanduser() / "handoff.json" if base else None


def default_state_dir():
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(base).expanduser() / "handoff" if base else None


class OsPort:
    platform = sys.platform

    def uid(self):
        return os.getuid()

    def now(self):
        return time.time()

    def process(self, pid):
        try:
            data = Path(f"/proc/{pid}/stat").read_text()
            fields = data[data.rindex(")") + 2:].split()
            owner = os.stat(f"/proc/{pid}").st_uid
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe = ""  # Still enumerate foreign/zombie members; never hide them.
            image = exe.removesuffix(" (deleted)")
            native = os.path.basename(image) if os.path.basename(image) in ("claude", "pi") else None
            if re.fullmatch(r".*/\.local/share/claude/versions/\d+(?:\.\d+)+(?:-[\w.]+)?", image):
                native = "claude"
            if os.path.basename(image) in ("node", "bun"):
                # Inspect only the script slot, not arbitrary prompt/credential argv.
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    slots, token = [], bytearray()
                    for _ in range(8192):
                        byte = handle.read(1)
                        if not byte:
                            break
                        if byte == b"\0":
                            slots.append(bytes(token))
                            token.clear()
                            if len(slots) == 2:
                                break
                        else:
                            token.extend(byte)
                script = slots[1].decode("utf-8", "replace") if len(slots) == 2 else ""
                if script.startswith("/"):
                    script = str(Path(script).resolve())
                if re.fullmatch(r"/.*/node_modules/@(?:earendil-works|mariozechner)/pi-coding-agent/dist/cli\.js", script):
                    native = "pi"
            return {"pid": pid, "state": fields[0], "ppid": int(fields[1]), "pgid": int(fields[2]),
                    "start": int(fields[19]), "uid": owner, "exe": exe, "native": native}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, IndexError):
            raise HandoffError("process identity unavailable") from None

    def table(self):
        try:
            entries = os.listdir("/proc")
        except OSError:
            raise HandoffError("process table unavailable") from None
        result = {}
        for entry in entries:
            if entry.isdigit():
                process = self.process(int(entry))
                if process is not None:
                    result[process["pid"]] = process
        return result

    def environment(self, pid):
        """Only safe config paths and presence of override keys; never token values."""
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                data = handle.read(1 << 20)
            if len(data) == 1 << 20:
                return None
        except OSError:
            return None
        result = {"overrides": False}
        for entry in data.split(b"\0"):
            key, sep, value = entry.partition(b"=")
            key = key.decode("utf-8", "replace")
            if sep and value:
                if key in OVERRIDE_KEYS or key.startswith(("OPENAI_", "ANTHROPIC_", "PI_AUTH_")):
                    result["overrides"] = True
                if key in SAFE_ENV_KEYS:
                    result[key] = value.decode("utf-8", "strict")
        return result

    def server_peer(self):
        """Kernel-authenticate the configured Unix listener; no application request."""
        path = os.environ.get("HERDR_SOCKET_PATH")
        if not path or not os.path.isabs(path):
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.5)
                connection.connect(path)
                pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                return pid if uid == self.uid() and pid > 1 else None
        except OSError:
            return None

    def launch_environment(self):
        """Prove the actual server ancestor, not the CLI client's cleaned env.

        Non-ancestor daemons cannot be proved by this adapter and are refused.
        The executable must really be herdr; argv/process-info labels aren't proof.
        """
        peer = self.server_peer()
        if peer is None:
            return None
        pid, seen = os.getppid(), set()
        for _ in range(32):
            if pid in seen or pid <= 1:
                break
            seen.add(pid)
            process = self.process(pid)
            if process is None or process["uid"] != self.uid():
                return None
            if os.path.basename(process["exe"].removesuffix(" (deleted)")) == "herdr" and pid == peer:
                env = self.environment(pid)
                if env is None or env["overrides"] or not env.get("HOME"):
                    return None
                return {"pid": pid, "start": process["start"], "env": env}
            pid = process["ppid"]
        return None

    def killpg(self, pgid, sig):
        os.killpg(pgid, sig)


class HerdrPort:
    def __init__(self, herdr_bin=None, runner=None):
        self.herdr_bin = herdr_bin or os.environ.get("HERDR_BIN_PATH") or "herdr"
        self.runner = runner

    def _execute(self, args):
        if self.runner:
            return self.runner([self.herdr_bin, *args])
        try:
            completed = subprocess.run([self.herdr_bin, *args], capture_output=True, text=True,
                                       timeout=SUBPROCESS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HandoffError("Herdr invocation failed") from None
        return completed.returncode, completed.stdout

    def _result(self, args, key):
        rc, text = self._execute(args)
        try:
            document = json.loads(text)
            value = document["result"][key]
            expected = list if key in ("agents", "panes", "workspaces") else dict
            if rc or document.get("ok") is False or not isinstance(value, expected):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise HandoffError("invalid Herdr response") from None

    def workspace_list(self):
        return self._result(["workspace", "list"], "workspaces")

    def agent_list(self):
        return self._result(["agent", "list"], "agents")

    def agent_get(self, pane):
        return self._result(["agent", "get", pane], "agent")

    def agent_snapshot(self, pane):
        agent = self.agent_get(pane)
        rc, text = self._execute(["agent", "read", pane, "--source", "recent-unwrapped", "--lines", "30", "--format", "text"])
        if rc or len(text) > 65536:
            raise HandoffError("invalid Herdr terminal read")
        return {"agent": agent, "text": text}

    def pane_list(self, workspace):
        return self._result(["pane", "list", "--workspace", workspace], "panes")

    def pane_get(self, pane):
        return self._result(["pane", "get", pane], "pane")

    def process_info(self, pane):
        return self._result(["pane", "process-info", "--pane", pane], "process_info")

    def tab_create(self, workspace_id, cwd, label, env=None, no_focus=True):
        args = ["tab", "create", "--workspace", workspace_id, "--cwd", cwd, "--label", label, "--no-focus"]
        for key, value in sorted((env or {}).items()):
            args += ["--env", f"{key}={value}"]
        # The CLI returns result.tab and result.root_pane, not result.tab.root_pane.
        rc, text = self._execute(args)
        try:
            document = json.loads(text)
            result = document["result"]
            if rc or document.get("ok") is False or not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise HandoffError("ambiguous tab create") from None

    def agent_start(self, name, kind, pane, args):
        return self._result(["agent", "start", name, "--kind", kind, "--pane", pane, "--", *args], "agent")

    def agent_prompt(self, pane, text):
        return self._result(["agent", "prompt", pane, text, "--wait", "--timeout", "15000"], "agent")

    def notification_show(self, destination_pane, destination_tab, source_pane):
        try:
            self._execute(["notification", "show", "Quota handoff prepared", "--body",
                           (f"New pane {destination_pane}, tab {destination_tab}, follows source pane {source_pane}. "
                            "The original remains parked; Terry can request continuation."), "--sound", "none"])
        except HandoffError:
            pass


def config_overrides(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "env":
                if not isinstance(item, dict) or any(
                        not isinstance(k, str) or not isinstance(v, str) or k in OVERRIDE_KEYS
                        or k.startswith(("OPENAI_", "ANTHROPIC_", "PI_AUTH_")) for k, v in item.items()):
                    return True
            elif str(key).lower() in AUTH_CONFIG_KEYS or config_overrides(item):
                return True
        return False
    if isinstance(value, list):
        return any(config_overrides(v) for v in value)
    return False


def optional_config(path):
    try:
        value = json.loads(read_file(path, 262144))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise HandoffError("invalid launch configuration")
    return value


def launch_settings_safe(env, cwd, config_dir=None):
    """Inspect server-selected and project-local noncredential configuration."""
    try:
        home = Path(env["HOME"]).expanduser()
        pi = home / ".pi" / "agent"
        models = optional_config(pi / "models.json")
        if models is not None:
            providers = models.get("providers")
            if not isinstance(providers, dict):
                return False
            entry = providers.get(DEFAULT_PI_PROVIDER, {})
            if not isinstance(entry, dict) or set(entry) - {"models", "name"} or config_overrides(entry):
                return False
        paths = [pi / "settings.json", Path(cwd) / ".pi" / "settings.json",
                 home / ".claude" / "settings.json", Path(cwd) / ".claude" / "settings.json",
                 Path(cwd) / ".claude" / "settings.local.json"]
        if config_dir:
            paths.append(Path(config_dir) / "settings.json")
        for path in paths:
            value = optional_config(path)
            if value is not None and config_overrides(value):
                return False
        return True
    except (OSError, ValueError, KeyError, HandoffError):
        return False


class QuotaPort:
    def __init__(self, state_dir, helper_path=None):
        self.state_dir = Path(state_dir)
        self.helper_path = helper_path or Path(__file__).with_name("claude_max_usage.py")
        self.probe_home = None

    def probe_env(self):
        env = {k: v for k, v in os.environ.items() if k not in OVERRIDE_KEYS
               and not k.startswith(("OPENAI_", "ANTHROPIC_", "PI_AUTH_"))}
        if self.probe_home:
            env["HOME"] = self.probe_home
        return env

    def build_child_env(self, config_dir, state):
        env = self.probe_env()
        env.update(CLAUDE_CONFIG_DIR=str(Path(config_dir).expanduser()),
                   HERDR_PLUGIN_STATE_DIR=str(state), MODEL_LANES_STATE_DIR=str(state))
        return env

    def claude_usage(self, profile, now):
        state = self.state_dir / ("profile-" + digest(profile))
        private_dir(state)
        results = []
        for flags in ([], ["--scoped"]):
            try:
                completed = subprocess.run([sys.executable, str(self.helper_path), *flags],
                                           env=self.build_child_env(profile["config_dir"], state),
                                           capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False)
                if completed.returncode:
                    raise HandoffError("Claude quota probe failed")
                results.append(json.loads(completed.stdout))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise HandoffError("Claude quota probe failed") from None
        if not isinstance(results[0], dict):
            raise HandoffError("invalid Claude quota result")
        return {**results[0], "weekly_scoped": results[1]}

    def pi_auth_check(self):
        try:
            completed = subprocess.run(["pi", "auth", "check", "--provider", DEFAULT_PI_PROVIDER,
                                        "--model", DEFAULT_PI_MODEL, "--json", "--no-refresh"],
                                       env=self.probe_env(), capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False)
            return json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise HandoffError("Pi auth probe failed") from None

    def codex_windows(self, now, timeout=SUBPROCESS_TIMEOUT):
        """Nonblocking pipe I/O, absolute deadline and bounded protocol buffer."""
        try:
            process = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"],
                                       env=self.probe_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise HandoffError("Codex probe unavailable") from None
        deadline, buffer = time.monotonic() + timeout, bytearray()
        selector = selectors.DefaultSelector()
        try:
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)

            def rpc(req_id, method, params):
                if time.monotonic() >= deadline:
                    raise HandoffError("Codex deadline exceeded")
                request = {"jsonrpc": "2.0", "method": method, "params": params}
                if req_id is not None:
                    request["id"] = req_id
                data = json.dumps(request).encode() + b"\n"
                # These fixed small requests fit PIPE_BUF; refuse blocked/partial writes.
                if os.write(process.stdin.fileno(), data) != len(data):
                    raise HandoffError("Codex input unavailable")
                if req_id is None:
                    return None
                while True:
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer[:] = rest
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise HandoffError("invalid Codex protocol")
                        if time.monotonic() >= deadline:
                            raise HandoffError("Codex deadline exceeded")
                        if message.get("id") == req_id:
                            if "error" in message or not isinstance(message.get("result"), dict):
                                raise HandoffError("Codex RPC failed")
                            return message["result"]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise HandoffError("Codex deadline exceeded")
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        raise HandoffError("Codex output closed")
                    buffer.extend(data)
                    if len(buffer) > 262144:
                        raise HandoffError("Codex output exceeded bound")

            rpc(1, "initialize", {"clientInfo": {"name": PLUGIN_ID, "version": "1"}})
            rpc(None, "initialized", {})
            account = rpc(2, "account/read", {"refreshToken": False})
            limits = rpc(3, "account/rateLimits/read", {})
            return {"account": account, "limits": limits}
        except (OSError, ValueError):
            raise HandoffError("Codex protocol failed") from None
        finally:
            selector.close()
            process.stdin.close()
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def session_token(agent):
    session = agent.get("agent_session")
    if (not isinstance(session, dict) or session.get("agent") != agent.get("agent")
            or session.get("kind") not in ("id", "path")
            or not isinstance(session.get("source"), str) or not session["source"]
            or not isinstance(session.get("value"), str) or not session["value"]):
        raise HandoffError("unknown source session")
    return digest(session)


IDENTITY_FIELDS = ("pane_id", "tab_id", "terminal_id", "workspace_id", "cwd", "agent", "agent_session")


def identity(agent):
    if not isinstance(agent, dict) or any(not agent.get(k) for k in IDENTITY_FIELDS):
        raise HandoffError("incomplete agent identity")
    session_token(agent)
    return {k: agent[k] for k in IDENTITY_FIELDS}


def event_key(agent):
    return digest(identity(agent))


def source_file(agent, group, os_port):
    """Claude UUID -> exact native projects path; Pi -> exact explicit session file."""
    session = agent["agent_session"]
    if agent["agent"] == "claude":
        envs = [os_port.environment(pid) for pid in group["native"]]
        if not envs or any(e is None or e.get("overrides") or not e.get("HOME") or not os.path.isabs(e["HOME"]) for e in envs):
            raise HandoffError("unproven native source environment")
        dirs = {os.path.abspath(os.path.expanduser(e.get("CLAUDE_CONFIG_DIR") or str(Path(e.get("HOME", "")) / ".claude"))) for e in envs if e.get("HOME")}
        if len(dirs) != 1:
            raise HandoffError("ambiguous native profile")
        config_dir = dirs.pop()
        value = session["value"]
        sid = Path(value).stem if session["kind"] == "path" else value
        if not UUID.fullmatch(sid):
            raise HandoffError("unknown native session ID")
        slug = re.sub(r"[^a-zA-Z0-9]", "-", agent["cwd"])
        path = Path(config_dir) / "projects" / slug / (sid + ".jsonl")
        if session["kind"] == "path" and os.path.abspath(os.path.expanduser(value)) != str(path):
            raise HandoffError("native transcript path mismatch")
        return path, sid, config_dir
    for pid in group["native"]:
        env = os_port.environment(pid)
        if env is None or env.get("overrides") or not launch_settings_safe(env, agent["cwd"]):
            raise HandoffError("unproven Pi source environment")
    if session["kind"] != "path" or not os.path.isabs(os.path.expanduser(session["value"])):
        raise HandoffError("unknown Pi session path")
    return Path(session["value"]).expanduser(), None, None


def transcript_lines(path, hasher):
    """Stream one exact regular file, with total/line/time bounds and no tail guessing."""
    deadline = time.monotonic() + TRANSCRIPT_TIMEOUT
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise HandoffError("unsafe transcript file")
        if before.st_size > MAX_TRANSCRIPT:
            raise HandoffError("transcript size bound exceeded")
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise HandoffError("transcript deadline exceeded")
            line = handle.readline(MAX_TRANSCRIPT_LINE + 1)
            if time.monotonic() >= deadline:
                raise HandoffError("transcript deadline exceeded")
            if not line:
                break
            total += len(line)
            if len(line) > MAX_TRANSCRIPT_LINE:
                raise HandoffError("transcript line bound exceeded")
            if total > MAX_TRANSCRIPT:
                raise HandoffError("transcript size bound exceeded")
            hasher.update(line)
            yield line
        after = os.fstat(handle.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or total != before.st_size:
            raise HandoffError("transcript changed during read")


def transcript_metadata(path, agent, sid, now):
    """Only error/model/session metadata and hashes escape this exact-file parser."""
    hasher = hashlib.sha256()
    latest, last_user, model = None, None, None
    # Claude's source identity is already bound by native env + cwd slug + UUID
    # filename. A sessionId is optional in records, but any reported ID must match.
    seen_session = agent["agent"] == "claude" and path.name == sid + ".jsonl"
    for line in transcript_lines(path, hasher):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise HandoffError("invalid transcript")
        kind = entry.get("type")
        if kind == "continued-in":
            raise HandoffError("unsupported native session continuation")
        if agent["agent"] == "claude":
            if entry.get("sessionId") is not None:
                if entry["sessionId"] != sid:
                    raise HandoffError("transcript session mismatch")
                seen_session = True
            message = entry.get("message")
            if kind not in ("user", "assistant"):
                continue
            if entry.get("isSidechain") is True:
                raise HandoffError("unproven native conversation")
        else:
            if kind == "session":
                if (seen_session or not isinstance(entry.get("id"), str)
                        or not UUID.fullmatch(entry["id"]) or entry.get("cwd") != agent["cwd"]
                        or not path.stem.endswith(entry["id"])):
                    raise HandoffError("Pi session mismatch")
                seen_session = True
            if kind == "model_change":
                model = (entry.get("provider"), entry.get("modelId"))
                latest = None  # changing model after an error invalidates that error
            if kind != "message":
                continue
            message = entry.get("message")
        if not isinstance(message, dict):
            raise HandoffError("invalid transcript message")
        role = message.get("role")
        if agent["agent"] == "claude" and role != kind:
            raise HandoffError("native message role mismatch")
        if role not in ("user", "assistant"):
            continue
        latest = None
        if role == "user":
            last_user = digest(entry.get("uuid") or entry.get("id") or message)
            continue
        timestamp = _to_epoch(entry.get("timestamp"))
        if timestamp is None or not now - MAX_AGE <= timestamp <= now + 5:
            continue
        if agent["agent"] == "claude":
            content = message.get("content")
            text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)) if isinstance(content, list) else ""
            qualifies = (kind == "assistant" and entry.get("isApiErrorMessage") is True
                         and entry.get("error") == "rate_limit" and tail_notice(text))
        else:
            qualifies = (model == (DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL)
                         and message.get("stopReason") == "error"
                         and tail_notice(message.get("errorMessage"), pi=True))
        if qualifies:
            latest = {"timestamp": timestamp, "last_user": last_user}
    if not seen_session:
        raise HandoffError("transcript has no exact session identity")
    return {"error": latest, "last_user": last_user, "hash": hasher.hexdigest()}


class ReceiptStore:
    def __init__(self, state_dir):
        self.receipts_dir = Path(state_dir) / "receipts"

    def path(self, key):
        return self.receipts_dir / (key + ".json")

    def load(self, key):
        try:
            value = json.loads(read_file(self.path(key), 65536, private=True))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, HandoffError):
            raise HandoffError("corrupt receipt") from None
        if not isinstance(value, dict) or value.get("event_key") != key or not isinstance(value.get("phase"), str):
            raise HandoffError("corrupt receipt")
        return value

    def save(self, key, receipt):
        atomic_json(self.path(key), receipt)


class QuotaHandoffEngine:
    def __init__(self, config, herdr, quota, os_port=None, state_dir=None):
        self.config, self.herdr, self.quota = config, herdr, quota
        self.os = os_port or OsPort()
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.receipts = ReceiptStore(self.state_dir) if self.state_dir else None

    def operational(self):
        return self.config.authorized() and self.os.platform == "linux" and self.state_dir is not None

    def global_invocation_lock(self):
        private_dir(self.state_dir)
        fd = os.open(self.state_dir / "invocation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise HandoffError("unsafe invocation lock")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            return None

    def candidates(self, own_pane=None, target=None):
        out = []
        workspaces = self.herdr.workspace_list()
        if not isinstance(workspaces, list) or len(workspaces) > 256:
            raise HandoffError("invalid workspace list")
        allowed_ids = {w["workspace_id"] for w in workspaces if isinstance(w, dict)
                       and isinstance(w.get("workspace_id"), str)
                       and (w["workspace_id"] in self.config.allowed_workspaces or w.get("label") in self.config.allowed_workspaces)}
        agents = self.herdr.agent_list()
        if not isinstance(agents, list) or len(agents) > 256:
            raise HandoffError("invalid agent list")
        for agent in agents:
            try:
                identity(agent)
            except HandoffError:
                continue
            if (agent["agent"] not in ("claude", "pi") or agent["workspace_id"] not in allowed_ids
                    or agent["pane_id"] == own_pane or (target and agent["pane_id"] != target)
                    or not os.path.isabs(agent["cwd"])):
                continue
            # Named delegated jobs are protected, not just names containing 'worker'.
            if agent.get("name") and agent["pane_id"] not in self.config.test_source_allowlist:
                continue
            if not self.single_pane(agent):
                continue
            out.append(agent)
        return out

    def single_pane(self, agent):
        panes = self.herdr.pane_list(agent["workspace_id"])
        if not isinstance(panes, list) or len(panes) > 1024 or any(
                not isinstance(p, dict) or not all(p.get(k) for k in ("pane_id", "tab_id", "workspace_id"))
                or p["workspace_id"] != agent["workspace_id"] for p in panes):
            return False
        matching = [p for p in panes if p["tab_id"] == agent["tab_id"]]
        return len(matching) == 1 and matching[0]["pane_id"] == agent["pane_id"]

    def detect_exhausted(self, candidate):
        first = self.herdr.agent_snapshot(candidate["pane_id"])
        second = self.herdr.agent_snapshot(candidate["pane_id"])
        for snapshot in (first, second):
            agent = snapshot.get("agent")
            if identity(agent) != identity(candidate) or not tail_notice(snapshot.get("text"), pi=agent["agent"] == "pi"):
                return None
            if agent.get("agent_status") not in ("idle", "blocked", "done", "working"):
                return None
        if first != second:
            return None
        return second["agent"]

    def validate_group(self, agent):
        if self.os.platform != "linux":
            raise HandoffError("Linux required")
        info = self.herdr.process_info(agent["pane_id"])
        pgid, shell = info.get("foreground_process_group_id"), info.get("shell_pid")
        claimed = info.get("foreground_processes")
        if (type(pgid) is not int or pgid <= 1 or type(shell) is not int or shell <= 1
                or not isinstance(claimed, list) or not claimed):
            raise HandoffError("unknown foreground group")
        table = self.os.table()
        members = {pid: p for pid, p in table.items() if p["pgid"] == pgid}
        claimed_pids = [p.get("pid") for p in claimed if isinstance(p, dict)]
        if set(claimed_pids) != set(members) or len(claimed_pids) != len(members) or shell in members:
            raise HandoffError("ambiguous foreground membership")
        shell_process = table.get(shell)
        if not shell_process or shell_process["uid"] != self.os.uid() or shell_process["pgid"] == pgid:
            raise HandoffError("unproven shell identity")
        native = []
        for pid, p in members.items():
            if p["uid"] != self.os.uid() or p["state"] in ("Z", "X", "T", "t") or type(p["start"]) is not int:
                raise HandoffError("unsafe foreground member")
            # Actual executable, never a substring in a claimed command.
            if p.get("native") == agent["agent"]:
                native.append(pid)
        if not native:
            raise HandoffError("no native agent member")
        seen = set(members)
        for _ in range(len(table) + 1):
            children = {pid for pid, p in table.items() if p["ppid"] in seen} - seen
            if not children:
                break
            if any(table[pid]["pgid"] != pgid and table[pid]["state"] not in ("Z", "X") for pid in children):
                raise HandoffError("active descendants outside source group")
            seen.update(children)
        return {"pgid": pgid, "shell_pid": shell, "native": sorted(native),
                "members": {str(pid): {"pid": pid, "pgid": pgid, "uid": p["uid"], "start": p["start"], "exe": p["exe"]} for pid, p in members.items()}}

    def verify_stopped(self, group):
        deadline = time.monotonic() + 0.5
        while True:
            table = self.os.table()
            for pid in group["members"]:
                p = table.get(int(pid))
                if p is not None and p["pgid"] != group["pgid"]:
                    raise HandoffError("member escaped stopped group")
            seen = {int(pid) for pid in group["members"]}
            while True:
                children = {pid for pid, p in table.items() if p["ppid"] in seen} - seen
                if not children:
                    break
                if any(table[pid]["pgid"] != group["pgid"] and table[pid]["state"] not in ("Z", "X") for pid in children):
                    raise HandoffError("active descendant after stop")
                seen.update(children)
            survivors = {str(pid): p for pid, p in table.items() if p["pgid"] == group["pgid"]}
            if not survivors or set(survivors) - set(group["members"]):
                raise HandoffError("ambiguous stopped group")
            stopped = True
            for pid, p in survivors.items():
                token = {k: p[k] for k in ("pid", "pgid", "uid", "start", "exe")}
                if token != group["members"][pid]:
                    raise HandoffError("stopped identity drift")
                stopped &= p["state"] in ("T", "t")
            if stopped:
                return
            if time.monotonic() >= deadline:
                raise HandoffError("stop verification timed out")
            time.sleep(0.01)

    def _cache_path(self, profile):
        return self.state_dir / "quota-cache" / (digest(profile) + ".json")

    def claude_capacity(self, profile, now):
        path = self._cache_path(profile)
        try:
            cached = json.loads(read_file(path, 65536, private=True))
            age = now - cached["at"]
            if 0 <= age <= (POSITIVE_CACHE_TTL if cached["ok"] else NEGATIVE_CACHE_TTL):
                if not cached["ok"]:
                    return None
                # Revalidate raw windows, including reset expiry, on every use.
                return validate_claude_payload(cached["payload"], now)
        except (OSError, ValueError, KeyError, TypeError, HandoffError):
            pass
        try:
            payload = self.quota.claude_usage(profile, now)
            capacity = validate_claude_payload(payload, now)
        except HandoffError:
            payload, capacity = None, None
        atomic_json(path, {"at": now, "ok": capacity is not None, "payload": payload if capacity else None})
        return capacity

    def pi_eligible(self, now):
        if not validate_pi_auth(self.quota.pi_auth_check()):
            return None
        capacity = validate_codex_snapshot(self.quota.codex_windows(now), now)
        if capacity and min(100 - w["utilization"] for w in capacity.values()) >= self.config.minimum_remaining_percent:
            return capacity
        return None

    def exclusions(self):
        try:
            value = json.loads(read_file(self.state_dir / "exclusions.json", 65536, private=True))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, HandoffError):
            raise HandoffError("corrupt account exclusions") from None
        if not isinstance(value, dict) or any(not isinstance(v, dict) or (v.get("until") is not None and not _finite(v["until"])) for v in value.values()):
            raise HandoffError("corrupt account exclusions")
        return value

    def exclude_source(self, config_dir, route, now):
        key = digest(config_dir) if route == "claude" else "sol"
        exclusions = self.exclusions()
        # Establish a reset only from current, proven exhausted applicable windows.
        resets = []
        try:
            if route == "claude":
                profile = next((p for p in self.config.profiles() if p["config_dir"] == config_dir), None)
                if profile:
                    payload = self.quota.claude_usage(profile, now)
                    capacity = validate_claude_payload(payload, now, exhausted=True)
                    if capacity:
                        resets = [w["resets_at"] for w in capacity.values() if w["utilization"] == 100 and w["resets_at"]]
            else:
                if not validate_pi_auth(self.quota.pi_auth_check()):
                    raise HandoffError("unproven Pi subscription")
                capacity = validate_codex_snapshot(self.quota.codex_windows(now), now, exhausted=True)
                if capacity is None:
                    raise HandoffError("unproven Pi subscription exhaustion")
                resets = [w["resets_at"] for w in capacity.values() if w["utilization"] == 100]
        except HandoffError:
            if route == "pi":
                raise
        old = exclusions.get(key, {})
        until = max(resets) if resets else None
        if key in exclusions:
            until = max(until, old["until"]) if until is not None and old.get("until") is not None else None
        exclusions[key] = {"until": until}
        atomic_json(self.state_dir / "exclusions.json", exclusions)

    def choose_destination(self, now, exclude_profile=None, *, allow_claude=True):
        exclusions = self.exclusions()
        best = None
        if allow_claude:
            for profile in self.config.profiles():
                exclusion = exclusions.get(digest(profile["config_dir"]))
                if profile["name"] == exclude_profile or (exclusion is not None and (exclusion["until"] is None or exclusion["until"] > now)):
                    continue
                capacity = self.claude_capacity(profile, now)
                if capacity is None:
                    continue
                remaining = min(100 - w["utilization"] for w in capacity.values())
                if remaining >= self.config.minimum_remaining_percent and (best is None or remaining > best[0]):
                    best = (remaining, profile, capacity)
        if best:
            return "claude", best[1]["name"], best[2]
        sol_exclusion = exclusions.get("sol")
        if sol_exclusion and (sol_exclusion["until"] is None or sol_exclusion["until"] > now):
            return None
        try:
            capacity = self.pi_eligible(now)
        except HandoffError:
            capacity = None
        return ("pi", "sol", capacity) if capacity else None

    def capacity_at_use(self, kind, name):
        if kind == "claude":
            profile = next((p for p in self.config.profiles() if p["name"] == name), None)
            capacity = self.claude_capacity(profile, self.os.now()) if profile else None
        else:
            capacity = self.pi_eligible(self.os.now())
        if not capacity or min(100 - w["utilization"] for w in capacity.values()) < self.config.minimum_remaining_percent:
            raise HandoffError("destination capacity no longer known")
        return capacity

    def source_proof(self, agent):
        group = self.validate_group(agent)
        path, sid, config_dir = source_file(agent, group, self.os)
        metadata = transcript_metadata(path, agent, sid, self.os.now())
        if not metadata["error"]:
            raise HandoffError("no current exact-source quota error")
        return group, path, sid, config_dir, metadata

    def handle_event(self, event):
        if not self.operational():
            return {"status": "disabled", "mutated": False}
        if not isinstance(event, dict):
            return {"status": "invalid-event", "mutated": False}
        # In hooks HERDR_PANE_ID is the target, never a caller exclusion.
        target = event.get("pane_id") or os.environ.get("HERDR_PANE_ID")
        if not isinstance(target, str) or not target:
            return {"status": "missing-target", "mutated": False}
        guard = self.global_invocation_lock()
        if guard is None:
            return {"status": "busy", "mutated": False}
        try:
            for candidate in self.candidates(target=target):
                key = event_key(candidate)
                try:
                    if self.receipts.load(key) is not None:
                        return {"status": "duplicate", "mutated": False}
                except HandoffError:
                    return {"status": "quarantined", "reason": "corrupt-receipt", "mutated": False}
                detected = self.detect_exhausted(candidate)
                if detected is not None:
                    return self._handoff(detected, key)
            return {"status": "no-exhausted-source", "mutated": False}
        except (HandoffError, OSError, ValueError, TypeError, KeyError) as exc:
            return {"status": "refused", "reason": refusal_reason(exc), "mutated": False}
        finally:
            os.close(guard)

    def _handoff(self, agent, key):
        receipt, mutated = None, False
        try:
            group, _path, _sid, cd, metadata = self.source_proof(agent)
            server = self.os.launch_environment()
            if server is None or server["env"].get("overrides") or not launch_settings_safe(server["env"], agent["cwd"], cd):
                raise HandoffError("unproven server launch environment")
            if self.config.launch_cwd_allowlist and agent["cwd"] not in self.config.launch_cwd_allowlist:
                raise HandoffError("cwd not allowed")
            if agent["agent"] == "pi" and any(self.os.environment(pid).get("HOME") != server["env"]["HOME"] for pid in group["native"]):
                raise HandoffError("unproven Pi source environment")
            self.quota.probe_home = server["env"]["HOME"]
            self.exclude_source(cd, agent["agent"], self.os.now())
            known = agent["agent"] == "pi" or cd in {p["config_dir"] for p in self.config.profiles()}
            destination = self.choose_destination(self.os.now(), allow_claude=known)
            if destination is None:
                return {"status": "no-available-destination", "mutated": False}
            kind, profile_name, capacity = destination
            destination_cd = self.config.profile_config_dir(profile_name) if kind == "claude" else None
            if not launch_settings_safe(server["env"], agent["cwd"], destination_cd):
                raise HandoffError("unsafe destination settings")
            self.capacity_at_use(kind, profile_name)
            existing = self.herdr.pane_list(agent["workspace_id"])
            existing_panes = {p["pane_id"] for p in existing}
            existing_tabs = {p["tab_id"] for p in existing}
            if agent["agent"] == "pi":
                # Re-probe after destination selection: friendly SDK text alone is
                # ambiguous, and an earlier official snapshot may no longer apply.
                self.exclude_source(cd, "pi", self.os.now())
            # Recheck *after* all potentially slow quota probes, immediately before intent/STOP.
            fresh = self.herdr.agent_snapshot(agent["pane_id"])
            if identity(fresh["agent"]) != identity(agent) or not tail_notice(fresh.get("text"), pi=agent["agent"] == "pi") or not self.single_pane(agent):
                raise HandoffError("source identity drift")
            current = self.source_proof(agent)
            if current[0] != group or current[4] != metadata or self.os.launch_environment() != server:
                raise HandoffError("source or server identity drift")
            now = self.os.now()
            if not all(w.get("unused") or w["resets_at"] > now for w in capacity.values()):
                raise HandoffError("destination snapshot expired")
            receipt = {"event_key": key, "phase": "park-intent", "created_at": now,
                       "source": {k: agent[k] for k in ("pane_id", "tab_id", "terminal_id", "workspace_id")},
                       "source_token": digest(identity(agent)), "park": group,
                       "destination": {"kind": kind, "profile": profile_name}}
            self.receipts.save(key, receipt)  # identity tokens durable BEFORE single STOP
            mutated = True  # STOP may have succeeded even if its response/verification is lost
            self.os.killpg(group["pgid"], signal.SIGSTOP)
            self.verify_stopped(group)
            receipt["phase"] = "parked"
            self.receipts.save(key, receipt)
            self.capacity_at_use(kind, profile_name)
            receipt["phase"] = "create-intent"
            self.receipts.save(key, receipt)
            created = self.herdr.tab_create(agent["workspace_id"], agent["cwd"], f"Continue {profile_name} · {agent['tab_id']}",
                                             {"CLAUDE_CONFIG_DIR": destination_cd} if destination_cd else None)
            pane, tab = created.get("root_pane"), created.get("tab")
            if not isinstance(pane, dict) or not isinstance(tab, dict):
                raise HandoffError("malformed create identity")
            pane_id, tab_id = pane.get("pane_id"), tab.get("tab_id")
            if not isinstance(pane_id, str) or not pane_id or not isinstance(tab_id, str) or not tab_id:
                raise HandoffError("missing destination identity")
            receipt["destination"].update(pane_id=pane_id, tab_id=tab_id)
            receipt["phase"] = "created"
            self.receipts.save(key, receipt)  # persist IDs before any further action
            if pane_id in existing_panes or tab_id in existing_tabs:
                raise HandoffError("create returned a pre-existing destination")
            self.destination_pane(agent, pane_id, tab_id)
            if self.os.launch_environment() != server or not launch_settings_safe(server["env"], agent["cwd"], destination_cd):
                raise HandoffError("launch configuration drift")
            launch_info = self.herdr.process_info(pane_id)
            launch_shell = self.os.environment(launch_info.get("shell_pid"))
            if (launch_shell is None or launch_shell.get("overrides")
                    or launch_shell.get("HOME") != server["env"]["HOME"]
                    or (destination_cd and launch_shell.get("CLAUDE_CONFIG_DIR") != destination_cd)):
                raise HandoffError("destination shell auth override")
            self.capacity_at_use(kind, profile_name)
            receipt["phase"] = "start-intent"
            self.receipts.save(key, receipt)
            args = (["--model", CLAUDE_MODEL, "--effort", "high"] if kind == "claude" else
                    ["--provider", DEFAULT_PI_PROVIDER, "--model", DEFAULT_PI_MODEL, "--thinking", "high"])
            if kind == "claude" and self.config.test_claude_launch_args:
                if agent["pane_id"] not in self.config.test_source_allowlist or not all(isinstance(x, str) for x in self.config.test_claude_launch_args):
                    raise HandoffError("invalid test launch profile")
                args += self.config.test_claude_launch_args
            started = self.herdr.agent_start("handoff-" + key[:12], kind, pane_id, args)
            self.ready_destination(started, kind, agent, pane_id, tab_id)
            receipt["destination"]["session_token"] = session_token(started)
            receipt["phase"] = "started"
            self.receipts.save(key, receipt)
            live = self.herdr.agent_get(pane_id)
            self.ready_destination(live, kind, agent, pane_id, tab_id)
            if identity(live) != identity(started):
                raise HandoffError("destination session drift")
            receipt["phase"] = "awaiting-confirmation"
            self.receipts.save(key, receipt)
            self.maybe_prompt(agent, metadata, live, receipt, key, server)
            self.herdr.notification_show(pane_id, tab_id, agent["pane_id"])
            return {"status": receipt["phase"], "mutated": True, "receipt": receipt}
        except (HandoffError, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            reason = refusal_reason(exc)
            if receipt is not None:
                receipt["phase"] = "quarantined"
                receipt["reason"] = reason
                try:
                    self.receipts.save(key, receipt)
                except (HandoffError, OSError, ValueError, TypeError):
                    pass  # The preceding durable intent still blocks all replay.
            return {"status": "quarantined" if receipt is not None else "refused", "reason": reason, "mutated": mutated}

    def destination_pane(self, source, pane_id, tab_id):
        pane = self.herdr.pane_get(pane_id)
        if (pane.get("pane_id") != pane_id or pane.get("tab_id") != tab_id
                or pane.get("workspace_id") != source["workspace_id"] or pane.get("cwd") != source["cwd"]
                or pane_id == source["pane_id"] or tab_id == source["tab_id"]):
            raise HandoffError("destination pane mismatch")
        if not self.single_pane({**source, "pane_id": pane_id, "tab_id": tab_id}):
            raise HandoffError("destination is not a top-level single pane")

    def ready_destination(self, live, kind, source, pane_id, tab_id):
        identity(live)
        if (live.get("agent") != kind or live.get("pane_id") != pane_id or live.get("tab_id") != tab_id
                or live.get("workspace_id") != source["workspace_id"] or live.get("cwd") != source["cwd"]
                or live.get("interactive_ready") is not True or live.get("agent_status") not in ("idle", "ready")):
            raise HandoffError("replacement not exactly ready")

    def checkpoint(self, path):
        raw = read_file(Path(path).expanduser(), 16384, private=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("classification") != "unclassified"
                or value.get("authority") != self.config.authority
                or not isinstance(value.get("checkpoint"), str) or not value["checkpoint"].strip()):
            raise HandoffError("invalid owner checkpoint")
        return hashlib.sha256(raw).hexdigest(), value["checkpoint"]

    def mark_public(self, pane_id, checkpoint=None):
        if not self.operational():
            return {"status": "disabled", "mutated": False}
        if not checkpoint:
            return {"status": "checkpoint-required", "mutated": False}
        guard = self.global_invocation_lock()
        if guard is None:
            return {"status": "busy", "mutated": False}
        try:
            agents = self.candidates(target=pane_id)
            if len(agents) != 1:
                raise HandoffError("unproven source")
            agent = self.herdr.agent_get(pane_id)
            if identity(agent) != identity(agents[0]):
                raise HandoffError("source identity drift")
            group = self.validate_group(agent)
            path, sid, _ = source_file(agent, group, self.os)
            metadata = transcript_metadata(path, agent, sid, self.os.now())
            if not metadata["last_user"]:
                raise HandoffError("unknown task token")
            checkpoint_hash, _ = self.checkpoint(checkpoint)
            atomic_json(self.state_dir / "markers" / (event_key(agent) + ".json"),
                        {"source_token": digest(identity(agent)), "last_user": metadata["last_user"],
                         "checkpoint_path": str(Path(checkpoint).expanduser().absolute()), "hash": checkpoint_hash})
            return {"status": "marked", "mutated": True}
        except (HandoffError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "refused", "reason": refusal_reason(exc), "mutated": False}
        finally:
            os.close(guard)

    def maybe_prompt(self, source, metadata, live, receipt, key, server):
        # Native UserPromptSubmit can inject global hook context: NEVER auto-prompt it.
        if live["agent"] != "pi":
            return
        try:
            marker = json.loads(read_file(self.state_dir / "markers" / (key + ".json"), 32768, private=True))
            if not isinstance(marker, dict):
                return
            if marker.get("source_token") != digest(identity(source)) or marker.get("last_user") != metadata["last_user"] or not metadata["last_user"]:
                return
            checkpoint_hash, text = self.checkpoint(marker["checkpoint_path"])
            template = self.config.auto_prompt_template
            if (checkpoint_hash != marker["hash"] or not isinstance(template, dict)
                    or template.get("authority") != self.config.authority or not isinstance(template.get("text"), str)):
                return
            parts = list(string.Formatter().parse(template["text"]))
            if not any(p[1] == "checkpoint" for p in parts) or any(p[1] not in (None, "checkpoint") or p[2] or p[3] for p in parts):
                return
            prompt = template["text"].format(checkpoint=text)
            if len(prompt) > 32768 or not self.pi_eligible(self.os.now()) or self.os.launch_environment() != server:
                return
            self.destination_pane(source, live["pane_id"], live["tab_id"])
            current = self.herdr.agent_get(live["pane_id"])
            self.ready_destination(current, "pi", source, live["pane_id"], live["tab_id"])
            if identity(current) != identity(live) or not launch_settings_safe(server["env"], source["cwd"]):
                return
            path, sid, _ = source_file(source, receipt["park"], self.os)
            if transcript_metadata(path, source, sid, self.os.now()) != metadata:
                return
            # Checkpoint itself must also remain exactly the owner-approved file.
            if self.checkpoint(marker["checkpoint_path"])[0] != checkpoint_hash:
                return
        except (HandoffError, OSError, ValueError, KeyError, TypeError):
            return  # Optional permission failure always leaves a blank successor.
        receipt["phase"] = "prompt-intent"
        self.receipts.save(key, receipt)
        self.herdr.agent_prompt(live["pane_id"], prompt)
        receipt["phase"] = "prompted"
        self.receipts.save(key, receipt)

    def status(self):
        return {"enabled": self.operational(), "status": "enabled" if self.operational() else "disabled"}

    def scan(self, own_pane=None):
        if not self.operational():
            return []
        out = []
        for candidate in self.candidates(own_pane=own_pane):
            try:
                detected = self.detect_exhausted(candidate)
                exhausted = bool(detected and self.source_proof(detected))
            except (HandoffError, OSError, ValueError, KeyError, TypeError):
                exhausted = False
            out.append({"pane_id": candidate["pane_id"], "exhausted": exhausted})
        return out

    def reconcile(self, pane_id=None):
        out = []
        if self.receipts:
            for path in sorted(self.receipts.receipts_dir.glob("*.json")):
                try:
                    receipt = self.receipts.load(path.stem)
                    if pane_id is None or receipt["source"]["pane_id"] == pane_id:
                        out.append(receipt)
                except (HandoffError, KeyError):
                    out.append({"phase": "quarantined"})
        return {"receipts": out}


def build_parser():
    parser = argparse.ArgumentParser(prog="quota-handoff", description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--state-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("event")
    scan = sub.add_parser("scan")
    scan.add_argument("--own-pane")
    mark = sub.add_parser("mark-public")
    mark.add_argument("--pane", required=True)
    mark.add_argument("--checkpoint", required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--pane")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    path = Path(args.config).expanduser() if args.config else default_config_path()
    config = HandoffConfig.load(path) if path else HandoffConfig()
    state = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    engine = QuotaHandoffEngine(config, HerdrPort(), QuotaPort(state or Path(".")), state_dir=state)
    rc = 0
    try:
        if args.command == "status":
            result = engine.status()
        elif args.command == "event":
            # Disabled is a no-call fast path, even for malformed event data.
            if not engine.operational():
                result = {"status": "disabled", "mutated": False}
            else:
                result = engine.handle_event(json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}")))
        elif args.command == "scan":
            result = {"agents": engine.scan(args.own_pane)}
        elif args.command == "mark-public":
            result = engine.mark_public(args.pane, args.checkpoint)
        else:
            result = engine.reconcile(args.pane)
        if isinstance(result, dict) and result.get("status") in ("invalid-event", "refused", "quarantined", "missing-target"):
            rc = 1
    except (HandoffError, OSError, ValueError, TypeError, KeyError) as exc:
        result, rc = {"status": "error", "reason": refusal_reason(exc), "mutated": False}, 1
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

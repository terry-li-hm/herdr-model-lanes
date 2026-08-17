"""Herdr plugin: show Codex and Claude subscription capacity.

Codex is queried through its local app-server. Claude Max usage is obtained
from the bundled ``claude_max_usage.py`` helper subprocess, which owns OAuth
and Keychain handling. This plugin sees only normalized usage JSON and never
credentials.
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WEEKLY_WINDOW_MINS = 10_080
WEEKLY_TOLERANCE_MINS = 240
CODEX_REFRESH_INTERVAL_SECS = 300
CLAUDE_REFRESH_INTERVAL_SECS = 1_800
CACHE_TTL_SECS = 6 * 3_600
SUBPROCESS_TIMEOUT_SECS = 15
CLAUDE_HELPER_TIMEOUT_SECS = 12
PLUGIN_ID = "terry.herdr-model-quota"
TOKEN_NAME = "model_quota"
TOKEN_TTL_MS = 2 * 60 * 60 * 1000
CODEX_CACHE_FILENAME = "codex-quota.json"
CLAUDE_CACHE_FILENAME = "claude-quota.json"
NON_SUBSCRIPTION_PLAN_MARKERS = ("api", "payg", "usage", "trial")


class QuotaError(Exception):
    """Raised when quota data cannot be safely obtained or parsed."""


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: int
    remaining_percent: int
    resets_at: int | None


@dataclass(frozen=True)
class CodexUsage:
    weekly: QuotaWindow
    fetched_at: int
    plan: str


@dataclass(frozen=True)
class ClaudeUsage:
    weekly: QuotaWindow
    session: QuotaWindow | None
    sonnet: QuotaWindow | None
    fetched_at: int
    source_stale: bool = False


@dataclass(frozen=True)
class RefreshOutcome:
    codex: CodexUsage | None
    claude: ClaudeUsage | None
    codex_stale: bool
    claude_stale: bool
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Parsing and formatting
# ---------------------------------------------------------------------------


def _percent(value: object, field: str) -> tuple[int, int]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise QuotaError(f"{field} is not numeric")
    if not 0 <= value <= 100:
        raise QuotaError(f"invalid {field}: {value!r}")
    used = round(value)
    return used, round(100 - value)


def _pick_weekly_window(rate_limits: dict) -> dict:
    windows = []
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if isinstance(window, dict):
            duration = window.get("windowDurationMins")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                windows.append(window)
    for window in windows:
        if (
            abs(window["windowDurationMins"] - WEEKLY_WINDOW_MINS)
            <= WEEKLY_TOLERANCE_MINS
        ):
            return window
    raise QuotaError("no weekly (10,080-minute) Codex window in response")


def parse_codex_usage(payload: dict, fetched_at: int) -> CodexUsage:
    """Parse an ``account/rateLimits/read`` result."""
    if not isinstance(payload, dict):
        raise QuotaError("Codex rate-limit payload is not an object")
    rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise QuotaError("Codex payload missing 'rateLimits' object")

    window = _pick_weekly_window(rate_limits)
    used, remaining = _percent(window.get("usedPercent"), "Codex used percentage")
    resets_at = window.get("resetsAt")
    if not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool):
        raise QuotaError("Codex weekly window missing numeric resetsAt")

    plan = rate_limits.get("planType")
    plan = plan if isinstance(plan, str) else ""
    if plan and any(marker in plan.lower() for marker in NON_SUBSCRIPTION_PLAN_MARKERS):
        raise QuotaError(f"Codex plan {plan!r} is not a subscription plan")

    return CodexUsage(
        weekly=QuotaWindow(used, remaining, int(resets_at)),
        fetched_at=fetched_at,
        plan=plan,
    )


def _parse_timestamp(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise QuotaError(f"{field} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QuotaError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise QuotaError(f"{field} has no timezone")
    return int(parsed.timestamp())


def _parse_claude_window(value: object, field: str) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QuotaError(f"Claude {field} window is not an object")
    used, remaining = _percent(value.get("utilization"), f"Claude {field} utilization")
    resets_at = _parse_timestamp(value.get("resets_at"), f"Claude {field} resets_at")
    return QuotaWindow(used, remaining, resets_at)


def parse_claude_usage(payload: dict, fetched_at: int) -> ClaudeUsage:
    """Parse the normalized, credential-free output of the bundled helper."""
    if not isinstance(payload, dict):
        raise QuotaError("Claude helper payload is not an object")
    weekly = _parse_claude_window(payload.get("seven_day"), "weekly")
    if weekly is None:
        raise QuotaError("Claude helper payload has no weekly window")

    source_stale = payload.get("stale") is True
    source_age = payload.get("stale_age_seconds")
    source_fetched_at = fetched_at
    if source_stale:
        if isinstance(source_age, (int, float)) and not isinstance(source_age, bool):
            source_fetched_at = fetched_at - max(0, int(source_age))
    elif weekly.resets_at is None:
        raise QuotaError("live Claude weekly window has no reset timestamp")

    return ClaudeUsage(
        weekly=weekly,
        session=_parse_claude_window(payload.get("five_hour"), "five-hour"),
        sonnet=_parse_claude_window(payload.get("seven_day_sonnet"), "Sonnet weekly"),
        fetched_at=source_fetched_at,
        source_stale=source_stale,
    )


def _countdown(resets_at: int | None, now: int) -> str:
    if resets_at is None:
        return ""
    seconds_left = max(resets_at - now, 0)
    days, remainder = divmod(seconds_left, 86_400)
    hours = remainder // 3_600
    return f"{days}d{hours}h" if days else f"{hours}h"


def _warning(remaining: int) -> str:
    if remaining < 10:
        return "!!"
    if remaining < 20:
        return "!"
    return ""


def _format_window(label: str, window: QuotaWindow, now: int, stale: bool) -> str:
    remaining = max(0, min(100, window.remaining_percent))
    marker = "~" if stale else ""
    reset = _countdown(window.resets_at, now)
    suffix = f" · {reset}" if reset else ""
    return f"{label} {remaining}%{_warning(remaining)}{marker}{suffix}"


def _format_constraint(label: str, window: QuotaWindow, now: int) -> str:
    return _format_window(label, window, now, stale=False)


def format_quota(
    codex: CodexUsage | None,
    claude: ClaudeUsage | None,
    now: int,
    codex_stale: bool = False,
    claude_stale: bool = False,
) -> str:
    codex_text = "Cx n/a"
    if codex is not None:
        codex_text = _format_window("Cx", codex.weekly, now, codex_stale)

    claude_text = "Cl n/a"
    if claude is not None:
        claude_text = _format_window(
            "Cl", claude.weekly, now, claude_stale or claude.source_stale
        )
        constraints = []
        for label, window in (("5h", claude.session), ("S", claude.sonnet)):
            if window is not None and (
                window.remaining_percent < claude.weekly.remaining_percent
                or window.remaining_percent < 20
            ):
                constraints.append(_format_constraint(label, window, now))
        if constraints:
            claude_text += " / " + " / ".join(constraints)

    return f"{codex_text} | {claude_text}"


# ---------------------------------------------------------------------------
# Normalized caches and process locking
# ---------------------------------------------------------------------------


def _window_to_dict(window: QuotaWindow | None) -> dict | None:
    if window is None:
        return None
    return {
        "used_percent": window.used_percent,
        "remaining_percent": window.remaining_percent,
        "resets_at": window.resets_at,
    }


def _window_from_dict(value: object, field: str) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{field} is not an object")
    used = int(value["used_percent"])
    remaining = int(value["remaining_percent"])
    resets_raw = value.get("resets_at")
    resets_at = None if resets_raw is None else int(resets_raw)
    if not 0 <= used <= 100 or not 0 <= remaining <= 100:
        raise ValueError(f"{field} percentage out of range")
    return QuotaWindow(used, remaining, resets_at)


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_codex_cache(path: Path, usage: CodexUsage) -> None:
    _save_json(
        path,
        {
            "weekly": _window_to_dict(usage.weekly),
            "fetched_at": usage.fetched_at,
            "plan": usage.plan,
        },
    )


def load_codex_cache(path: Path) -> CodexUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        weekly = _window_from_dict(raw["weekly"], "Codex weekly")
        if weekly is None or weekly.resets_at is None:
            return None
        return CodexUsage(weekly, int(raw["fetched_at"]), str(raw.get("plan", "")))
    except (OSError, KeyError, TypeError, ValueError):
        return None


def save_claude_cache(path: Path, usage: ClaudeUsage) -> None:
    _save_json(
        path,
        {
            "weekly": _window_to_dict(usage.weekly),
            "session": _window_to_dict(usage.session),
            "sonnet": _window_to_dict(usage.sonnet),
            "fetched_at": usage.fetched_at,
            "source_stale": usage.source_stale,
        },
    )


def load_claude_cache(path: Path) -> ClaudeUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        weekly = _window_from_dict(raw["weekly"], "Claude weekly")
        if weekly is None:
            return None
        return ClaudeUsage(
            weekly=weekly,
            session=_window_from_dict(raw.get("session"), "Claude session"),
            sonnet=_window_from_dict(raw.get("sonnet"), "Claude Sonnet"),
            fetched_at=int(raw["fetched_at"]),
            source_stale=raw.get("source_stale") is True,
        )
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _attempt_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".attempt.json")


def _load_attempt(path: Path) -> int | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return int(raw["attempted_at"])
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _save_attempt(path: Path, attempted_at: int) -> None:
    _save_json(path, {"attempted_at": attempted_at})


@contextmanager
def _cache_lock(cache_path: Path) -> Iterator[None]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _discard_expired(path: Path, usage: CodexUsage | ClaudeUsage | None, now: int):
    if usage is not None and now - usage.fetched_at <= CACHE_TTL_SECS:
        return usage
    if usage is not None:
        try:
            path.unlink()
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# Bounded data-source clients
# ---------------------------------------------------------------------------


def _codex_request(
    request_id: int | None, method: str, params: dict | None = None
) -> str:
    request: dict = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        request["id"] = request_id
    if params is not None:
        request["params"] = params
    return json.dumps(request)


def query_codex(now: int | None = None, codex_bin: str = "codex") -> CodexUsage:
    """Query Codex app-server without reading credential or session stores."""
    if now is None:
        now = int(time.time())
    try:
        process = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise QuotaError(f"cannot start Codex app-server: {exc}") from exc

    deadline = time.monotonic() + SUBPROCESS_TIMEOUT_SECS
    lines: queue.Queue[str] = queue.Queue()

    def pump_stdout() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.put(line)
        except OSError:
            pass
        finally:
            lines.put("")

    threading.Thread(target=pump_stdout, daemon=True).start()

    def send(line: str) -> None:
        if deadline - time.monotonic() <= 0:
            raise QuotaError("Codex app-server deadline exceeded")
        assert process.stdin is not None
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise QuotaError(f"Codex app-server stdin failure: {exc}") from exc

    def read_result(target_id: int) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuotaError("Codex app-server deadline exceeded")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise QuotaError("Codex app-server deadline exceeded") from None
            if line == "":
                raise QuotaError("Codex app-server closed its output stream")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == target_id:
                if "error" in message:
                    raise QuotaError("Codex app-server returned an error")
                return message.get("result") or {}

    rate_limit_payload: dict | None = None
    try:
        send(
            _codex_request(
                1,
                "initialize",
                {"clientInfo": {"name": "herdr-model-quota", "version": "2"}},
            )
        )
        read_result(1)
        send(_codex_request(None, "initialized", {}))
        send(_codex_request(2, "account/read", {}))
        account = read_result(2)
        auth = account.get("authMethod")
        if isinstance(account.get("auth"), dict):
            auth = auth or account["auth"].get("method")
        if auth and "api" in str(auth).lower():
            raise QuotaError("Codex uses API-key authentication, not a subscription")
        send(_codex_request(3, "account/rateLimits/read", {}))
        rate_limit_payload = read_result(3)
    except QuotaError:
        raise
    except (OSError, ValueError) as exc:
        raise QuotaError(f"Codex app-server protocol failure: {exc}") from exc
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    if rate_limit_payload is None:
        raise QuotaError("no Codex rate-limit response received")
    return parse_codex_usage(rate_limit_payload, fetched_at=now)


def default_claude_command() -> list[str]:
    """Default helper invocation: current interpreter, bundled module path."""
    return [sys.executable, str(Path(__file__).with_name("claude_max_usage.py"))]


def query_claude(
    now: int | None = None, claude_command: list[str] | None = None
) -> ClaudeUsage:
    """Obtain normalized Claude Max usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = claude_command if claude_command is not None else default_claude_command()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CLAUDE_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain Claude usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"Claude helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("Claude helper returned invalid JSON") from exc
    usage = parse_claude_usage(payload, fetched_at=now)
    if now - usage.fetched_at > CACHE_TTL_SECS:
        raise QuotaError(
            "Claude helper fallback is older than the six-hour display limit"
        )
    return usage


# ---------------------------------------------------------------------------
# Herdr workspace publication
# ---------------------------------------------------------------------------


def _herdr(herdr_bin: str, args: list[str]) -> dict:
    completed = subprocess.run(
        [herdr_bin, *args],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECS,
        check=False,
    )
    if completed.returncode != 0:
        command = args[1] if len(args) > 1 else args[0]
        raise QuotaError(f"herdr {command} failed: rc={completed.returncode}")
    try:
        return json.loads(completed.stdout or "{}")
    except ValueError:
        return {}


def _list_workspaces(herdr_bin: str) -> list[dict]:
    result = _herdr(herdr_bin, ["workspace", "list"])
    return (result.get("result") or {}).get("workspaces") or []


def publish_to_focused_workspace(token_value: str, herdr_bin: str = "herdr") -> None:
    """Set ``model_quota`` only on the focused workspace."""
    workspaces = _list_workspaces(herdr_bin)
    for workspace in workspaces:
        args = [
            "workspace",
            "report-metadata",
            workspace["workspace_id"],
            "--source",
            PLUGIN_ID,
            "--ttl-ms",
            str(TOKEN_TTL_MS),
        ]
        if workspace.get("focused"):
            args.extend(["--token", f"{TOKEN_NAME}={token_value}"])
        else:
            args.extend(["--clear-token", TOKEN_NAME])
        _herdr(herdr_bin, args)


def clear_all(herdr_bin: str = "herdr") -> None:
    """Clear the model quota token from every workspace."""
    for workspace in _list_workspaces(herdr_bin):
        _herdr(
            herdr_bin,
            [
                "workspace",
                "report-metadata",
                workspace["workspace_id"],
                "--source",
                PLUGIN_ID,
                "--clear-token",
                TOKEN_NAME,
            ],
        )


# ---------------------------------------------------------------------------
# Independent refresh orchestration
# ---------------------------------------------------------------------------


def _refresh_codex(
    force: bool,
    cache_path: Path,
    now: int,
    codex_bin: str,
) -> tuple[CodexUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_codex_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < CODEX_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_codex(now=now, codex_bin=codex_bin)
            save_codex_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def _refresh_claude(
    force: bool,
    cache_path: Path,
    now: int,
    claude_command: list[str] | None,
) -> tuple[ClaudeUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_claude_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < CLAUDE_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            source_stale = cached.source_stale if cached is not None else False
            return cached, source_stale or failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_claude(now=now, claude_command=claude_command)
            save_claude_cache(cache_path, usage)
            return usage, usage.source_stale, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def refresh(
    force: bool = False,
    state_dir: Path | None = None,
    now: int | None = None,
    herdr_bin: str = "herdr",
    codex_bin: str = "codex",
    claude_command: list[str] | None = None,
) -> RefreshOutcome:
    if now is None:
        now = int(time.time())
    if state_dir is None:
        state_value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        if not state_value:
            line = format_quota(None, None, now)
            print(line, flush=True)
            return RefreshOutcome(None, None, False, False, ("no state dir",))
        state_dir = Path(state_value)

    codex, codex_stale, codex_error = _refresh_codex(
        force, state_dir / CODEX_CACHE_FILENAME, now, codex_bin
    )
    claude, claude_stale, claude_error = _refresh_claude(
        force, state_dir / CLAUDE_CACHE_FILENAME, now, claude_command
    )
    line = format_quota(codex, claude, now, codex_stale, claude_stale)
    publish_to_focused_workspace(line, herdr_bin=herdr_bin)
    print(line, flush=True)
    errors = tuple(error for error in (codex_error, claude_error) if error)
    return RefreshOutcome(codex, claude, codex_stale, claude_stale, errors)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "clear":
        clear_all()
        return 0
    refresh(force="--force" in argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

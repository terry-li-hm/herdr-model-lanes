"""Herdr plugin: show subscription capacity and route model-class lanes.

Codex is queried through its local app-server. Claude Max, Grok, GLM,
Antigravity, and Kimi Code usage each come from a bundled helper subprocess
that is the sole credential boundary for that provider. The argparse CLI
exposes ``refresh``, ``clear``, ``route``, and ``ag``. This plugin sees
only normalized usage JSON and never credentials.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import antigravity_usage
import cursor_usage
import glm_usage
import kimi_usage

WEEKLY_WINDOW_MINS = 10_080
WEEKLY_TOLERANCE_MINS = 240
CODEX_REFRESH_INTERVAL_SECS = 300
CLAUDE_REFRESH_INTERVAL_SECS = 1_800
CACHE_TTL_SECS = 6 * 3_600
SUBPROCESS_TIMEOUT_SECS = 15
CLAUDE_HELPER_TIMEOUT_SECS = 12
GROK_REFRESH_INTERVAL_SECS = 1_800
GROK_HELPER_TIMEOUT_SECS = 12
GLM_WINDOW_SECONDS = 5 * 3_600
GLM_REFRESH_INTERVAL_SECS = 300
GLM_HELPER_TIMEOUT_SECS = 12
ANTIGRAVITY_REFRESH_INTERVAL_SECS = 1_800
ANTIGRAVITY_HELPER_TIMEOUT_SECS = 12
KIMI_REFRESH_INTERVAL_SECS = 300
KIMI_HELPER_TIMEOUT_SECS = 12
CURSOR_REFRESH_INTERVAL_SECS = 1_800
CURSOR_HELPER_TIMEOUT_SECS = 12
LAUNCH_TIMEOUT_SECS = 30
PLUGIN_ID = "terry.herdr-model-lanes"
TOKEN_NAME = "model_quota"
TOKEN_TTL_MS = 2 * 60 * 60 * 1000
CODEX_CACHE_FILENAME = "codex-quota.json"
CLAUDE_CACHE_FILENAME = "claude-quota.json"
GROK_CACHE_FILENAME = "grok-quota.json"
GLM_CACHE_FILENAME = "glm-quota.json"
ANTIGRAVITY_CACHE_FILENAME = "antigravity-quota.json"
KIMI_CACHE_FILENAME = "kimi-quota.json"
CURSOR_CACHE_FILENAME = "cursor-quota.json"
CLASSES_FILENAME = "classes.toml"
ROUTE_WINDOW_SECONDS = 604_800
SURPLUS_HEALTH = 2.0
SURPLUS_RESET_WINDOW_SECS = 48 * 3_600
LANE_EXECUTABLES = {
    "claude": "claude",
    "pi": "pi",
    "grok": "grok",
    "cursor": "cursor-agent",
    "codex": "codex",
    "gemini": "gemini",
    "agy": "agy",
    "kimi": "kimi",
}
UNSET = object()
NON_SUBSCRIPTION_PLAN_MARKERS = ("api", "payg", "usage", "trial")


class QuotaError(Exception):
    """Raised when quota data cannot be safely obtained or parsed."""


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: int
    remaining_percent: int
    resets_at: int | None
    window_seconds: int = ROUTE_WINDOW_SECONDS


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
class GrokUsage:
    weekly: QuotaWindow
    fetched_at: int


@dataclass(frozen=True)
class GlmUsage:
    five_hour: QuotaWindow
    fetched_at: int


@dataclass(frozen=True)
class AntigravityUsage:
    gemini: QuotaWindow
    fetched_at: int


@dataclass(frozen=True)
class KimiUsage:
    coding: QuotaWindow
    fetched_at: int


@dataclass(frozen=True)
class CursorUsage:
    monthly: QuotaWindow
    fetched_at: int


@dataclass(frozen=True)
class RefreshOutcome:
    codex: CodexUsage | None
    claude: ClaudeUsage | None
    codex_stale: bool
    claude_stale: bool
    errors: tuple[str, ...] = ()
    grok: GrokUsage | None = None
    grok_stale: bool = False
    glm: GlmUsage | None = None
    glm_stale: bool = False
    antigravity: AntigravityUsage | None = None
    antigravity_stale: bool = False
    kimi: KimiUsage | None = None
    kimi_stale: bool = False
    cursor: CursorUsage | None = None
    cursor_stale: bool = False


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


def parse_grok_usage(payload: dict, fetched_at: int) -> GrokUsage:
    """Parse the normalized, credential-free output of the Grok helper."""
    if not isinstance(payload, dict):
        raise QuotaError("Grok helper payload is not an object")
    weekly = _parse_grok_window(payload.get("weekly"), "Grok weekly")
    if weekly is None:
        raise QuotaError("Grok helper payload has no weekly window")
    if weekly.resets_at is None:
        raise QuotaError("Grok helper weekly window has no reset timestamp")
    return GrokUsage(weekly=weekly, fetched_at=fetched_at)


def _parse_grok_window(value: object, field: str) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QuotaError(f"{field} window is not an object")
    used, remaining = _percent(value.get("used_percent"), f"{field} used percentage")
    resets_at = _parse_timestamp(value.get("resets_at"), f"{field} resets_at")
    return QuotaWindow(used, remaining, resets_at)


def _parse_glm_window(value: object, field: str) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QuotaError(f"{field} window is not an object")
    used, remaining = _percent(value.get("used_percent"), f"{field} used percentage")
    resets_at = value.get("resets_at")
    if resets_at is not None and (
        not isinstance(resets_at, (int, float)) or isinstance(resets_at, bool)
    ):
        raise QuotaError(f"{field} resets_at is not a unix timestamp")
    return QuotaWindow(
        used,
        remaining,
        None if resets_at is None else int(resets_at),
        window_seconds=GLM_WINDOW_SECONDS,
    )


def parse_glm_usage(payload: dict, fetched_at: int) -> GlmUsage:
    """Parse the normalized, credential-free output of the GLM helper."""
    if not isinstance(payload, dict):
        raise QuotaError("GLM helper payload is not an object")
    five_hour = _parse_glm_window(payload.get("five_hour"), "GLM five_hour")
    if five_hour is None:
        raise QuotaError("GLM helper payload has no five_hour window")
    if five_hour.resets_at is None:
        raise QuotaError("GLM helper five_hour window has no reset timestamp")
    return GlmUsage(five_hour=five_hour, fetched_at=fetched_at)


def _parse_antigravity_window(value: object, field: str) -> QuotaWindow | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QuotaError(f"{field} window is not an object")
    used, remaining = _percent(value.get("used_percent"), f"{field} used percentage")
    resets_at = _parse_timestamp(value.get("resets_at"), f"{field} resets_at")
    window_seconds = value.get("window_seconds", ROUTE_WINDOW_SECONDS)
    if not isinstance(window_seconds, (int, float)) or isinstance(window_seconds, bool):
        raise QuotaError(f"{field} window_seconds is not numeric")
    if window_seconds <= 0:
        raise QuotaError(f"{field} window_seconds must be positive")
    return QuotaWindow(used, remaining, resets_at, window_seconds=int(window_seconds))


def parse_antigravity_usage(payload: dict, fetched_at: int) -> AntigravityUsage:
    """Parse the normalized, credential-free output of the Antigravity helper."""
    if not isinstance(payload, dict):
        raise QuotaError("Antigravity helper payload is not an object")
    gemini = _parse_antigravity_window(payload.get("gemini"), "Antigravity gemini")
    if gemini is None:
        raise QuotaError("Antigravity helper payload has no gemini window")
    if gemini.resets_at is None:
        raise QuotaError("Antigravity helper gemini window has no reset timestamp")
    return AntigravityUsage(gemini=gemini, fetched_at=fetched_at)


def parse_kimi_usage(payload: dict, fetched_at: int) -> KimiUsage:
    """Parse the normalized, credential-free output of the Kimi helper."""
    if not isinstance(payload, dict):
        raise QuotaError("Kimi helper payload is not an object")
    coding = _parse_antigravity_window(payload.get("coding"), "Kimi coding")
    if coding is None:
        raise QuotaError("Kimi helper payload has no coding window")
    if coding.resets_at is None:
        raise QuotaError("Kimi helper coding window has no reset timestamp")
    return KimiUsage(coding=coding, fetched_at=fetched_at)


def parse_cursor_usage(payload: dict, fetched_at: int) -> CursorUsage:
    """Parse the normalized, credential-free output of the Cursor helper."""
    if not isinstance(payload, dict):
        raise QuotaError("Cursor helper payload is not an object")
    monthly = _parse_antigravity_window(payload.get("monthly"), "Cursor monthly")
    if monthly is None:
        raise QuotaError("Cursor helper payload has no monthly window")
    if monthly.resets_at is None:
        raise QuotaError("Cursor helper monthly window has no reset timestamp")
    return CursorUsage(monthly=monthly, fetched_at=fetched_at)


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
    hours, remainder = divmod(remainder, 3_600)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h"
    return f"{remainder // 60}m"


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
    grok: GrokUsage | None | object = UNSET,
    grok_stale: bool = False,
    glm: GlmUsage | None | object = UNSET,
    glm_stale: bool = False,
    antigravity: AntigravityUsage | None | object = UNSET,
    antigravity_stale: bool = False,
    kimi: KimiUsage | None | object = UNSET,
    kimi_stale: bool = False,
    cursor: CursorUsage | None | object = UNSET,
    cursor_stale: bool = False,
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

    line = f"{codex_text} | {claude_text}"
    if grok is not UNSET:
        grok_usage = grok if isinstance(grok, GrokUsage) else None
        grok_text = "Gk n/a"
        if grok_usage is not None:
            grok_text = _format_window("Gk", grok_usage.weekly, now, grok_stale)
        line += f" | {grok_text}"
    if glm is not UNSET:
        glm_usage_obj = glm if isinstance(glm, GlmUsage) else None
        glm_text = "Gl n/a"
        if glm_usage_obj is not None:
            glm_text = _format_window("Gl", glm_usage_obj.five_hour, now, glm_stale)
        line += f" | {glm_text}"
    if antigravity is not UNSET:
        antigravity_usage = (
            antigravity if isinstance(antigravity, AntigravityUsage) else None
        )
        antigravity_text = "Ag n/a"
        if antigravity_usage is not None:
            antigravity_text = _format_window(
                "Ag", antigravity_usage.gemini, now, antigravity_stale
            )
        line += f" | {antigravity_text}"
    if kimi is not UNSET:
        kimi_usage_obj = kimi if isinstance(kimi, KimiUsage) else None
        kimi_text = "Km n/a"
        if kimi_usage_obj is not None:
            kimi_text = _format_window("Km", kimi_usage_obj.coding, now, kimi_stale)
        line += f" | {kimi_text}"
    if cursor is not UNSET:
        cursor_usage_obj = cursor if isinstance(cursor, CursorUsage) else None
        cursor_text = "Cu n/a"
        if cursor_usage_obj is not None:
            cursor_text = _format_window(
                "Cu", cursor_usage_obj.monthly, now, cursor_stale
            )
        line += f" | {cursor_text}"
    return line


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


def save_grok_cache(path: Path, usage: GrokUsage) -> None:
    _save_json(
        path,
        {"weekly": _window_to_dict(usage.weekly), "fetched_at": usage.fetched_at},
    )


def load_grok_cache(path: Path) -> GrokUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        weekly = _window_from_dict(raw["weekly"], "Grok weekly")
        if weekly is None or weekly.resets_at is None:
            return None
        return GrokUsage(weekly, int(raw["fetched_at"]))
    except (OSError, KeyError, TypeError, ValueError):
        return None


def save_glm_cache(path: Path, usage: GlmUsage) -> None:
    _save_json(
        path,
        {"five_hour": _window_to_dict(usage.five_hour), "fetched_at": usage.fetched_at},
    )


def load_glm_cache(path: Path) -> GlmUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        five_hour = _parse_glm_window(raw["five_hour"], "GLM five_hour")
        if five_hour is None or five_hour.resets_at is None:
            return None
        return GlmUsage(five_hour, int(raw["fetched_at"]))
    except (OSError, KeyError, TypeError, ValueError, QuotaError):
        return None


def save_antigravity_cache(path: Path, usage: AntigravityUsage) -> None:
    payload = _window_to_dict(usage.gemini) or {}
    payload["window_seconds"] = usage.gemini.window_seconds
    _save_json(
        path,
        {"gemini": payload, "fetched_at": usage.fetched_at},
    )


def load_antigravity_cache(path: Path) -> AntigravityUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        gemini_raw = raw["gemini"]
        if not isinstance(gemini_raw, dict):
            return None
        used = int(gemini_raw["used_percent"])
        remaining = int(gemini_raw["remaining_percent"])
        resets_raw = gemini_raw.get("resets_at")
        if resets_raw is None:
            return None
        window_seconds = int(gemini_raw.get("window_seconds", ROUTE_WINDOW_SECONDS))
        if not 0 <= used <= 100 or not 0 <= remaining <= 100 or window_seconds <= 0:
            return None
        return AntigravityUsage(
            QuotaWindow(used, remaining, int(resets_raw), window_seconds),
            int(raw["fetched_at"]),
        )
    except (OSError, KeyError, TypeError, ValueError, QuotaError):
        return None


def save_kimi_cache(path: Path, usage: KimiUsage) -> None:
    payload = _window_to_dict(usage.coding) or {}
    payload["window_seconds"] = usage.coding.window_seconds
    _save_json(path, {"coding": payload, "fetched_at": usage.fetched_at})


def load_kimi_cache(path: Path) -> KimiUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        coding_raw = raw["coding"]
        if not isinstance(coding_raw, dict):
            return None
        used = int(coding_raw["used_percent"])
        remaining = int(coding_raw["remaining_percent"])
        resets_raw = coding_raw.get("resets_at")
        if resets_raw is None:
            return None
        window_seconds = int(coding_raw.get("window_seconds", ROUTE_WINDOW_SECONDS))
        if not 0 <= used <= 100 or not 0 <= remaining <= 100 or window_seconds <= 0:
            return None
        return KimiUsage(
            QuotaWindow(used, remaining, int(resets_raw), window_seconds),
            int(raw["fetched_at"]),
        )
    except (OSError, KeyError, TypeError, ValueError, QuotaError):
        return None


def save_cursor_cache(path: Path, usage: CursorUsage) -> None:
    payload = _window_to_dict(usage.monthly) or {}
    payload["window_seconds"] = usage.monthly.window_seconds
    _save_json(path, {"monthly": payload, "fetched_at": usage.fetched_at})


def load_cursor_cache(path: Path) -> CursorUsage | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        monthly_raw = raw["monthly"]
        if not isinstance(monthly_raw, dict):
            return None
        used = int(monthly_raw["used_percent"])
        remaining = int(monthly_raw["remaining_percent"])
        resets_raw = monthly_raw.get("resets_at")
        if resets_raw is None:
            return None
        window_seconds = int(monthly_raw.get("window_seconds", ROUTE_WINDOW_SECONDS))
        if not 0 <= used <= 100 or not 0 <= remaining <= 100 or window_seconds <= 0:
            return None
        return CursorUsage(
            QuotaWindow(used, remaining, int(resets_raw), window_seconds),
            int(raw["fetched_at"]),
        )
    except (OSError, KeyError, TypeError, ValueError, QuotaError):
        return None


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
                {"clientInfo": {"name": "herdr-model-lanes", "version": "2"}},
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


def default_grok_command() -> list[str]:
    """Default Grok helper invocation: current interpreter, bundled module."""
    return [sys.executable, str(Path(__file__).with_name("grok_usage.py"))]


def query_grok(
    now: int | None = None, grok_command: list[str] | None = None
) -> GrokUsage:
    """Obtain normalized Grok usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = grok_command if grok_command is not None else default_grok_command()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GROK_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain Grok usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"Grok helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("Grok helper returned invalid JSON") from exc
    return parse_grok_usage(payload, fetched_at=now)


def default_glm_command() -> list[str]:
    """Default GLM helper invocation: current interpreter, bundled module."""
    return [sys.executable, str(Path(__file__).with_name("glm_usage.py"))]


def default_antigravity_command() -> list[str]:
    """Default Antigravity helper invocation: current interpreter, bundled module."""
    return [sys.executable, str(Path(__file__).with_name("antigravity_usage.py"))]


def query_glm(now: int | None = None, glm_command: list[str] | None = None) -> GlmUsage:
    """Obtain normalized GLM usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = glm_command if glm_command is not None else default_glm_command()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GLM_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain GLM usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"GLM helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("GLM helper returned invalid JSON") from exc
    return parse_glm_usage(payload, fetched_at=now)


def query_antigravity(
    now: int | None = None, antigravity_command: list[str] | None = None
) -> AntigravityUsage:
    """Obtain normalized Antigravity usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = (
        antigravity_command
        if antigravity_command is not None
        else default_antigravity_command()
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=ANTIGRAVITY_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain Antigravity usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"Antigravity helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("Antigravity helper returned invalid JSON") from exc
    return parse_antigravity_usage(payload, fetched_at=now)


def default_kimi_command() -> list[str]:
    """Default Kimi helper invocation: current interpreter, bundled module."""
    return [sys.executable, str(Path(__file__).with_name("kimi_usage.py"))]


def query_kimi(now: int | None = None, kimi_command: list[str] | None = None) -> KimiUsage:
    """Obtain normalized Kimi Code usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = kimi_command if kimi_command is not None else default_kimi_command()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=KIMI_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain Kimi usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"Kimi helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("Kimi helper returned invalid JSON") from exc
    return parse_kimi_usage(payload, fetched_at=now)


def default_cursor_command() -> list[str]:
    """Default Cursor helper invocation: current interpreter, bundled module."""
    return [sys.executable, str(Path(__file__).with_name("cursor_usage.py"))]


def query_cursor(
    now: int | None = None, cursor_command: list[str] | None = None
) -> CursorUsage:
    """Obtain normalized Cursor usage from the bundled helper subprocess."""
    if now is None:
        now = int(time.time())
    command = cursor_command if cursor_command is not None else default_cursor_command()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CURSOR_HELPER_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(
            f"cannot obtain Cursor usage from helper: {type(exc).__name__}"
        ) from exc
    if completed.returncode != 0:
        raise QuotaError(f"Cursor helper failed with rc={completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise QuotaError("Cursor helper returned invalid JSON") from exc
    return parse_cursor_usage(payload, fetched_at=now)


def _herdr(herdr_bin: str, args: list[str]) -> dict:
    command = args[1] if len(args) > 1 else args[0]
    try:
        completed = subprocess.run(
            [herdr_bin, *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(f"herdr {command} failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
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


def _refresh_grok(
    force: bool,
    cache_path: Path,
    now: int,
    grok_command: list[str] | None,
) -> tuple[GrokUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_grok_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < GROK_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_grok(now=now, grok_command=grok_command)
            save_grok_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def _refresh_glm(
    force: bool,
    cache_path: Path,
    now: int,
    glm_command: list[str] | None,
) -> tuple[GlmUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_glm_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < GLM_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_glm(now=now, glm_command=glm_command)
            save_glm_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def _refresh_antigravity(
    force: bool,
    cache_path: Path,
    now: int,
    antigravity_command: list[str] | None,
) -> tuple[AntigravityUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_antigravity_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if (
            not force
            and freshness
            and now - freshness < ANTIGRAVITY_REFRESH_INTERVAL_SECS
        ):
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_antigravity(now=now, antigravity_command=antigravity_command)
            save_antigravity_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def _refresh_kimi(
    force: bool,
    cache_path: Path,
    now: int,
    kimi_command: list[str] | None,
) -> tuple[KimiUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_kimi_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < KIMI_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_kimi(now=now, kimi_command=kimi_command)
            save_kimi_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def _refresh_cursor(
    force: bool,
    cache_path: Path,
    now: int,
    cursor_command: list[str] | None,
) -> tuple[CursorUsage | None, bool, str | None]:
    with _cache_lock(cache_path):
        cached = _discard_expired(cache_path, load_cursor_cache(cache_path), now)
        attempted_at = _load_attempt(_attempt_path(cache_path))
        freshness = max(
            cached.fetched_at if cached is not None else 0,
            attempted_at or 0,
        )
        if not force and freshness and now - freshness < CURSOR_REFRESH_INTERVAL_SECS:
            failed_after_cache = (
                cached is not None
                and attempted_at is not None
                and attempted_at > cached.fetched_at
            )
            return cached, failed_after_cache, None
        _save_attempt(_attempt_path(cache_path), now)
        try:
            usage = query_cursor(now=now, cursor_command=cursor_command)
            save_cursor_cache(cache_path, usage)
            return usage, False, None
        except QuotaError as exc:
            return cached, cached is not None, str(exc)


def state_dir_from_env() -> Path | None:
    """Resolve the cache directory: Herdr's variable first, then the standalone one."""
    for name in ("HERDR_PLUGIN_STATE_DIR", "MODEL_LANES_STATE_DIR"):
        value = os.environ.get(name)
        if value:
            return Path(value)
    return None


def refresh(
    force: bool = False,
    state_dir: Path | None = None,
    now: int | None = None,
    herdr_bin: str = "herdr",
    codex_bin: str = "codex",
    claude_command: list[str] | None = None,
    grok_command: list[str] | None = None,
    include_grok: bool = False,
    glm_command: list[str] | None = None,
    include_glm: bool = False,
    antigravity_command: list[str] | None = None,
    include_antigravity: bool = False,
    kimi_command: list[str] | None = None,
    include_kimi: bool = False,
    cursor_command: list[str] | None = None,
    include_cursor: bool = False,
    emit: bool = True,
) -> RefreshOutcome:
    if now is None:
        now = int(time.time())
    if state_dir is None:
        state_dir = state_dir_from_env()
        if state_dir is None:
            line = format_quota(None, None, now)
            print(line, file=sys.stdout if emit else sys.stderr, flush=True)
            return RefreshOutcome(None, None, False, False, ("no state dir",))

    codex, codex_stale, codex_error = _refresh_codex(
        force, state_dir / CODEX_CACHE_FILENAME, now, codex_bin
    )
    claude, claude_stale, claude_error = _refresh_claude(
        force, state_dir / CLAUDE_CACHE_FILENAME, now, claude_command
    )
    grok: GrokUsage | None = None
    grok_stale = False
    grok_error: str | None = None
    if include_grok:
        grok, grok_stale, grok_error = _refresh_grok(
            force, state_dir / GROK_CACHE_FILENAME, now, grok_command
        )
    glm: GlmUsage | None = None
    glm_stale = False
    glm_error: str | None = None
    if include_glm:
        glm, glm_stale, glm_error = _refresh_glm(
            force, state_dir / GLM_CACHE_FILENAME, now, glm_command
        )
    antigravity: AntigravityUsage | None = None
    antigravity_stale = False
    antigravity_error: str | None = None
    if include_antigravity:
        antigravity, antigravity_stale, antigravity_error = _refresh_antigravity(
            force,
            state_dir / ANTIGRAVITY_CACHE_FILENAME,
            now,
            antigravity_command,
        )
    kimi: KimiUsage | None = None
    kimi_stale = False
    kimi_error: str | None = None
    if include_kimi:
        kimi, kimi_stale, kimi_error = _refresh_kimi(
            force, state_dir / KIMI_CACHE_FILENAME, now, kimi_command
        )
    cursor: CursorUsage | None = None
    cursor_stale = False
    cursor_error: str | None = None
    if include_cursor:
        cursor, cursor_stale, cursor_error = _refresh_cursor(
            force, state_dir / CURSOR_CACHE_FILENAME, now, cursor_command
        )
    line = format_quota(
        codex,
        claude,
        now,
        codex_stale,
        claude_stale,
        grok if include_grok else UNSET,
        grok_stale,
        glm if include_glm else UNSET,
        glm_stale,
        antigravity if include_antigravity else UNSET,
        antigravity_stale,
        kimi if include_kimi else UNSET,
        kimi_stale,
        cursor if include_cursor else UNSET,
        cursor_stale,
    )
    try:
        publish_to_focused_workspace(line, herdr_bin=herdr_bin)
    except QuotaError:
        pass
    print(line, file=sys.stdout if emit else sys.stderr, flush=True)
    errors = tuple(
        error
        for error in (
            codex_error,
            claude_error,
            grok_error,
            glm_error,
            antigravity_error,
            kimi_error,
            cursor_error,
        )
        if error
    )
    return RefreshOutcome(
        codex,
        claude,
        codex_stale,
        claude_stale,
        errors,
        grok,
        grok_stale,
        glm,
        glm_stale,
        antigravity,
        antigravity_stale,
        kimi,
        kimi_stale,
        cursor,
        cursor_stale,
    )


# ---------------------------------------------------------------------------
# Model-class routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneSpec:
    name: str
    kind: str
    args: tuple[str, ...]
    quota: str
    classified_ok: bool


@dataclass(frozen=True)
class ClassSpec:
    name: str
    description: str
    lanes: tuple[LaneSpec, ...]


def load_class_spec(name: str, path: Path | None = None) -> ClassSpec:
    """Load one model class from ``classes.toml`` beside the manifest."""
    if path is None:
        path = Path(__file__).with_name(CLASSES_FILENAME)
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuotaError(
            f"cannot read {CLASSES_FILENAME}: {type(exc).__name__}"
        ) from exc
    classes = document.get("classes")
    entry = classes.get(name) if isinstance(classes, dict) else None
    if not isinstance(entry, dict):
        raise QuotaError(f"unknown model class: {name!r}")
    lanes = []
    for lane in entry.get("lanes", []):
        lanes.append(
            LaneSpec(
                name=str(lane["name"]),
                kind=str(lane["kind"]),
                args=tuple(str(arg) for arg in lane.get("args", [])),
                quota=str(lane["quota"]),
                classified_ok=lane.get("classified_ok", False) is True,
            )
        )
    if not lanes:
        raise QuotaError(f"model class {name!r} has no lanes")
    return ClassSpec(
        name=name,
        description=str(entry.get("description", "")),
        lanes=tuple(lanes),
    )


def _lane_health(window: QuotaWindow | None, now: int) -> float | None:
    """quota_left / time_left; ``None`` ranks the lane last."""
    if window is None or window.resets_at is None or window.resets_at <= now:
        return None
    time_left = max(
        (window.resets_at - now) / window.window_seconds,
        1e-9,
    )
    quota_left = max(0, min(100, window.remaining_percent)) / 100
    return quota_left / time_left


def select_lane(
    class_spec: ClassSpec,
    quotas: dict[str, QuotaWindow | None],
    now: int,
    classified: bool = False,
) -> tuple[LaneSpec, list[str]]:
    """Pick a lane from remaining quota; pure, unit-tested."""
    lanes = [lane for lane in class_spec.lanes if not classified or lane.classified_ok]
    if not lanes:
        raise QuotaError(f"model class {class_spec.name!r} has no eligible lanes")

    rationale: list[str] = []
    for lane in lanes:
        window = quotas.get(lane.quota)
        health = _lane_health(window, now)
        if health is None:
            rationale.append(f"{lane.name}: n/a ({lane.quota} quota unavailable)")
            continue
        remaining = max(0, min(100, window.remaining_percent))
        eta = _countdown(window.resets_at, now)
        rationale.append(
            f"{lane.name}: {remaining}% left, resets in {eta}, health {health:.2f}"
        )

    candidates = [
        (index, lane, quotas[lane.quota])
        for index, lane in enumerate(lanes)
        if (window := quotas.get(lane.quota)) is not None
        and (health := _lane_health(window, now)) is not None
        and health >= 1
        and max(0, min(100, window.remaining_percent)) >= 20
    ]
    surplus = [
        (index, lane, window)
        for index, lane, window in candidates
        if window.window_seconds > SURPLUS_RESET_WINDOW_SECS
        and _lane_health(window, now) >= SURPLUS_HEALTH
        and 0 <= window.resets_at - now <= SURPLUS_RESET_WINDOW_SECS
    ]
    if surplus:
        pick = min(
            surplus,
            key=lambda item: (-_lane_health(item[2], now), item[0]),
        )[1]
        reason = "surplus before reset"
    elif candidates:
        pick = candidates[0][1]
        reason = "first healthy lane in order"
    else:
        available = [
            (index, lane, quotas[lane.quota])
            for index, lane in enumerate(lanes)
            if (window := quotas.get(lane.quota)) is not None
            and _lane_health(window, now) is not None
        ]
        if available:
            pick = min(
                available,
                key=lambda item: (
                    -_lane_health(item[2], now),
                    item[0],
                ),
            )[1]
            reason = "least unhealthy lane"
        else:
            pick = lanes[0]
            reason = "no lane has quota data; first lane by order"
    rationale.append(f"pick: {pick.name} ({reason})")
    return pick, rationale


def _run_herdr_command(
    herdr_bin: str, args: list[str], what: str
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            [herdr_bin, *args],
            capture_output=True,
            text=True,
            timeout=LAUNCH_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(f"{what} failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise QuotaError(f"{what} failed with rc={completed.returncode}")
    return completed


def _launch(herdr_bin: str, class_spec: ClassSpec, lane: LaneSpec, line: str) -> None:
    """Create the tab, echo the rationale, and start the agent once."""
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    cwd = os.environ.get("HOME", "/tmp")
    if pane_id:
        completed = _run_herdr_command(
            herdr_bin, ["pane", "get", pane_id], "herdr pane get"
        )
        try:
            document = json.loads(completed.stdout or "{}")
            pane = (document.get("result") or document).get("pane") or {}
            pane_cwd = pane.get("cwd")
            if isinstance(pane_cwd, str) and pane_cwd:
                cwd = pane_cwd
        except ValueError:
            pass

    workspace_id = os.environ.get("HERDR_WORKSPACE_ID", "")
    if not workspace_id:
        raise QuotaError("launch requires HERDR_WORKSPACE_ID")
    completed = _run_herdr_command(
        herdr_bin,
        [
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            cwd,
            "--label",
            f"{class_spec.name}: {lane.name}",
        ],
        "herdr tab create",
    )
    try:
        document = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise QuotaError("herdr tab create returned invalid JSON") from exc
    result = document.get("result") or document
    root_pane = result.get("root_pane") or {}
    tab = result.get("tab") or {}
    new_pane = root_pane.get("pane_id")
    tabnum = tab.get("number") or tab.get("tab_id")
    if not isinstance(new_pane, str) or not new_pane:
        raise QuotaError("herdr tab create returned no pane id")

    _run_herdr_command(
        herdr_bin,
        ["pane", "run", new_pane, f"printf '%s\\n' {shlex.quote(line)}"],
        "herdr pane run",
    )
    _run_herdr_command(
        herdr_bin,
        [
            "agent",
            "start",
            f"{class_spec.name}-{lane.name}-{tabnum}",
            "--kind",
            lane.kind,
            "--pane",
            new_pane,
            "--",
            *lane.args,
        ],
        "herdr agent start",
    )


def _lane_executable(kind: str) -> str:
    """Map a lane kind to the binary `ag` will exec."""
    return LANE_EXECUTABLES.get(kind, kind)


def lane_command(lane: LaneSpec) -> list[str]:
    """The shell command a lane runs, executable first."""
    return [_lane_executable(lane.kind), *lane.args]


def _notify_route(herdr_bin: str, class_name: str, pick_line: str) -> None:
    """Show the pick as a Herdr notification; non-fatal without Herdr."""
    try:
        _run_herdr_command(
            herdr_bin,
            [
                "notification",
                "show",
                f"Route: {class_name}",
                "--body",
                pick_line,
            ],
            "herdr notification show",
        )
    except QuotaError as exc:
        print(f"notification skipped: {exc}", file=sys.stderr)


def _route_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="route",
        description="Pick a model-class lane from remaining subscription quota.",
        add_help=add_help,
    )
    parser.add_argument(
        "class_name",
        metavar="CLASS",
        help="Model class, such as medium or high",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print one line per lane plus the pick (default unless --launch or --argv)",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Create a Herdr tab and start the chosen agent",
    )
    parser.add_argument(
        "--argv",
        action="store_true",
        dest="argv_mode",
        help="Print the chosen command on stdout and the rationale on stderr",
    )
    parser.add_argument(
        "--classified",
        action="store_true",
        help="Hide lanes that are not classified-ok",
    )
    parser.add_argument("--lane", metavar="NAME", help="Override the pick with this lane")
    return parser


def _ag_parser(*, add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ag",
        description=(
            "Show each lane's quota, mark the suggestion, and exec the chosen "
            "agent in this shell. Enter accepts the star, a number starts that "
            "lane, q quits."
        ),
        epilog=(
            "Examples:\n"
            "  ag\n"
            "  ag high\n"
            "  ag --classified\n"
            "  ag medium -y\n"
            "\n"
            "Environment: AG_YES=1 skips the prompt; AG_TIMEOUT seconds until "
            "auto-accept (default 10)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=add_help,
    )
    parser.add_argument(
        "class_name",
        metavar="CLASS",
        nargs="?",
        default="medium",
        help="Model class (default: medium)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print the rationale and exit without starting an agent",
    )
    parser.add_argument(
        "--classified",
        action="store_true",
        help="Hide lanes that are not classified-ok",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Start the suggested lane without prompting",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herdr_model_lanes.py",
        description="Show subscription capacity and route a model-class lane.",
    )
    sub = parser.add_subparsers(dest="command")
    refresh_p = sub.add_parser("refresh", help="Refresh quota caches and publish the sidebar line")
    refresh_p.add_argument(
        "--force",
        action="store_true",
        help="Bypass per-source refresh intervals",
    )
    sub.add_parser("clear", help="Clear the model_quota token from every workspace")
    route_parent = _route_parser(add_help=False)
    sub.add_parser(
        "route",
        parents=[route_parent],
        help="Pick a lane for a model class",
        description=route_parent.description,
    )
    ag_parent = _ag_parser(add_help=False)
    sub.add_parser(
        "ag",
        parents=[ag_parent],
        help="Pick a lane and exec it in this shell",
        prog="ag",
        description=ag_parent.description,
        epilog=ag_parent.epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return parser


def resolve_route(
    class_name: str,
    *,
    classified: bool = False,
    lane_override: str | None = None,
    herdr_bin: str = "herdr",
    now: int | None = None,
    emit: bool = True,
) -> tuple[ClassSpec, LaneSpec, list[str]]:
    """Load quota, pick a lane, and apply an optional user override."""
    if now is None:
        now = int(time.time())
    class_spec = load_class_spec(class_name)
    outcome = refresh(
        state_dir=state_dir_from_env(),
        now=now,
        herdr_bin=herdr_bin,
        include_grok=True,
        include_glm=True,
        include_antigravity=True,
        include_kimi=True,
        include_cursor=True,
        emit=emit,
    )
    quotas: dict[str, QuotaWindow | None] = {
        "codex": outcome.codex.weekly if outcome.codex else None,
        "claude": outcome.claude.weekly if outcome.claude else None,
        "grok": outcome.grok.weekly if outcome.grok else None,
        "glm": outcome.glm.five_hour if outcome.glm else None,
        "antigravity": outcome.antigravity.gemini if outcome.antigravity else None,
        "kimi": outcome.kimi.coding if outcome.kimi else None,
        "cursor": outcome.cursor.monthly if outcome.cursor else None,
    }
    lane, rationale = select_lane(class_spec, quotas, now, classified=classified)
    if lane_override:
        chosen = next(
            (item for item in class_spec.lanes if item.name == lane_override), None
        )
        if chosen is None:
            raise QuotaError(f"no lane {lane_override!r} in class {class_name!r}")
        if classified and not chosen.classified_ok:
            raise QuotaError(f"lane {lane_override!r} is not classified-ok")
        lane = chosen
        rationale.append(f"override: {lane.name} (chosen by user)")
    return class_spec, lane, rationale


def route_command(
    argv: list[str],
    herdr_bin: str = "herdr",
    now: int | None = None,
) -> int:
    """``route <class> [--explain] [--launch] [--argv] [--classified] [--lane NAME]``."""
    parser = _route_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    argv_mode = args.argv_mode
    explain = args.explain or not (args.launch or argv_mode)
    try:
        class_spec, lane, rationale = resolve_route(
            args.class_name,
            classified=args.classified,
            lane_override=args.lane,
            herdr_bin=herdr_bin,
            now=now,
            emit=not argv_mode,
        )
    except QuotaError as exc:
        print(f"route: {exc}", file=sys.stderr)
        return 2 if "lane" in str(exc) else 1
    stream = sys.stderr if argv_mode else sys.stdout
    for line in rationale:
        print(line, file=stream)
    pick_line = rationale[-1]
    if argv_mode:
        print(shlex.join(lane_command(lane)), flush=True)
    if explain and not argv_mode:
        _notify_route(herdr_bin, args.class_name, pick_line)
    if args.launch:
        _launch(herdr_bin, class_spec, lane, pick_line)
    return 0


def picker_lines(rationale: list[str], pick_name: str) -> tuple[list[str], list[str]]:
    """Numbered picker rows and the lane names they refer to."""
    names: list[str] = []
    rows: list[str] = []
    for line in rationale:
        if line.startswith(("pick:", "override:")):
            continue
        name = line.split(":", 1)[0]
        names.append(name)
        mark = "*" if name == pick_name else " "
        rows.append(f"{mark} {len(names)}) {line}")
    return names, rows


def _ag_timeout_secs() -> int:
    raw = os.environ.get("AG_TIMEOUT", "10")
    try:
        return max(0, int(raw))
    except ValueError:
        return 10


def read_picker_choice(timeout_secs: int) -> str:
    """Read a picker answer from the controlling tty, or '' on timeout."""
    if not sys.stdin.isatty():
        return ""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return ""
    try:
        ready, _, _ = select.select([fd], [], [], timeout_secs)
        if not ready:
            return ""
        data = os.read(fd, 256).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)
    line = data.splitlines()[0] if data else ""
    return line.strip()


def _ensure_ag_state_dir() -> None:
    if state_dir_from_env() is not None:
        return
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "state"
    os.environ["HERDR_PLUGIN_STATE_DIR"] = str(
        root / "herdr" / "plugins" / "terry.herdr-model-lanes"
    )


def ag_command(
    argv: list[str],
    herdr_bin: str = "herdr",
    now: int | None = None,
    exec_fn=os.execvp,
    choice_fn=None,
) -> int:
    """``ag [CLASS] [--explain] [--classified] [-y]``."""
    parser = _ag_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    _ensure_ag_state_dir()
    assume_yes = args.yes or os.environ.get("AG_YES") == "1"
    try:
        class_spec, lane, rationale = resolve_route(
            args.class_name,
            classified=args.classified,
            herdr_bin=herdr_bin,
            now=now,
            emit=False,
        )
    except QuotaError as exc:
        print(f"ag: {exc}", file=sys.stderr)
        return 1
    names, rows = picker_lines(rationale, lane.name)
    for row in rows:
        print(row, file=sys.stderr)
    print(rationale[-1], file=sys.stderr)
    if args.explain:
        return 0
    timeout = _ag_timeout_secs()
    if not assume_yes:
        print(
            f"ag: Enter = start {lane.name}, number = other lane, q = quit "
            f"(auto in {timeout}s): ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        reader = choice_fn if choice_fn is not None else read_picker_choice
        answer = reader(timeout)
        print(file=sys.stderr)
        if answer in {"q", "Q"}:
            print("ag: aborted", file=sys.stderr)
            return 0
        if answer not in {"", "y", "Y"}:
            chosen = None
            for index, name in enumerate(names, start=1):
                if str(index) == answer:
                    chosen = name
                    break
            if chosen is None:
                print(f"ag: no lane number {answer}", file=sys.stderr)
                return 2
            try:
                class_spec, lane, rationale = resolve_route(
                    args.class_name,
                    classified=args.classified,
                    lane_override=chosen,
                    herdr_bin=herdr_bin,
                    now=now,
                    emit=False,
                )
            except QuotaError as exc:
                print(f"ag: {exc}", file=sys.stderr)
                return 2
            print(rationale[-1], file=sys.stderr)
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if pane_id:
        try:
            _run_herdr_command(
                herdr_bin,
                ["pane", "rename", pane_id, f"{class_spec.name}: {lane.name}"],
                "herdr pane rename",
            )
        except QuotaError:
            pass
        _notify_route(herdr_bin, f"ag {class_spec.name} -> {lane.name}", rationale[-1])
    print(f"ag: starting {lane.name}", file=sys.stderr)
    command = lane_command(lane)
    try:
        exec_fn(command[0], command)
    except FileNotFoundError:
        print(f"ag: {command[0]} not found on PATH", file=sys.stderr)
        return 127
    return 0


def _cmd_refresh(force: bool) -> int:
    include_grok = Path.home().joinpath(".grok", "auth.json").exists()
    refresh(
        force=force,
        include_grok=include_grok,
        include_glm=glm_usage.key_available(),
        include_antigravity=antigravity_usage.probe_available(),
        include_kimi=kimi_usage.key_available(),
        include_cursor=cursor_usage.key_available(),
    )
    return 0


def main(argv: list[str]) -> int:
    parser = build_parser()
    if not argv:
        return _cmd_refresh(force=False)
    if argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    if argv[0] not in {"refresh", "clear", "route", "ag"}:
        argv = ["refresh", *argv]
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command == "clear":
        clear_all()
        return 0
    if args.command == "route":
        try:
            return route_command(argv[1:], herdr_bin="herdr")
        except QuotaError as exc:
            print(f"route error: {exc}", file=sys.stderr)
            return 1
    if args.command == "ag":
        return ag_command(argv[1:])
    return _cmd_refresh(force=getattr(args, "force", False))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

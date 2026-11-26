"""Antigravity CLI status-line quota collector (experimental).

The Antigravity CLI (``agy``) invokes the configured ``statusLine`` command
for every render and pipes one JSON document over stdin. This collector
reads at most 1 MiB of that document, normalizes the ``quota`` bucket map
(the CLI reports remaining fractions per bucket, e.g. ``gemini-5h`` and
``gemini-weekly``), and persists the tightest Gemini window — normalized
used/remaining percentages and reset epoch — plus, when the payload reports
third-party (``3p``, Claude/GPT) buckets, the tightest of those, into the
plugin state directory as ``antigravity-quota.json``, the cache
``herdr_model_lanes.load_antigravity_cache`` reads. Every other input
field — ``session_id``, ``model``, ``workspace``/``cwd``, transcript paths
— is used only to render a status line and is never persisted. The
transcript file is never read; only its modification time is checked.

The CLI re-renders the status line on a timer, and a timer run in an idle
session carries the buckets from that session's last API response, however
old. The cache is therefore written only while the session reports a
non-idle ``agent_state`` or its transcript has changed within the last
minute, so an idle session can neither overwrite a busier session's newer
reading nor stamp an old one as fresh. A session whose transcript file does
not exist yet (startup launch) still counts as writable.

A non-idle session can still be stale: one started before a global quota
reset reports the pre-reset windows, with earlier reset timestamps, until
its next API response. Before overwriting, the collector therefore
re-reads the existing cache (bounded to 64 KiB, and treated as absent
when missing, unreadable, oversized or invalid) and, when that cache was
written within the last half hour, judges the Gemini and third-party
pools independently against its reset epochs — recorded additively under
``bucket_resets``, and for legacy caches derived from the stored
windows. A pool whose weekly or 5-hour reset epoch trails the fresh
cache is a rollback: the write keeps the cached window and epochs for
that pool, and is skipped outright when every reported pool regressed,
so the shared cache never ping-pongs backwards. Forward resets and
same-epoch consumption updates persist normally; the rendered status
line is never affected by the guard.

The rendered line reads, left to right: model and reasoning effort, context
depth in tokens, the active pool's 5h and weekly windows, a one-number
summary of the other pool, and active task/subagent counts. The active
pool follows the model: a Claude or GPT model reads the ``3p`` buckets with
a Gemini summary, anything else reads the Gemini buckets with a ``3p``
summary. Common display names are shortened to their codename, and a
trailing effort suffix never appears twice. Segments whose source is
absent are omitted or render ``n/a``, so the least important ones sit on
the right where a narrow terminal truncates first. Readings near a limit
are coloured unless ``NO_COLOR`` is set.

The width comes from the payload's ``terminal_width`` (the CLI knows its
real width) with the ``COLUMNS`` environment variable as a fallback; the
usable width is the terminal width minus four columns. When the full line
would not fit, detail is shed in a fixed order until it does: the other
pool's summary, the weekly countdown, the ``ctx`` label, the 5h countdown,
the bar separators, the effort and finally the model name. The context
depth, the active pool's percentages and the task and subagent counts are
never shed, so a phone-width pane still shows them.

When arguments follow ``--``, the original stdin bytes are forwarded to
that exact argv without a shell and with a short timeout; on success the
downstream command's stdout is passed through unchanged. Rendering happens
only when no downstream ran or the downstream failed.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

PLUGIN_ID_PATH = "herdr/plugins/terry.herdr-model-lanes"
CACHE_FILENAME = "antigravity-quota.json"
GEMINI_FIVE_HOUR_ID = "gemini-5h"
GEMINI_WEEKLY_ID = "gemini-weekly"
THIRD_PARTY_FIVE_HOUR_ID = "3p-5h"
THIRD_PARTY_WEEKLY_ID = "3p-weekly"
FIVE_HOUR_WINDOW_SECONDS = 5 * 3600
WEEKLY_WINDOW_SECONDS = 7 * 86400
# The quota pools the rollback guard judges independently, each with the
# bucket ids whose reset epochs it compares (weekly first, then 5-hour).
POOL_RESET_KEYS = {
    "gemini": (GEMINI_WEEKLY_ID, GEMINI_FIVE_HOUR_ID),
    "third_party": (THIRD_PARTY_WEEKLY_ID, THIRD_PARTY_FIVE_HOUR_ID),
}
MAX_INPUT_BYTES = 1 << 20
DOWNSTREAM_TIMEOUT_SECS = 1
MAX_RESET_AHEAD_SECS = 365 * 24 * 60 * 60
# A reset timestamp a little in the past is clock skew, not a stale window.
RESET_SKEW_SECS = 60
# A cache written within this window is defended against older sessions
# that still hold pre-reset quota buckets in memory.
STALE_GUARD_MAX_AGE_SECS = 30 * 60
# The existing cache is re-read before every guarded write; anything
# larger than this is treated as no cache at all.
MAX_CACHE_BYTES = 65536
# Context depth is coloured against absolute token counts, because the cost
# of a deep session follows tokens re-read per call, not the share of the
# window. Gemini sessions compact silently near 512K and carry a 400K
# hypoxia nudge, while third-party Claude/GPT pools warn at 200K/300K.
CONTEXT_WARN_TOKENS = 200_000
CONTEXT_HIGH_TOKENS = 300_000
GEMINI_CONTEXT_WARN_TOKENS = 350_000
GEMINI_CONTEXT_HIGH_TOKENS = 400_000
QUOTA_WARN_PERCENT = 70
QUOTA_HIGH_PERCENT = 90
# Efforts that drain a subscription quickly enough to deserve a visible
# flag, kept however narrow the terminal is.
COSTLY_EFFORT_LEVELS = frozenset({"xhigh", "max"})
EFFORT_TOKENS = frozenset({"low", "medium", "high", "xhigh", "max"})
# A session whose transcript has been quiet for longer than this is idle,
# and the buckets in its payload are as old as that quiet period.
ACTIVE_TRANSCRIPT_SECS = 60
IDLE_AGENT_STATE = "idle"
DEFAULT_MODEL = "Gemini"
# The CLI indents the line by two columns and truncates two short of the
# right edge, so the usable width is terminal_width minus this margin.
STATUS_LINE_MARGIN_COLUMNS = 4
MIN_PLAUSIBLE_COLUMNS = 10
MAX_PLAUSIBLE_COLUMNS = 2000
# Detail shed, in order, when the line is wider than the terminal.
WIDTH_REDUCTIONS = (
    "secondary_pool",
    "weekly_countdown",
    "context_label",
    "five_hour_countdown",
    "bar_separator",
    "effort",
    "model",
)
# Long codenames shortened to their common form in the status line.
CODENAME_ABBREVIATIONS = {"Hydrogen": "Hydro"}
# Third-party model markers that switch the primary pool away from Gemini.
THIRD_PARTY_MARKERS = ("claude", "gpt")
ANSI_WARN = "\033[33m"
ANSI_HIGH = "\033[31m"
ANSI_RESET = "\033[0m"


class StatuslineError(Exception):
    """Raised on collector failure; messages never echo stdin content."""


def _base_state_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the base state directory with the same precedence as ``ag``."""
    if state_dir is not None:
        return Path(os.fspath(state_dir))
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.environ.get(
        "MODEL_LANES_STATE_DIR"
    )
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(xdg) / PLUGIN_ID_PATH


def statusline_cache_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the antigravity quota cache path with the ``ag`` precedence."""
    return _base_state_path(state_dir) / CACHE_FILENAME


def _is_finite_number(value: object) -> bool:
    """True for a non-bool int/float that is neither NaN nor infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _window_seconds_for(bucket_id: str) -> int:
    """Window length from the bucket id, defaulting to the weekly window."""
    if "5h" in bucket_id.lower():
        return FIVE_HOUR_WINDOW_SECONDS
    return WEEKLY_WINDOW_SECONDS


def _pool_for_bucket(bucket_id: str) -> str | None:
    """The quota pool a bucket id belongs to, or None for foreign buckets.

    ``gemini-5h``/``gemini-weekly`` and any ``gemini*`` id read the Gemini
    pool; ``3p-5h``/``3p-weekly`` and any ``3p*``/``claude*`` id read the
    third-party pool. The tightest-window selection and the rollback
    guard share this classification, so a bucket can never count for
    one and not the other.
    """
    if bucket_id in (GEMINI_FIVE_HOUR_ID, GEMINI_WEEKLY_ID):
        return "gemini"
    if bucket_id in (THIRD_PARTY_FIVE_HOUR_ID, THIRD_PARTY_WEEKLY_ID):
        return "third_party"
    lowered = bucket_id.lower()
    if lowered.startswith("gemini"):
        return "gemini"
    if lowered.startswith(("3p", "claude")):
        return "third_party"
    return None


def _resets_at(bucket: dict, now: int) -> int | None:
    """Reset epoch from ``reset_time``, else ``now + reset_in_seconds``."""
    reset_time = bucket.get("reset_time")
    if reset_time is not None:
        if not isinstance(reset_time, str) or not reset_time:
            return None
        try:
            parsed = datetime.fromisoformat(reset_time)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp())
    reset_in = bucket.get("reset_in_seconds")
    if not _is_finite_number(reset_in):
        return None
    return now + round(reset_in)


def _normalize_bucket(bucket_id: str, value: object, now: int) -> dict | None:
    """Return one normalized bucket window, or None when absent or invalid.

    The CLI reports ``remaining_fraction``; it is rounded to the
    ``used_percent``/``remaining_percent`` pair the Model Lanes cache
    holds. NaN and infinity are rejected because ``json.loads`` accepts
    them, and resets beyond a little skew in the past or more than a year
    ahead are rejected as implausible.
    """
    if not isinstance(value, dict):
        return None
    fraction = value.get("remaining_fraction")
    if not _is_finite_number(fraction) or not 0 <= fraction <= 1:
        return None
    resets_at = _resets_at(value, now)
    if resets_at is None:
        return None
    if resets_at < now - RESET_SKEW_SECS or resets_at > now + MAX_RESET_AHEAD_SECS:
        return None
    remaining_percent = round(100 * fraction)
    return {
        "remaining_fraction": fraction,
        "used_percent": 100 - remaining_percent,
        "remaining_percent": remaining_percent,
        "resets_at": resets_at,
        "window_seconds": _window_seconds_for(bucket_id),
    }


def parse_quota_document(document: object, now: int) -> dict[str, dict]:
    """Return the valid normalized ``quota`` windows keyed by bucket id."""
    if not isinstance(document, dict):
        raise StatuslineError("status-line payload is not an object")
    quota = document.get("quota")
    if not isinstance(quota, dict):
        return {}
    windows: dict[str, dict] = {}
    for bucket_id, value in quota.items():
        if not isinstance(bucket_id, str) or not bucket_id:
            continue
        window = _normalize_bucket(bucket_id, value, now)
        if window is not None:
            windows[bucket_id] = window
    return windows


def tightest_gemini_window(windows: dict[str, dict]) -> dict | None:
    """Pick the tightest Gemini bucket, matching the usage helper.

    Candidates are ``gemini-5h``, ``gemini-weekly`` and any bucket whose
    lowercase id starts with ``gemini``; the smallest remaining fraction
    wins, with the longer window breaking a tie.
    """
    candidates = [
        window
        for bucket_id, window in windows.items()
        if _pool_for_bucket(bucket_id) == "gemini"
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda window: (window["remaining_fraction"], -window["window_seconds"]),
    )


def tightest_third_party_window(windows: dict[str, dict]) -> dict | None:
    """Pick the tightest third-party (Claude/GPT) bucket, when present.

    Candidates are ``3p-5h``, ``3p-weekly`` and any bucket whose lowercase
    id starts with ``3p`` or ``claude``; the smallest remaining fraction
    wins, with the longer window breaking a tie. Returns ``None`` when the
    CLI reports no third-party pool.
    """
    candidates = [
        window
        for bucket_id, window in windows.items()
        if _pool_for_bucket(bucket_id) == "third_party"
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda window: (window["remaining_fraction"], -window["window_seconds"]),
    )


def save_cache(
    path: Path,
    fetched_at: int,
    window: dict | None,
    third_party: dict | None = None,
    *,
    bucket_resets: dict[str, int] | None = None,
) -> None:
    """Atomically persist the normalized windows with mode 0600.

    The Gemini window keeps the exact ``"gemini"`` schema it always had,
    and a third-party window, when known, is stored beside it under
    ``"third_party"`` with the same four keys. Callers that pass only a
    Gemini window keep writing the legacy two-key payload. A non-empty
    ``bucket_resets`` map of bucket ids to reset epochs is stored
    additively at top level so a later run can tell a pre-reset rollback
    from a fresh reading; readers that do not know the key ignore it.
    """
    payload: dict[str, dict | int] = {}
    if window is not None:
        payload["gemini"] = {
            "used_percent": window["used_percent"],
            "remaining_percent": window["remaining_percent"],
            "resets_at": window["resets_at"],
            "window_seconds": window["window_seconds"],
        }
    if third_party is not None:
        payload["third_party"] = {
            "used_percent": third_party["used_percent"],
            "remaining_percent": third_party["remaining_percent"],
            "resets_at": third_party["resets_at"],
            "window_seconds": third_party["window_seconds"],
        }
    payload["fetched_at"] = fetched_at
    if isinstance(bucket_resets, dict) and bucket_resets:
        resets = {
            bucket_id: int(epoch)
            for bucket_id, epoch in bucket_resets.items()
            if isinstance(bucket_id, str) and _is_finite_number(epoch)
        }
        if resets:
            payload["bucket_resets"] = resets
    encoded = json.dumps(payload).encode("utf-8")
    directory = path.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(directory), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_existing_cache(path: Path) -> dict | None:
    """The existing cache payload, or None when absent or unusable.

    The read is bounded to ``MAX_CACHE_BYTES``; a missing, unreadable,
    oversized, non-object or invalid-JSON file counts as no cache, so a
    fresh reading always wins over a broken one.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_CACHE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_CACHE_BYTES:
        return None
    try:
        existing = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return existing if isinstance(existing, dict) else None


def _cache_is_fresh(existing: dict, now: int) -> bool:
    """True when the existing cache is recent enough to be defended."""
    fetched_at = existing.get("fetched_at")
    if not _is_finite_number(fetched_at):
        return False
    return now - STALE_GUARD_MAX_AGE_SECS <= fetched_at <= now + RESET_SKEW_SECS


def _reset_bucket_for(pool: str, window_seconds: object) -> str | None:
    """The canonical ``bucket_resets`` key for a pool and window length."""
    if window_seconds == FIVE_HOUR_WINDOW_SECONDS:
        return POOL_RESET_KEYS[pool][1]
    if window_seconds == WEEKLY_WINDOW_SECONDS:
        return POOL_RESET_KEYS[pool][0]
    return None


def _reset_epochs_from_windows(windows: dict[str, dict]) -> dict[str, int]:
    """Reset epochs from the payload, keyed by canonical bucket id.

    Every Gemini and third-party bucket contributes the epoch of its
    window length; when several buckets share a length, the latest epoch
    wins.
    """
    epochs: dict[str, int] = {}
    for bucket_id, window in windows.items():
        pool = _pool_for_bucket(bucket_id)
        if pool is None:
            continue
        key = _reset_bucket_for(pool, window["window_seconds"])
        if key is None:
            continue
        epoch = window["resets_at"]
        if key not in epochs or epoch > epochs[key]:
            epochs[key] = epoch
    return epochs


def _reset_epochs_from_cache(existing: dict) -> dict[str, int]:
    """Reset epochs held by the existing cache, keyed by canonical bucket id.

    Epochs come from ``bucket_resets`` when present, and from the stored
    Gemini/third-party windows — their ``window_seconds`` and
    ``resets_at`` — so a legacy cache file without the metadata still
    guards. ``bucket_resets`` wins when both describe a window.
    """
    epochs: dict[str, int] = {}
    resets = existing.get("bucket_resets")
    if isinstance(resets, dict):
        for bucket_id, epoch in resets.items():
            if not isinstance(bucket_id, str) or not _is_finite_number(epoch):
                continue
            pool = _pool_for_bucket(bucket_id)
            if pool is None:
                continue
            key = _reset_bucket_for(pool, _window_seconds_for(bucket_id))
            if key is None:
                continue
            if key not in epochs or epoch > epochs[key]:
                epochs[key] = epoch
    for pool in POOL_RESET_KEYS:
        entry = existing.get(pool)
        if not isinstance(entry, dict):
            continue
        epoch = entry.get("resets_at")
        if not _is_finite_number(epoch):
            continue
        key = _reset_bucket_for(pool, entry.get("window_seconds"))
        if key is not None and key not in epochs:
            epochs[key] = epoch
    return epochs


def _pool_is_regressed(
    pool: str, incoming: dict[str, int], existing: dict[str, int]
) -> bool:
    """True when one pool's incoming reading trails the fresh cache.

    The pool regressed when its weekly reset epoch is earlier than the
    cached one, or — the weekly epochs agreeing within skew, or missing
    on at least one side — when its 5-hour epoch is earlier than the
    cached one. A forward weekly reset is progress, never a regression.
    """
    weekly, five = POOL_RESET_KEYS[pool]
    incoming_weekly = incoming.get(weekly)
    existing_weekly = existing.get(weekly)
    if incoming_weekly is not None and existing_weekly is not None:
        if incoming_weekly < existing_weekly - RESET_SKEW_SECS:
            return True
        if abs(incoming_weekly - existing_weekly) > RESET_SKEW_SECS:
            return False
    incoming_five = incoming.get(five)
    existing_five = existing.get(five)
    return (
        incoming_five is not None
        and existing_five is not None
        and incoming_five < existing_five - RESET_SKEW_SECS
    )


def _cache_pool_entry(entry: object) -> dict | None:
    """A cached pool window ready to write back, or None when unusable.

    Exactly the four keys ``save_cache`` persists are copied, so a
    preserved pool keeps the schema and nothing else; a malformed entry
    is ignored rather than crashing the collector.
    """
    if not isinstance(entry, dict):
        return None
    window: dict[str, int | float] = {}
    for key in ("used_percent", "remaining_percent", "resets_at", "window_seconds"):
        value = entry.get(key)
        if not _is_finite_number(value):
            return None
        window[key] = value
    return window


def _guarded_cache_update(
    existing: dict | None,
    now: int,
    gemini: dict | None,
    third_party: dict | None,
    incoming_resets: dict[str, int],
) -> tuple[dict | None, dict | None, dict[str, int]] | None:
    """The windows and reset epochs main() may persist, or None to skip.

    With no fresh cache to defend — the file missing, unreadable, or
    older than ``STALE_GUARD_MAX_AGE_SECS`` — the incoming reading is
    persisted unchanged. Otherwise each pool is judged independently
    against the cached reset epochs: a regressed pool keeps the cached
    window and epochs, a pool the payload omits keeps whatever the cache
    holds, and a healthy pool is written with its epochs merged over the
    cached ones. None is returned only when every pool the payload
    reports regressed, leaving the cache — ``fetched_at`` included —
    untouched.
    """
    if existing is None or not _cache_is_fresh(existing, now):
        return gemini, third_party, dict(incoming_resets)
    existing_resets = _reset_epochs_from_cache(existing)
    incoming_windows = {"gemini": gemini, "third_party": third_party}
    planned_windows = {
        "gemini": _cache_pool_entry(existing.get("gemini")),
        "third_party": _cache_pool_entry(existing.get("third_party")),
    }
    planned_resets = {
        pool: {
            key: epoch
            for key, epoch in existing_resets.items()
            if key in POOL_RESET_KEYS[pool]
        }
        for pool in POOL_RESET_KEYS
    }
    reported: set[str] = set()
    regressed: set[str] = set()
    for pool, keys in POOL_RESET_KEYS.items():
        pool_resets = {
            key: epoch for key, epoch in incoming_resets.items() if key in keys
        }
        if not pool_resets:
            continue  # The payload omits this pool: keep the cached one.
        reported.add(pool)
        if _pool_is_regressed(pool, pool_resets, existing_resets):
            regressed.add(pool)
            continue
        planned_windows[pool] = incoming_windows[pool]
        planned_resets[pool].update(pool_resets)
    if regressed and regressed == reported:
        # Every pool this payload reports still holds pre-reset windows.
        return None
    merged: dict[str, int] = {}
    for epochs in planned_resets.values():
        merged.update(epochs)
    return planned_windows["gemini"], planned_windows["third_party"], merged


def session_is_current(document: object, now: int) -> bool:
    """False when an idle session's buckets may be stale.

    The CLI re-renders on a timer; a session reporting ``idle`` whose
    transcript has been quiet for over a minute carries windows from its
    last response, however old. A payload with no transcript path, a
    transcript that does not exist yet (startup launch), a recently
    touched transcript or a non-idle state counts as current.
    """
    if not isinstance(document, dict):
        return True
    if document.get("agent_state") != IDLE_AGENT_STATE:
        return True
    transcript = document.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return True
    try:
        modified = os.stat(transcript).st_mtime
    except OSError:
        return True
    return now - modified <= ACTIVE_TRANSCRIPT_SECS


def _countdown(resets_at: int, now: int) -> str:
    seconds = max(resets_at - now, 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24}h"


def _paint(text: str, level: str | None, color: bool) -> str:
    """Wrap text in the ANSI colour for ``level`` ("warn" or "high")."""
    if not color or level is None:
        return text
    return f"{ANSI_HIGH if level == 'high' else ANSI_WARN}{text}{ANSI_RESET}"


def _level(value: float, warn_at: float, high_at: float) -> str | None:
    if value >= high_at:
        return "high"
    if value >= warn_at:
        return "warn"
    return None


def _is_effort_token(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z]{1,8}", value))


def _paren_effort(paren: str | None) -> str | None:
    """The effort level inside a display-name suffix, when it is one."""
    if paren is None:
        return None
    lowered = paren.lower()
    return lowered if lowered in EFFORT_TOKENS else None


def _shorten_model(raw: str, explicit: str | None) -> tuple[str, str | None]:
    """Shorten a display name and split off its trailing effort suffix.

    ``"Gemini 4 Neon (XHigh)"`` reads ``"Neon 4"`` with effort ``xhigh``;
    ``"Claude Opus 5.5 (High)"`` reads ``"Opus 5.5"``; ``"Orchestrator
    (Neon)"`` reads ``"Orch Neon"``. An explicit ``model.effort`` wins over
    the suffix, and the suffix is stripped whenever an effort is displayed
    separately so it never appears twice.
    """
    match = re.fullmatch(r"Gemini (\d+(?:\.\d+)?) (\S+)(?: \(([^()]+)\))?", raw)
    if match:
        effort = explicit if explicit is not None else _paren_effort(match.group(3))
        codename = CODENAME_ABBREVIATIONS.get(match.group(2), match.group(2))
        return f"{codename} {match.group(1)}", effort
    match = re.fullmatch(r"Claude (\S+) (\d+(?:\.\d+)?)(?: \(([^()]+)\))?", raw)
    if match:
        effort = explicit if explicit is not None else _paren_effort(match.group(3))
        return f"{match.group(1)} {match.group(2)}", effort
    match = re.fullmatch(r"Orchestrator \(([^()]+)\)", raw)
    if match:
        return f"Orch {match.group(1)}", explicit
    match = re.fullmatch(r"(.+) \(([^()]+)\)", raw)
    if match:
        effort = explicit if explicit is not None else _paren_effort(match.group(2))
        if effort is not None:
            return match.group(1), effort
    return raw, explicit


def _model_and_effort(document: object) -> tuple[str, str | None, bool]:
    """Short model label, effort level, and whether the pool is third-party."""
    display = ""
    identifier = ""
    explicit: str | None = None
    if isinstance(document, dict):
        model_obj = document.get("model")
        if isinstance(model_obj, dict):
            value = model_obj.get("display_name")
            if isinstance(value, str) and value:
                display = value
            value = model_obj.get("id")
            if isinstance(value, str) and value:
                identifier = value
            effort = model_obj.get("effort")
            if _is_effort_token(effort):
                explicit = effort.lower()
    raw = display or identifier
    if raw:
        name, effort = _shorten_model(raw, explicit)
    else:
        name, effort = DEFAULT_MODEL, explicit
    third_party = any(
        marker in source.lower()
        for source in (display, identifier)
        if source
        for marker in THIRD_PARTY_MARKERS
    )
    return name, effort, third_party


def _context_tokens(document: object) -> float | None:
    """Context depth in tokens, or None before the first response.

    Prefers ``context_window.current_usage`` — fresh input plus cache
    writes and reads from the most recent response — and falls back to
    ``context_window.total_input_tokens``.
    """
    if not isinstance(document, dict):
        return None
    window = document.get("context_window")
    if not isinstance(window, dict):
        return None
    usage = window.get("current_usage")
    if isinstance(usage, dict):
        total = 0.0
        seen = False
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if _is_finite_number(value):
                seen = True
                total += value
        if seen and total > 0:
            return total
    tokens = window.get("total_input_tokens")
    if _is_finite_number(tokens) and tokens > 0:
        return tokens
    return None


def _context_amount(tokens: float) -> str:
    if tokens >= 999_500:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{round(tokens / 1000)}K"


def _activity(document: object) -> str | None:
    """Active task and running-subagent counts, e.g. ``"2t 1sub"``."""
    if not isinstance(document, dict):
        return None
    parts: list[str] = []
    count = document.get("task_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        parts.append(f"{count}t")
    subagents = document.get("subagents")
    if isinstance(subagents, list):
        running = sum(
            1
            for agent in subagents
            if isinstance(agent, dict) and agent.get("status") == "running"
        )
        if running:
            parts.append(f"{running}sub")
    return " ".join(parts) if parts else None


def _secondary_segment(
    windows: dict[str, dict], *, gemini_primary: bool
) -> tuple[str, str | None] | None:
    """One-number summary of the pool the model is not drawing from.

    A Gemini model summarises the third-party weekly bucket only once it
    shows use; a third-party model always summarises the Gemini weekly
    bucket when the CLI reports one.
    """
    if gemini_primary:
        window = windows.get(THIRD_PARTY_WEEKLY_ID)
        if window is None or window["used_percent"] <= 0:
            return None
        label = "3p"
    else:
        window = windows.get(GEMINI_WEEKLY_ID)
        if window is None:
            return None
        label = "G"
    used = window["used_percent"]
    return f"{label} {used}%", _level(used, QUOTA_WARN_PERCENT, QUOTA_HIGH_PERCENT)


def _window_segment(
    label: str, window: dict | None, now: int, *, countdown: bool = True
) -> tuple[str, str | None]:
    """One quota window as (text, colour level)."""
    if window is None:
        return f"{label} n/a", None
    text = f"{label} {window['used_percent']}%"
    if countdown:
        text += f" ({_countdown(window['resets_at'], now)})"
    return text, _level(window["used_percent"], QUOTA_WARN_PERCENT, QUOTA_HIGH_PERCENT)


def _columns_from_env() -> int | None:
    """Terminal width from ``COLUMNS``."""
    raw = os.environ.get("COLUMNS", "")
    if not raw.isascii() or not raw.isdigit():
        return None
    columns = int(raw)
    if not MIN_PLAUSIBLE_COLUMNS <= columns <= MAX_PLAUSIBLE_COLUMNS:
        return None
    return columns


def _terminal_columns(document: object) -> int | None:
    """Terminal width from the payload's ``terminal_width``, else ``COLUMNS``."""
    if isinstance(document, dict):
        width = document.get("terminal_width")
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and MIN_PLAUSIBLE_COLUMNS <= width <= MAX_PLAUSIBLE_COLUMNS
        ):
            return width
    return _columns_from_env()


def render_status_line(
    document: object,
    windows: dict[str, dict],
    now: int,
    *,
    color: bool | None = None,
    columns: int | None = None,
) -> str:
    """Render model, effort, context depth, quota pools and activity.

    ``color`` defaults to on unless the ``NO_COLOR`` environment variable
    is set to a non-empty value. ``columns`` defaults to the payload's
    ``terminal_width`` and then the ``COLUMNS`` environment variable; when
    the line is wider than the terminal, detail is shed in
    ``WIDTH_REDUCTIONS`` order until it fits or nothing is left to shed.
    With no known width the full line is printed.
    """
    if color is None:
        color = not os.environ.get("NO_COLOR")
    if columns is None:
        columns = _terminal_columns(document)
    model, effort, third_party = _model_and_effort(document)
    tokens = _context_tokens(document)
    activity = _activity(document)
    secondary = _secondary_segment(windows, gemini_primary=not third_party)
    if third_party:
        five_id, weekly_id = THIRD_PARTY_FIVE_HOUR_ID, THIRD_PARTY_WEEKLY_ID
        five_label, weekly_label = "3p5h", "3p7d"
        context_warn, context_high = CONTEXT_WARN_TOKENS, CONTEXT_HIGH_TOKENS
    else:
        five_id, weekly_id = GEMINI_FIVE_HOUR_ID, GEMINI_WEEKLY_ID
        five_label, weekly_label = "5h", "7d"
        context_warn, context_high = (
            GEMINI_CONTEXT_WARN_TOKENS,
            GEMINI_CONTEXT_HIGH_TOKENS,
        )
    five_window = windows.get(five_id)
    weekly_window = windows.get(weekly_id)
    costly_effort = effort in COSTLY_EFFORT_LEVELS

    def compose(shed: frozenset[str]) -> tuple[list[tuple[str, str | None]], str]:
        segments: list[tuple[str, str | None]] = []
        if "model" not in shed:
            segments.append((model, None))
        # A costly effort stays visible however narrow the terminal is.
        if effort is not None and ("effort" not in shed or costly_effort):
            segments.append((effort, "warn" if costly_effort else None))
        if tokens is not None:
            label = "" if "context_label" in shed else "ctx "
            segments.append(
                (
                    f"{label}{_context_amount(tokens)}",
                    _level(tokens, context_warn, context_high),
                )
            )
        segments.append(
            _window_segment(
                five_label,
                five_window,
                now,
                countdown="five_hour_countdown" not in shed,
            )
        )
        segments.append(
            _window_segment(
                weekly_label,
                weekly_window,
                now,
                countdown="weekly_countdown" not in shed,
            )
        )
        if secondary is not None and "secondary_pool" not in shed:
            segments.append(secondary)
        if activity is not None:
            segments.append((activity, None))
        return segments, " " if "bar_separator" in shed else " | "

    def join(
        segments: list[tuple[str, str | None]], separator: str, paint: bool
    ) -> str:
        # Model and effort read as one unit, so only a space ever parts them.
        pieces = []
        for index, (text, level) in enumerate(segments):
            if index:
                glued = index == 1 and "model" not in shed and segments[1][0] == effort
                pieces.append(" " if glued else separator)
            pieces.append(_paint(text, level, paint))
        return "".join(pieces)

    shed: frozenset[str] = frozenset()
    segments, separator = compose(shed)
    if columns is not None:
        budget = columns - STATUS_LINE_MARGIN_COLUMNS
        for reduction in WIDTH_REDUCTIONS:
            if len(join(segments, separator, False)) <= budget:
                break
            shed = shed | {reduction}
            segments, separator = compose(shed)
    return join(segments, separator, color)


def run_downstream(argv: list[str], stdin_bytes: bytes) -> bool:
    """Forward stdin to argv without a shell; pass stdout through on success."""
    if not argv:
        return False
    try:
        completed = subprocess.run(
            argv,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            timeout=DOWNSTREAM_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    sys.stdout.buffer.write(completed.stdout)
    sys.stdout.buffer.flush()
    return True


def read_stdin_bounded(stream: object) -> bytes:
    """Read at most MAX_INPUT_BYTES; refuse larger payloads."""
    data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise StatuslineError("status-line input exceeds 1 MiB")
    return data


def main(argv: list[str] | None = None) -> int:
    """Collect, optionally forward, and render; never leak a traceback."""
    if argv is None:
        argv = sys.argv[1:]
    separator = argv.index("--") if "--" in argv else len(argv)
    if argv[:separator]:
        print(
            "usage: antigravity-statusline [-- downstream-command args...]",
            file=sys.stderr,
        )
        return 2
    downstream = argv[separator + 1 :]
    try:
        stdin_bytes = read_stdin_bounded(sys.stdin.buffer)
        now = int(time.time())
        try:
            document = json.loads(stdin_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise StatuslineError("status-line input is not valid JSON") from exc
        windows = parse_quota_document(document, now)
        gemini = tightest_gemini_window(windows)
        third_party = tightest_third_party_window(windows)
        if (gemini is not None or third_party is not None) and session_is_current(
            document, now
        ):
            try:
                cache_path = statusline_cache_path()
                update = _guarded_cache_update(
                    _read_existing_cache(cache_path),
                    now,
                    gemini,
                    third_party,
                    _reset_epochs_from_windows(windows),
                )
                if update is not None:
                    window, third, resets = update
                    save_cache(
                        cache_path,
                        now,
                        window,
                        third_party=third,
                        bucket_resets=resets,
                    )
            except OSError:
                # A cache write failure must never suppress an existing
                # status line; warn once and keep displaying.
                print(
                    "antigravity statusline warning: cannot persist cache",
                    file=sys.stderr,
                )
        if downstream and run_downstream(downstream, stdin_bytes):
            return 0
        print(render_status_line(document, windows, now))
        return 0
    except StatuslineError as exc:
        print(f"antigravity statusline error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

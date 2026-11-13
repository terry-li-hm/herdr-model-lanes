"""Claude Code status-line quota collector (experimental).

Claude Code invokes the configured ``statusLine`` command for every render
and pipes one JSON document over stdin. This collector reads at most 1 MiB
of that document, normalizes the ``rate_limits.five_hour`` and
``rate_limits.seven_day`` windows, and persists only ``captured_at`` plus
those two windows (normalized ``utilization`` and reset epoch) into the
plugin state directory. Input windows use the documented
``used_percentage`` field, which is normalized to ``utilization`` in the
cache. Every other input field — ``session_id``, ``model``,
``workspace``/``cwd``, transcript paths, prompts — is used only to render a
status line and is never persisted.

The rendered line reads, left to right: model and reasoning effort, context
depth in tokens, the 5h and 7d windows, the model-scoped weekly limit when a
fresh cache holds one, and a short label for a non-default
``CLAUDE_CONFIG_DIR``. Segments whose source is absent are omitted, so the
least important ones sit on the right where a narrow terminal truncates
first. Readings near a limit are coloured unless ``NO_COLOR`` is set.

Claude Code exports the terminal width as ``COLUMNS``. When the full line
would not fit, detail is shed in a fixed order until it does: the account
label, the weekly countdowns, the ``ctx`` label, the 5h countdown, the bar
separators, the effort and finally the model name. The percentages and the
context depth are never shed, so a phone-width pane still shows them.

When arguments follow ``--``, the original stdin bytes are forwarded to that
exact argv without a shell and with a short timeout; on success the
downstream command's stdout is passed through unchanged. Rendering happens
only when no downstream ran or the downstream failed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ID_PATH = "herdr/plugins/terry.herdr-model-lanes"
CACHE_FILENAME = "claude-statusline.json"
SCOPED_CACHE_FILENAME = "claude-scoped-limit.json"
SCOPED_CACHE_MAX_AGE_SECS = 24 * 3600
MAX_INPUT_BYTES = 1 << 20
MAX_CACHE_BYTES = 64 * 1024
DOWNSTREAM_TIMEOUT_SECS = 1
MAX_RESET_AHEAD_SECS = 365 * 24 * 60 * 60
# Context depth is coloured against absolute token counts, because the cost of
# a deep session follows tokens re-read per call, not the share of the window.
CONTEXT_WARN_TOKENS = 150_000
CONTEXT_HIGH_TOKENS = 200_000
QUOTA_WARN_PERCENT = 70
QUOTA_HIGH_PERCENT = 90
# Efforts that drain a subscription quickly enough to deserve a visible flag.
COSTLY_EFFORT_LEVELS = frozenset({"max"})
SAME_RESET_TOLERANCE_SECS = 60
MAX_ACCOUNT_LABEL_CHARS = 12
# Claude Code indents the line by two columns and truncates two short of the
# right edge, so the usable width is COLUMNS minus this margin.
STATUS_LINE_MARGIN_COLUMNS = 4
MIN_PLAUSIBLE_COLUMNS = 10
MAX_PLAUSIBLE_COLUMNS = 2000
# Detail shed, in order, when the line is wider than the terminal.
WIDTH_REDUCTIONS = (
    "account",
    "weekly_countdown",
    "context_label",
    "five_hour_countdown",
    "bar_separator",
    "effort",
    "model",
)
ANSI_WARN = "\033[33m"
ANSI_HIGH = "\033[31m"
ANSI_RESET = "\033[0m"


class StatuslineError(Exception):
    """Raised on collector failure; messages never echo stdin content."""


def _profile_subdir(config_dir: str | os.PathLike[str] | None = None) -> str | None:
    """Profile-keyed state subdir for a non-default CLAUDE_CONFIG_DIR.

    Returns None for the default ``~/.claude`` profile and for a blank
    setting, so those keep using the root state directory. A sibling of the
    home directory (``~/.claude-account2``) keys by basename; any other
    directory is keyed by a sanitized basename plus a path digest.
    """
    raw = (
        os.fspath(config_dir)
        if config_dir is not None
        else os.environ.get("CLAUDE_CONFIG_DIR", "")
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    norm = os.path.normpath(os.path.expanduser(raw.strip()))
    default_cfg = os.path.normpath(os.path.join(os.path.expanduser("~"), ".claude"))
    if norm == default_cfg:
        return None
    home_norm = os.path.normpath(os.path.expanduser("~"))
    base = os.path.basename(norm)
    if (
        os.path.dirname(norm) == home_norm
        and re.fullmatch(r"\.?[A-Za-z0-9._-]+", base)
        and base not in (".", "..")
    ):
        return os.path.join("profiles", base)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "profile"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    return os.path.join("profiles", f"{safe}-{digest}")


def _already_profile_scoped(base_dir: str) -> bool:
    """True when base_dir is already a per-profile state directory.

    Callers such as quota_handoff.py pass a ``.../profile-<hash>`` state dir
    and claude-pick passes ``.../profiles/<config-basename>``; those must not
    get a second ``profiles/`` level appended.
    """
    norm_base = os.path.normpath(base_dir)
    return (
        os.path.basename(os.path.dirname(norm_base)) == "profiles"
        or os.path.basename(norm_base).startswith("profile-")
    )


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


def statusline_cache_path(
    state_dir: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the cache path with the same precedence as ``bin/ag``.

    A non-default ``CLAUDE_CONFIG_DIR`` keys the cache under ``profiles/``
    so concurrent profiles never read or overwrite each other's cache. An
    explicit ``state_dir`` argument is used as given.
    """
    base_path = _base_state_path(state_dir)
    if state_dir is None and not _already_profile_scoped(str(base_path)):
        sub = _profile_subdir(config_dir)
        if sub is not None:
            base_path = base_path / sub
    return base_path / CACHE_FILENAME


def root_statusline_cache_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    """The un-scoped cache path; the global Herdr sidebar reads this one."""
    return _base_state_path(state_dir) / CACHE_FILENAME


def _is_finite_number(value: object) -> bool:
    """True for a non-bool int/float that is neither NaN nor infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _normalize_window(value: object, now: int) -> dict | None:
    """Return a normalized live window, or None when absent or invalid.

    The documented status-line shape reports ``used_percentage``; it is
    normalized to ``utilization`` for the cache. NaN and infinity are
    rejected because ``json.loads`` accepts them.
    """
    if not isinstance(value, dict):
        return None
    utilization = value.get("used_percentage")
    if not _is_finite_number(utilization) or not 0 <= utilization <= 100:
        return None
    resets_at = value.get("resets_at")
    if (
        not _is_finite_number(resets_at)
        or resets_at <= 0
        or int(resets_at) <= now
        or resets_at > now + MAX_RESET_AHEAD_SECS
    ):
        return None
    return {"utilization": utilization, "resets_at": int(resets_at)}


def parse_statusline_document(
    document: object, now: int
) -> tuple[dict | None, dict | None]:
    """Extract normalized live windows; return (five_hour, seven_day)."""
    if not isinstance(document, dict):
        raise StatuslineError("status-line payload is not an object")
    rate_limits = document.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None, None
    return (
        _normalize_window(rate_limits.get("five_hour"), now),
        _normalize_window(rate_limits.get("seven_day"), now),
    )


def save_cache(
    path: Path, captured_at: int, five_hour: dict | None, seven_day: dict | None
) -> None:
    """Atomically persist the normalized cache with mode 0600."""
    payload = json.dumps(
        {
            "captured_at": captured_at,
            "five_hour": five_hour,
            "seven_day": seven_day,
        }
    ).encode("utf-8")
    if len(payload) > MAX_CACHE_BYTES:
        raise StatuslineError("normalized cache exceeds 64 KiB")
    directory = path.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(directory), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise



def load_scoped_limit(now: int, state_dir: str | os.PathLike[str] | None = None) -> dict | None:
    """Return the cached model-scoped weekly limit written by claude_max_usage.

    The cache is used only when ``fetched_at`` is within 24 hours and the
    window has not reset. Returns ``{"scope", "utilization", "resets_at"}``.
    """
    path = statusline_cache_path(state_dir).with_name(SCOPED_CACHE_FILENAME)
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    fetched_at = document.get("fetched_at")
    if not _is_finite_number(fetched_at) or not (now - SCOPED_CACHE_MAX_AGE_SECS) <= fetched_at <= now + 60:
        return None
    scope = document.get("scope")
    utilization = document.get("utilization")
    resets_at = document.get("resets_at")
    if not isinstance(scope, str) or not scope:
        return None
    if not _is_finite_number(utilization) or not 0 <= utilization <= 100:
        return None
    if not _is_finite_number(resets_at) or int(resets_at) <= now:
        return None
    return {"scope": scope, "utilization": utilization, "resets_at": int(resets_at)}

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


def _window_segment(
    label: str, window: dict | None, now: int, *, countdown: bool = True
) -> tuple[str, str | None]:
    """One quota window as (text, colour level)."""
    if window is None:
        return f"{label} n/a", None
    utilization = window["utilization"]
    percent = int(utilization) if float(utilization).is_integer() else utilization
    text = f"{label} {percent}%"
    if countdown:
        text += f" {_countdown(window['resets_at'], now)}"
    return text, _level(utilization, QUOTA_WARN_PERCENT, QUOTA_HIGH_PERCENT)


def _model_and_effort(document: object) -> tuple[str, str | None]:
    """Model display name and the session's reasoning effort, if valid."""
    model = "Claude"
    effort = None
    if isinstance(document, dict):
        model_obj = document.get("model")
        if isinstance(model_obj, dict):
            display = model_obj.get("display_name")
            if isinstance(display, str) and display:
                model = display
        elif isinstance(model_obj, str) and model_obj:
            model = model_obj
        effort_obj = document.get("effort")
        if isinstance(effort_obj, dict):
            level = effort_obj.get("level")
            if isinstance(level, str) and re.fullmatch(r"[a-z]{1,8}", level):
                effort = level
    return model, effort


def _context_tokens(document: object) -> float | None:
    """Context depth in tokens, or None before the first response.

    Uses ``context_window.total_input_tokens``: fresh input plus cache reads
    and writes from the most recent response, which is what every further
    call re-reads.
    """
    if not isinstance(document, dict):
        return None
    window = document.get("context_window")
    if not isinstance(window, dict):
        return None
    tokens = window.get("total_input_tokens")
    if not _is_finite_number(tokens) or tokens <= 0:
        return None
    return tokens


def _context_amount(tokens: float) -> str:
    if tokens >= 999_500:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{round(tokens / 1000)}K"


def _account_label(config_dir: str | os.PathLike[str] | None = None) -> str | None:
    """Short label for a non-default ``CLAUDE_CONFIG_DIR``; None for the default.

    ``~/.claude-account6`` reads ``a6``. Any other directory reads as its
    sanitized basename without a leading dot or ``claude-`` prefix.
    """
    sub = _profile_subdir(config_dir)
    if sub is None:
        return None
    raw = (
        os.fspath(config_dir)
        if config_dir is not None
        else os.environ.get("CLAUDE_CONFIG_DIR", "")
    )
    base = os.path.basename(os.path.normpath(os.path.expanduser(raw.strip())))
    name = re.sub(r"[^A-Za-z0-9_-]", "", base.lstrip("."))
    if name.startswith("claude-") and len(name) > len("claude-"):
        name = name[len("claude-") :]
    numbered = re.fullmatch(r"account(\d+)", name)
    if numbered:
        return f"a{numbered.group(1)}"
    return name[:MAX_ACCOUNT_LABEL_CHARS] or None


def _terminal_columns() -> int | None:
    """Terminal width from ``COLUMNS``, which Claude Code sets per render."""
    raw = os.environ.get("COLUMNS", "")
    if not raw.isascii() or not raw.isdigit():
        return None
    columns = int(raw)
    if not MIN_PLAUSIBLE_COLUMNS <= columns <= MAX_PLAUSIBLE_COLUMNS:
        return None
    return columns


def render_status_line(
    document: object,
    five_hour: dict | None,
    seven_day: dict | None,
    now: int,
    *,
    color: bool | None = None,
    columns: int | None = None,
) -> str:
    """Render model, effort, context depth, quota windows and account label.

    ``color`` defaults to on unless the ``NO_COLOR`` environment variable is
    set to a non-empty value. ``columns`` defaults to the ``COLUMNS``
    environment variable; when the line is wider than the terminal, detail
    is shed in ``WIDTH_REDUCTIONS`` order until it fits or nothing is left
    to shed. With no known width the full line is printed.
    """
    if color is None:
        color = not os.environ.get("NO_COLOR")
    if columns is None:
        columns = _terminal_columns()
    scoped = load_scoped_limit(now)
    model, effort = _model_and_effort(document)
    tokens = _context_tokens(document)
    account = _account_label()
    # A scoped weekly limit usually resets with the 7d window; print that
    # shared countdown once, on the scoped segment.
    shared_reset = (
        scoped is not None
        and seven_day is not None
        and abs(scoped["resets_at"] - seven_day["resets_at"]) <= SAME_RESET_TOLERANCE_SECS
    )
    costly_effort = effort in COSTLY_EFFORT_LEVELS

    def compose(shed: frozenset[str]) -> tuple[list[tuple[str, str | None]], str]:
        weekly_countdown = "weekly_countdown" not in shed
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
                    _level(tokens, CONTEXT_WARN_TOKENS, CONTEXT_HIGH_TOKENS),
                )
            )
        segments.append(
            _window_segment(
                "5h", five_hour, now, countdown="five_hour_countdown" not in shed
            )
        )
        segments.append(
            _window_segment(
                "7d", seven_day, now, countdown=weekly_countdown and not shared_reset
            )
        )
        if scoped is not None:
            segments.append(
                _window_segment(scoped["scope"], scoped, now, countdown=weekly_countdown)
            )
        if account is not None and "account" not in shed:
            segments.append((account, None))
        return segments, " " if "bar_separator" in shed else " | "

    def join(segments: list[tuple[str, str | None]], separator: str, paint: bool) -> str:
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
            "usage: claude-statusline [-- downstream-command args...]", file=sys.stderr
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
        five_hour, seven_day = parse_statusline_document(document, now)
        if seven_day is not None:
            profile_path = statusline_cache_path()
            try:
                save_cache(profile_path, now, five_hour, seven_day)
            except OSError:
                # A cache write failure must never suppress an existing
                # status line; warn once and keep displaying.
                print(
                    "claude statusline warning: cannot persist cache",
                    file=sys.stderr,
                )
            root_path = root_statusline_cache_path()
            if root_path != profile_path:
                # Keep the global sidebar current with the latest active
                # profile while each profile's own cache stays isolated.
                try:
                    save_cache(root_path, now, five_hour, seven_day)
                except OSError:
                    print(
                        "claude statusline warning: cannot persist cache",
                        file=sys.stderr,
                    )
        if downstream and run_downstream(downstream, stdin_bytes):
            return 0
        print(render_status_line(document, five_hour, seven_day, now))
        return 0
    except StatuslineError as exc:
        print(f"claude statusline error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

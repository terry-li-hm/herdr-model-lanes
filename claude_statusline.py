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

When arguments follow ``--``, the original stdin bytes are forwarded to that
exact argv without a shell and with a short timeout; on success the
downstream command's stdout is passed through unchanged. Rendering happens
only when no downstream ran or the downstream failed.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ID_PATH = "herdr/plugins/terry.herdr-model-lanes"
CACHE_FILENAME = "claude-statusline.json"
MAX_INPUT_BYTES = 1 << 20
MAX_CACHE_BYTES = 64 * 1024
DOWNSTREAM_TIMEOUT_SECS = 1
MAX_RESET_AHEAD_SECS = 365 * 24 * 60 * 60


class StatuslineError(Exception):
    """Raised on collector failure; messages never echo stdin content."""


def statusline_cache_path(
    state_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the cache path with the same precedence as ``bin/ag``."""
    if state_dir is not None:
        return Path(os.fspath(state_dir)) / CACHE_FILENAME
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.environ.get(
        "MODEL_LANES_STATE_DIR"
    )
    if base:
        return Path(base) / CACHE_FILENAME
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(xdg) / PLUGIN_ID_PATH / CACHE_FILENAME


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


def _window_text(label: str, window: dict | None, now: int) -> str:
    if window is None:
        return f"{label} n/a"
    utilization = window["utilization"]
    percent = int(utilization) if float(utilization).is_integer() else utilization
    return f"{label} {percent}% ({_countdown(window['resets_at'], now)})"


def render_status_line(
    document: object, five_hour: dict | None, seven_day: dict | None, now: int
) -> str:
    """Render a concise model plus 5h/7d status line."""
    model = "Claude"
    if isinstance(document, dict):
        model_obj = document.get("model")
        if isinstance(model_obj, dict):
            display = model_obj.get("display_name")
            if isinstance(display, str) and display:
                model = display
        elif isinstance(model_obj, str) and model_obj:
            model = model_obj
    return " | ".join(
        (
            model,
            _window_text("5h", five_hour, now),
            _window_text("7d", seven_day, now),
        )
    )


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
            try:
                save_cache(statusline_cache_path(), now, five_hour, seven_day)
            except OSError:
                # A cache write failure must never suppress an existing
                # status line; warn once and keep displaying.
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

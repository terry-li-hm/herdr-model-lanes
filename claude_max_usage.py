"""Bundled Claude Max usage helper (experimental).

This module is the sole credential boundary of the plugin. On macOS it reads
the Claude Code OAuth token from the Keychain; on Linux it reads the same
``claudeAiOauth`` payload from ``.credentials.json`` under
``CLAUDE_CONFIG_DIR`` (or ``~/.claude``). It calls Anthropic's undocumented
OAuth usage endpoint and prints only the three normalized quota windows as
JSON. The OAuth token never appears in argv, logs, caches, stdout errors,
exception messages, or files.
"""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import time
import tempfile
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

PLUGIN_VERSION = "2.2.0"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECS = 5
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
CREDENTIALS_FILENAME = ".credentials.json"
MAX_CREDENTIALS_BYTES = 64 * 1024
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (Claude Max usage helper)"
NORMALIZED_WINDOWS = ("five_hour", "seven_day", "seven_day_sonnet")
SCOPED_CACHE_FILENAME = "claude-scoped-limit.json"
SCOPED_REFRESH_SECS = 15 * 60
STATUSLINE_PLUGIN_ID_PATH = "herdr/plugins/terry.herdr-model-lanes"
STATUSLINE_CACHE_FILENAME = "claude-statusline.json"
STATUSLINE_CACHE_MAX_AGE_SECS = 30 * 60
STATUSLINE_CACHE_MAX_BYTES = 64 * 1024
MAX_RESET_AHEAD_SECS = 365 * 24 * 60 * 60


class UsageHelperError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def parse_credential_payload(payload: str, now_ms: int) -> str:
    """Return the live OAuth token stored inside a Keychain JSON payload."""
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise UsageHelperError("Credential payload is not valid JSON") from exc
    if not isinstance(document, dict):
        raise UsageHelperError("Credential payload is not an object")
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise UsageHelperError("Credential payload has no claudeAiOauth object")
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        raise UsageHelperError("Credential payload has no access token")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise UsageHelperError("Credential payload has no expiry timestamp")
    if not math.isfinite(expires_at):
        raise UsageHelperError("Credential payload has an invalid expiry timestamp")
    if expires_at <= now_ms:
        raise UsageHelperError("Claude OAuth token has expired; sign in again")
    return token


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the token is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UsageHelperError(f"usage endpoint refused a redirect (HTTP {code})")


def credentials_path(config_dir: str | os.PathLike[str] | None = None) -> str:
    """Return the Linux credentials file path without touching the file."""
    base = config_dir
    if base is None:
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
    return os.path.join(os.fspath(base), CREDENTIALS_FILENAME)


def read_credentials_file(
    path: str | os.PathLike[str],
    now_ms: int | None = None,
) -> str:
    """Securely read the OAuth token from the Linux credentials file.

    The read is bounded to 64 KiB and refuses symlinks, non-regular files,
    files owned by another effective user, and group/world-readable files.
    Failures never include the file's contents.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UsageHelperError(
            f"cannot open credentials file: {type(exc).__name__}"
        ) from exc
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode):
            raise UsageHelperError("credentials file is a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise UsageHelperError("credentials file is not a regular file")
        if info.st_uid != os.geteuid():
            raise UsageHelperError("credentials file is not owned by this user")
        if info.st_mode & 0o077:
            raise UsageHelperError("credentials file is group/world accessible")
        chunks: list[bytes] = []
        remaining = MAX_CREDENTIALS_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload_bytes = b"".join(chunks)
        if len(payload_bytes) > MAX_CREDENTIALS_BYTES:
            raise UsageHelperError("credentials file exceeds 64 KiB")
    except OSError as exc:
        raise UsageHelperError(
            f"cannot read credentials file: {type(exc).__name__}"
        ) from exc
    finally:
        os.close(fd)
    try:
        payload_text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageHelperError("credentials file is not valid UTF-8") from None
    return parse_credential_payload(payload_text, now_ms)


def read_oauth_token(
    now_ms: int | None = None,
    security_bin: str = "/usr/bin/security",
    config_dir: str | os.PathLike[str] | None = None,
    platform: str | None = None,
) -> str:
    """Read the Claude Code OAuth token on the current platform."""
    if platform is None:
        platform = sys.platform
    if platform == "darwin":
        return _read_keychain_token(now_ms, security_bin)
    if platform == "linux":
        return read_credentials_file(credentials_path(config_dir), now_ms)
    raise UsageHelperError(f"unsupported platform for Claude usage: {platform}")


def _read_keychain_token(
    now_ms: int | None = None,
    security_bin: str = "/usr/bin/security",
) -> str:
    """Read the Claude Code OAuth token from the macOS Keychain."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    try:
        completed = subprocess.run(
            [security_bin, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageHelperError(f"cannot read Keychain: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise UsageHelperError(f"Keychain lookup failed with rc={completed.returncode}")
    return parse_credential_payload(completed.stdout, now_ms)


def _normalize_window(value: object, name: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UsageHelperError(f"usage endpoint returned an invalid {name} window")
    return {
        "utilization": value.get("utilization"),
        "resets_at": value.get("resets_at"),
    }


def _iso_to_epoch(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def extract_scoped_limit(document: object) -> dict | None:
    """Return the model-scoped weekly limit from ``limits``, or None.

    The usage endpoint lists a ``weekly_scoped`` entry when a model carries
    its own weekly cap (the Fable cap, September 2026). The result carries
    only the scope display name, the percent used and the reset epoch.
    """
    if not isinstance(document, dict):
        return None
    limits = document.get("limits")
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        percent = entry.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        scope = entry.get("scope")
        name = None
        if isinstance(scope, dict):
            model = scope.get("model")
            if isinstance(model, dict) and isinstance(model.get("display_name"), str):
                name = model["display_name"]
        resets_at = _iso_to_epoch(entry.get("resets_at"))
        if name and resets_at is not None:
            return {"scope": name, "utilization": float(percent), "resets_at": resets_at}
    return None


def scoped_cache_path() -> str:
    return os.path.join(os.path.dirname(statusline_cache_path()), SCOPED_CACHE_FILENAME)


def save_scoped_limit(scoped: dict | None, fetched_at: int, path: str | None = None) -> None:
    """Atomically persist the scoped limit with mode 0600; None clears it."""
    if path is None:
        path = scoped_cache_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if scoped is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    payload = json.dumps({**scoped, "fetched_at": fetched_at}).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".scoped.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _scoped_cache_age(now: int, path: str | None = None) -> int | None:
    if path is None:
        path = scoped_cache_path()
    try:
        document = json.loads(open(path, "rb").read(MAX_RESPONSE_BYTES).decode("utf-8"))
        fetched_at = document.get("fetched_at")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not _is_finite_number(fetched_at):
        return None
    return now - int(fetched_at)


def fetch_usage(
    token: str,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
    include_scoped: bool = False,
) -> dict:
    """Fetch and normalize OAuth usage; return only the three quota windows."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    if opener is None:
        opener = urllib.request.build_opener(RejectRedirectHandler).open
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            if status != 200:
                raise UsageHelperError(f"usage endpoint returned HTTP {status}")
            body = response.read(MAX_RESPONSE_BYTES)
    except UsageHelperError:
        raise
    except Exception as exc:
        raise UsageHelperError(f"usage request failed: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise UsageHelperError("usage endpoint returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise UsageHelperError("usage endpoint returned a non-object payload")
    windows = {
        window: _normalize_window(document.get(window), window)
        for window in NORMALIZED_WINDOWS
    }
    if include_scoped:
        windows["weekly_scoped"] = extract_scoped_limit(document)
    return windows


def statusline_cache_path() -> str:
    """Resolve the status-line cache path with the same precedence as ``ag``."""
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.environ.get(
        "MODEL_LANES_STATE_DIR"
    )
    if base:
        return os.path.join(base, STATUSLINE_CACHE_FILENAME)
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(xdg, STATUSLINE_PLUGIN_ID_PATH, STATUSLINE_CACHE_FILENAME)


def _read_cache_file(path: str) -> bytes | None:
    """Bounded, symlink-refusing cache read; failures never echo contents."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        chunks: list[bytes] = []
        remaining = STATUSLINE_CACHE_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > STATUSLINE_CACHE_MAX_BYTES:
            return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return payload


def _is_finite_number(value: object) -> bool:
    """True for a non-bool int/float that is neither NaN nor infinity."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _cache_window(value: object, now: int) -> dict | None:
    """Validate one cached window; return the helper shape or None."""
    if not isinstance(value, dict):
        return None
    utilization = value.get("utilization")
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
    return {
        "utilization": utilization,
        "resets_at": datetime.fromtimestamp(int(resets_at), UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }


def load_statusline_usage(now: int | None = None) -> dict | None:
    """Return the cached official windows, or None when unusable.

    The cache is used only when ``captured_at`` is no more than 30 minutes
    old and the ``seven_day`` window is still live; an expired ``five_hour``
    window is dropped independently. Output matches the helper shape with
    ``seven_day_sonnet`` absent.
    """
    if now is None:
        now = int(time.time())
    payload = _read_cache_file(statusline_cache_path())
    if payload is None:
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    captured_at = document.get("captured_at")
    if not _is_finite_number(captured_at) or captured_at <= 0:
        return None
    age = now - int(captured_at)
    if age > STATUSLINE_CACHE_MAX_AGE_SECS or age < -60:
        return None
    seven_day = _cache_window(document.get("seven_day"), now)
    if seven_day is None:
        return None
    return {
        "five_hour": _cache_window(document.get("five_hour"), now),
        "seven_day": seven_day,
    }


def main(argv: list[str] | None = None) -> int:
    """Print normalized quota windows, or a credential-free error.

    The official status-line cache is checked first, before any credential
    or network access; ``--refresh`` does not bypass a valid cache because
    the point is avoiding endpoint calls. ``--scoped`` is the one mode that
    always calls the endpoint: it records the model-scoped weekly limit
    (the Fable cap) for the status line and prints only that window.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    for arg in args:
        if arg not in ("--refresh", "--scoped"):
            print(f"usage: {sys.argv[0]} [--refresh] [--scoped]", file=sys.stderr)
            return 2
    if "--scoped" in args:
        now = int(time.time())
        try:
            windows = fetch_usage(
                read_oauth_token(), timeout=FETCH_TIMEOUT_SECS, include_scoped=True
            )
            save_scoped_limit(windows.get("weekly_scoped"), now)
        except (UsageHelperError, OSError) as exc:
            print(f"claude usage error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(windows.get("weekly_scoped")))
        return 0
    try:
        cached = load_statusline_usage()
        if cached is not None:
            print(json.dumps(cached))
            return 0
        windows = fetch_usage(read_oauth_token(), timeout=FETCH_TIMEOUT_SECS)
    except UsageHelperError as exc:
        print(f"claude usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(windows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

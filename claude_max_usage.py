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
import os
import stat
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable

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


def fetch_usage(
    token: str,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
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
    return {
        window: _normalize_window(document.get(window), window)
        for window in NORMALIZED_WINDOWS
    }


def main() -> int:
    """Print the normalized quota windows, or a credential-free error."""
    try:
        windows = fetch_usage(read_oauth_token(), timeout=FETCH_TIMEOUT_SECS)
    except UsageHelperError as exc:
        print(f"claude usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(windows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

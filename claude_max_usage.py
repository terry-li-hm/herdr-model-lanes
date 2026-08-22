"""Bundled Claude Max usage helper (experimental, macOS-only).

This module is the sole credential boundary of the plugin. It reads the
Claude Code OAuth token from the macOS Keychain, calls Anthropic's
undocumented OAuth usage endpoint, and prints only the three normalized
quota windows as JSON. The OAuth token never appears in argv, logs,
caches, stdout errors, exception messages, or files.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable

PLUGIN_VERSION = "2.1.0"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECS = 5
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (Claude Max usage helper)"
NORMALIZED_WINDOWS = ("five_hour", "seven_day", "seven_day_sonnet")


class UsageHelperError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def parse_keychain_payload(payload: str, now_ms: int) -> str:
    """Return the live OAuth token stored inside a Keychain JSON payload."""
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise UsageHelperError("Keychain payload is not valid JSON") from exc
    if not isinstance(document, dict):
        raise UsageHelperError("Keychain payload is not an object")
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise UsageHelperError("Keychain payload has no claudeAiOauth object")
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        raise UsageHelperError("Keychain payload has no access token")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        raise UsageHelperError("Keychain payload has no expiry timestamp")
    if expires_at <= now_ms:
        raise UsageHelperError("Claude OAuth token has expired; sign in again")
    return token


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the token is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UsageHelperError(f"usage endpoint refused a redirect (HTTP {code})")


def read_oauth_token(
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
    return parse_keychain_payload(completed.stdout, now_ms)


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

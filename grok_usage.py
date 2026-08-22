"""Bundled Grok usage helper.

This module is the sole credential boundary for Grok quota. It reads the
login key from ``~/.grok/auth.json`` (the top-level keys are auth-host
strings; it takes the ``key`` field of the first entry), calls Grok's
billing endpoint, and prints only the normalized weekly quota window as
JSON. The key never appears in argv, logs, caches, stdout errors,
exception messages, or files.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

PLUGIN_VERSION = "2.2.0"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
AUTH_FILENAME = Path.home() / ".grok" / "auth.json"
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-quota/{PLUGIN_VERSION} (Grok usage helper)"
WEEKLY_PERIOD_TYPE = "USAGE_PERIOD_TYPE_WEEKLY"


class GrokUsageError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def parse_auth_payload(payload: str) -> str:
    """Return the login key stored inside the nested ``auth.json`` shape."""
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise GrokUsageError("auth payload is not valid JSON") from exc
    if not isinstance(document, dict) or not document:
        raise GrokUsageError("auth payload has no auth-host entries")
    for entry in document.values():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key:
            return key
    raise GrokUsageError("auth payload has no login key")


def read_login_key(auth_path: Path | None = None) -> str:
    """Read the Grok login key from ``~/.grok/auth.json``."""
    path = auth_path if auth_path is not None else AUTH_FILENAME
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GrokUsageError(f"cannot read auth file: {type(exc).__name__}") from exc
    return parse_auth_payload(payload)


def _parse_resets_at(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise GrokUsageError("billing response missing period end timestamp")
    try:
        time.strptime(value[: len("2026-08-22T00:00:00")], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise GrokUsageError("billing period end is not a timestamp") from exc
    return value


def parse_billing(payload: dict) -> dict:
    """Normalize the billing response to a single weekly quota window."""
    if not isinstance(payload, dict):
        raise GrokUsageError("billing response is not an object")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise GrokUsageError("billing response has no config object")
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        raise GrokUsageError("billing response has no current period")
    if period.get("type") != WEEKLY_PERIOD_TYPE:
        raise GrokUsageError(
            "billing period is not weekly; refusing to show it as a weekly window"
        )
    used = config.get("creditUsagePercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        raise GrokUsageError("billing response has no credit usage percentage")
    if not 0 <= used <= 100:
        raise GrokUsageError("credit usage percentage out of range")
    return {
        "weekly": {
            "used_percent": round(used),
            "remaining_percent": round(100 - used),
            "resets_at": _parse_resets_at(period.get("end")),
        }
    }


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the key is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GrokUsageError(f"billing endpoint refused a redirect (HTTP {code})")


def fetch_credits(
    key: str,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    """Fetch and normalize the weekly credits window."""
    request = urllib.request.Request(
        BILLING_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    if opener is None:
        opener = urllib.request.build_opener(RejectRedirectHandler).open
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            if status != 200:
                raise GrokUsageError(f"billing endpoint returned HTTP {status}")
            body = response.read(MAX_RESPONSE_BYTES)
    except GrokUsageError:
        raise
    except Exception as exc:
        raise GrokUsageError(f"billing request failed: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise GrokUsageError("billing endpoint returned invalid JSON") from exc
    return parse_billing(document)


def main(auth_path: Path | None = None) -> int:
    """Print the normalized weekly window, or a credential-free error."""
    try:
        window = fetch_credits(read_login_key(auth_path), timeout=FETCH_TIMEOUT_SECS)
    except GrokUsageError as exc:
        print(f"grok usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

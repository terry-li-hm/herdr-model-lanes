"""Bundled Cursor usage helper.

This module is the sole credential boundary for Cursor quota. It reads the
Cursor.app access token from the local VS Code-style ``state.vscdb``
(``cursorAuth/accessToken``), sends it only as a loopback-safe
``WorkosCursorSessionToken`` cookie to ``GET https://cursor.com/api/usage-summary``,
and prints only the normalized monthly plan window as JSON.

The token never appears in argv, logs, caches, stdout errors, exception
messages, or files. The helper never reads or uses the refresh token.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.error import URLError

PLUGIN_VERSION = "3.7.0"
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (Cursor usage helper)"
USAGE_URL = "https://cursor.com/api/usage-summary"
TOKEN_KEY = "cursorAuth/accessToken"
JWT_MIN_REMAINING_SECS = 60
STATE_DB_CANDIDATES = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
    Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb",
)


class CursorUsageError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def _b64url_json(segment: str) -> dict:
    padded = segment + ("=" * ((4 - len(segment) % 4) % 4))
    try:
        document = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CursorUsageError("access token is not a JWT") from exc
    if not isinstance(document, dict):
        raise CursorUsageError("access token payload is not an object")
    return document


def jwt_expiry(token: str) -> int:
    """Return the JWT ``exp`` unix timestamp. Never logs claims."""
    parts = token.split(".")
    if len(parts) < 2:
        raise CursorUsageError("access token is not a JWT")
    payload = _b64url_json(parts[1])
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise CursorUsageError("access token has no expiry")
    return int(exp)


def _usable_token(value: object, now: int) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    token = value.strip()
    try:
        if jwt_expiry(token) <= now + JWT_MIN_REMAINING_SECS:
            return None
    except CursorUsageError:
        return None
    return token


def _read_token_from_db(path: Path, now: int) -> str | None:
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (TOKEN_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    return _usable_token(row[0], now)


def find_state_db(candidates: tuple[Path, ...] | None = None) -> Path | None:
    for path in candidates if candidates is not None else STATE_DB_CANDIDATES:
        if path.is_file():
            return path
    return None


def resolve_token(
    now: int | None = None,
    db_path: Path | None = None,
    candidates: tuple[Path, ...] | None = None,
) -> str:
    if now is None:
        now = int(time.time())
    path = db_path if db_path is not None else find_state_db(candidates)
    if path is None:
        raise CursorUsageError("Cursor state database not found")
    token = _read_token_from_db(path, now)
    if token is None:
        raise CursorUsageError("no fresh Cursor access token")
    return token


def key_available(
    now: int | None = None,
    db_path: Path | None = None,
    candidates: tuple[Path, ...] | None = None,
) -> bool:
    try:
        resolve_token(now=now, db_path=db_path, candidates=candidates)
    except CursorUsageError:
        return False
    return True


def _parse_iso(value: object, field: str) -> int:
    if not isinstance(value, str) or not value:
        raise CursorUsageError(f"{field} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CursorUsageError(f"{field} is not a timestamp") from exc
    return int(parsed.timestamp())


def parse_usage_summary(payload: dict) -> dict:
    """Normalize usage-summary to a single monthly plan window."""
    if not isinstance(payload, dict):
        raise CursorUsageError("usage summary is not an object")
    start = _parse_iso(payload.get("billingCycleStart"), "billingCycleStart")
    end = _parse_iso(payload.get("billingCycleEnd"), "billingCycleEnd")
    if end <= start:
        raise CursorUsageError("billing cycle end is not after start")
    if payload.get("isUnlimited") is True:
        remaining_percent = 100
        used_percent = 0
    else:
        usage = payload.get("individualUsage")
        if not isinstance(usage, dict):
            raise CursorUsageError("usage summary has no individualUsage object")
        plan = usage.get("plan")
        if not isinstance(plan, dict) or plan.get("enabled") is not True:
            raise CursorUsageError("usage summary has no enabled plan")
        percent_used = plan.get("totalPercentUsed")
        if isinstance(percent_used, (int, float)) and not isinstance(percent_used, bool):
            if not 0 <= percent_used <= 100:
                raise CursorUsageError("totalPercentUsed out of range")
            used_percent = max(0, min(100, round(percent_used)))
            remaining_percent = 100 - used_percent
        else:
            raise CursorUsageError("plan has no totalPercentUsed")
    return {
        "monthly": {
            "used_percent": used_percent,
            "remaining_percent": remaining_percent,
            "resets_at": payload["billingCycleEnd"]
            if isinstance(payload.get("billingCycleEnd"), str)
            else None,
            "window_seconds": end - start,
        }
    }


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the session cookie is never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CursorUsageError(f"usage endpoint refused a redirect (HTTP {code})")


def fetch_usage(
    token: str,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Cookie": f"WorkosCursorSessionToken=::{token}",
        },
        method="GET",
    )
    if opener is None:
        opener = urllib.request.build_opener(RejectRedirectHandler).open
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            if status != 200:
                raise CursorUsageError(f"usage endpoint returned HTTP {status}")
            body = response.read(MAX_RESPONSE_BYTES)
    except CursorUsageError:
        raise
    except (OSError, TimeoutError, URLError) as exc:
        raise CursorUsageError(f"usage request failed: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise CursorUsageError("usage endpoint returned invalid JSON") from exc
    return parse_usage_summary(document)


def main() -> int:
    try:
        window = fetch_usage(resolve_token())
    except CursorUsageError as exc:
        print(f"cursor usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

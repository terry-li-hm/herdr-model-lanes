"""Bundled Kimi Code usage helper.

This module is the sole credential boundary for Kimi Code quota. It looks
up the key from ``KIMI_CODE_API_KEY`` in the process environment, then
``~/.env.resolved``, then a still-fresh access token in
``~/.kimi-code/credentials/kimi-code.json``. It calls
``GET https://api.kimi.com/coding/v1/usages`` and prints only the tightest
coding window as JSON. Keys that start with ``$`` or ``!`` are skipped.
Moonshot Open Platform keys are not Kimi Code credentials and are ignored.
The token never appears in argv, logs, caches, stdout errors, exception
messages, or files. The helper never reads or uses the CLI refresh token.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.error import URLError

PLUGIN_VERSION = "3.5.0"
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (Kimi Code usage helper)"
USAGES_URL = "https://api.kimi.com/coding/v1/usages"
ENV_KEY_NAME = "KIMI_CODE_API_KEY"
UNUSABLE_KEY_PREFIXES = ("$", "!")
ENV_RESOLVED_FILENAME = Path.home() / ".env.resolved"
KIMI_CODE_HOME = Path.home() / ".kimi-code"
CREDENTIALS_FILENAME = KIMI_CODE_HOME / "credentials" / "kimi-code.json"
WEEKLY_WINDOW_SECONDS = 604_800
FIVE_HOUR_WINDOW_SECONDS = 5 * 3_600
FIVE_HOUR_MINUTES = 300


class KimiUsageError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def _usable_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key or key.startswith(UNUSABLE_KEY_PREFIXES):
        return None
    return key


def _read_env_file_value(path: Path, name: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    prefix = f"{name}="
    export_prefix = f"export {name}="
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(export_prefix):
            value = line[len(export_prefix) :]
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return _usable_key(value)
    return None


def _read_cli_access_token(path: Path, now: int) -> str | None:
    """Return a still-fresh CLI access token, or None. Never the refresh token."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    token = document.get("access_token")
    expires_at = document.get("expires_at")
    if not isinstance(token, str) or not token.strip():
        return None
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    if int(expires_at) <= now:
        return None
    return token.strip()


def _find_key(
    env: Callable[[str], str] | None = None,
    now: int | None = None,
    env_resolved: Path | None = None,
    credentials_path: Path | None = None,
) -> str | None:
    getenv = env if env is not None else os.environ.get
    value = _usable_key(getenv(ENV_KEY_NAME))
    if value:
        return value
    resolved = env_resolved if env_resolved is not None else ENV_RESOLVED_FILENAME
    value = _read_env_file_value(resolved, ENV_KEY_NAME)
    if value:
        return value
    if now is None:
        now = int(time.time())
    creds = credentials_path if credentials_path is not None else CREDENTIALS_FILENAME
    return _read_cli_access_token(creds, now)


def resolve_key(
    env: Callable[[str], str] | None = None,
    now: int | None = None,
    env_resolved: Path | None = None,
    credentials_path: Path | None = None,
) -> str:
    key = _find_key(
        env=env,
        now=now,
        env_resolved=env_resolved,
        credentials_path=credentials_path,
    )
    if key is None:
        raise KimiUsageError("no Kimi Code API key or fresh CLI token")
    return key


def key_available(
    env: Callable[[str], str] | None = None,
    now: int | None = None,
    env_resolved: Path | None = None,
    credentials_path: Path | None = None,
) -> bool:
    try:
        resolve_key(
            env=env,
            now=now,
            env_resolved=env_resolved,
            credentials_path=credentials_path,
        )
    except KimiUsageError:
        return False
    return True


def _intish(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _parse_reset(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise KimiUsageError("usage window missing reset timestamp")
    try:
        time_part = value[: len("2026-08-22T00:00:00")]
        time.strptime(time_part, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise KimiUsageError("usage reset is not a timestamp") from exc
    return value


def _window_from_detail(detail: dict, window_seconds: int) -> dict:
    limit = _intish(detail.get("limit"))
    remaining = _intish(detail.get("remaining"))
    used = _intish(detail.get("used"))
    if remaining is None and used is not None and limit is not None:
        remaining = limit - used
    if limit is None or remaining is None or limit <= 0:
        raise KimiUsageError("usage window has no usable limit and remaining")
    remaining = max(0, min(limit, remaining))
    remaining_percent = round(100 * remaining / limit)
    return {
        "used_percent": 100 - remaining_percent,
        "remaining_percent": remaining_percent,
        "resets_at": _parse_reset(detail.get("resetTime")),
        "window_seconds": window_seconds,
        "remaining_fraction": remaining / limit,
    }


def _five_hour_detail(limits: object) -> dict | None:
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        window = entry.get("window")
        detail = entry.get("detail")
        if not isinstance(window, dict) or not isinstance(detail, dict):
            continue
        duration = _intish(window.get("duration"))
        unit = window.get("timeUnit")
        if duration == FIVE_HOUR_MINUTES and unit == "TIME_UNIT_MINUTE":
            return detail
    return None


def parse_usages(payload: dict) -> dict:
    """Normalize a Kimi Code usages payload to the tightest coding window."""
    if not isinstance(payload, dict):
        raise KimiUsageError("usages response is not an object")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise KimiUsageError("usages response has no usage object")
    candidates = [_window_from_detail(usage, WEEKLY_WINDOW_SECONDS)]
    five_hour = _five_hour_detail(payload.get("limits"))
    if five_hour is not None:
        candidates.append(_window_from_detail(five_hour, FIVE_HOUR_WINDOW_SECONDS))
    picked = min(
        candidates,
        key=lambda item: (item["remaining_fraction"], -item["window_seconds"]),
    )
    return {
        "coding": {
            "used_percent": picked["used_percent"],
            "remaining_percent": picked["remaining_percent"],
            "resets_at": picked["resets_at"],
            "window_seconds": picked["window_seconds"],
        }
    }


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the key is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise KimiUsageError(f"usages endpoint refused a redirect (HTTP {code})")


def fetch_usages(
    key: str,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    request = urllib.request.Request(
        USAGES_URL,
        headers={
            "Authorization": f"Bearer {key}",
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
                raise KimiUsageError(f"usages endpoint returned HTTP {status}")
            body = response.read(MAX_RESPONSE_BYTES)
    except KimiUsageError:
        raise
    except (OSError, TimeoutError, URLError) as exc:
        raise KimiUsageError(f"usages request failed: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise KimiUsageError("usages endpoint returned invalid JSON") from exc
    return parse_usages(document)


def main() -> int:
    try:
        window = fetch_usages(resolve_key())
    except KimiUsageError as exc:
        print(f"kimi usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

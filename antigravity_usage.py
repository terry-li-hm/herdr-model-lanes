"""Bundled Antigravity (agy) usage helper.

This module is the sole credential boundary for Antigravity quota. It
discovers a same-user Antigravity.app language_server (or a running ``agy``
CLI) from process listings, reads the CSRF token from that process's argv
when present, POSTs to the loopback Connect RPC, and prints only the
normalized Gemini quota window as JSON.

The CSRF token never appears in argv constructed by this helper, logs,
caches, stdout errors, exception messages, or files. This helper does not
spawn ``agy`` or the desktop app; if neither is already running, quota is
unavailable. Loopback TLS verification is skipped only for 127.0.0.1.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlparse

PLUGIN_VERSION = "3.4.0"
FETCH_TIMEOUT_SECS = 5
PS_TIMEOUT_SECS = 3
LSOF_TIMEOUT_SECS = 3
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (Antigravity usage helper)"
CONNECT_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
LOOPBACK_HOST = "127.0.0.1"
WEEKLY_WINDOW_SECONDS = 604_800
FIVE_HOUR_WINDOW_SECONDS = 5 * 3_600
REQUEST_BODY = json.dumps(
    {
        "metadata": {
            "ideName": "antigravity",
            "extensionName": "antigravity",
            "locale": "en",
            "ideVersion": "unknown",
        }
    }
).encode()


class AntigravityUsageError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


@dataclass(frozen=True)
class LocalServer:
    pid: int
    port: int
    csrf_token: str | None
    kind: str


def _is_gemini_group(display_name: object) -> bool:
    return isinstance(display_name, str) and "gemini" in display_name.lower()


def _remaining_fraction(bucket: dict) -> float | None:
    remaining = bucket.get("remaining")
    if isinstance(remaining, dict):
        value = remaining.get("remainingFraction")
    else:
        value = bucket.get("remainingFraction")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not 0 <= value <= 1:
        return None
    return float(value)


def _window_seconds(bucket: dict) -> int:
    window = bucket.get("window")
    bucket_id = bucket.get("bucketId")
    parts = []
    if isinstance(window, str):
        parts.append(window.lower())
    if isinstance(bucket_id, str):
        parts.append(bucket_id.lower())
    labels = " ".join(parts)
    if any(marker in labels for marker in ("five_hour", "five-hour", "5h", "session")):
        return FIVE_HOUR_WINDOW_SECONDS
    return WEEKLY_WINDOW_SECONDS


def _parse_reset(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AntigravityUsageError("quota summary missing reset timestamp")
    try:
        time_part = value[: len("2026-08-22T00:00:00")]
        time.strptime(time_part, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise AntigravityUsageError("quota reset is not a timestamp") from exc
    return value


def parse_quota_summary(payload: dict) -> dict:
    """Normalize RetrieveUserQuotaSummary to the tightest Gemini window."""
    if not isinstance(payload, dict):
        raise AntigravityUsageError("quota summary is not an object")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise AntigravityUsageError("quota summary has no response object")
    groups = response.get("groups")
    if not isinstance(groups, list):
        raise AntigravityUsageError("quota summary has no groups list")

    candidates: list[tuple[float, int, str]] = []
    for group in groups:
        if not isinstance(group, dict) or not _is_gemini_group(group.get("displayName")):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            fraction = _remaining_fraction(bucket)
            if fraction is None:
                continue
            reset = _parse_reset(bucket.get("resetTime"))
            candidates.append((fraction, _window_seconds(bucket), reset))
    if not candidates:
        raise AntigravityUsageError("quota summary has no Gemini remaining fraction")

    fraction, window_seconds, reset = min(candidates, key=lambda item: (item[0], -item[1]))
    remaining_percent = round(100 * fraction)
    return {
        "gemini": {
            "used_percent": 100 - remaining_percent,
            "remaining_percent": remaining_percent,
            "resets_at": reset,
            "window_seconds": window_seconds,
        }
    }


def _parse_csrf(command: str) -> str | None:
    match = re.search(r"--csrf_token(?:=|\s+)(\S+)", command)
    if match is None:
        return None
    token = match.group(1).strip()
    return token or None


def parse_language_server_line(line: str) -> tuple[int, str | None, str] | None:
    """Return ``(pid, csrf, kind)`` for an Antigravity app or agy CLI line."""
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 1)
    if len(parts) != 2:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    command = parts[1]
    lowered = command.lower()
    if "antigravity-ide" in lowered or "--app_data_dir antigravity-ide" in lowered:
        return None
    if "language_server" in lowered and "--app_data_dir antigravity" in lowered:
        return pid, _parse_csrf(command), "app"
    path = command.split(None, 1)[0]
    name = path.rsplit("/", 1)[-1]
    if name == "agy":
        return pid, None, "cli"
    return None


def parse_lsof_ports(payload: str) -> list[int]:
    """Extract loopback listen ports from ``lsof -nP -iTCP -sTCP:LISTEN`` output."""
    ports: list[int] = []
    for line in payload.splitlines():
        match = re.search(rf"{re.escape(LOOPBACK_HOST)}:(\d+)", line)
        if match:
            ports.append(int(match.group(1)))
    return ports


def _run(command: list[str], timeout: int) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AntigravityUsageError(f"{command[0]} failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise AntigravityUsageError(f"{command[0]} failed with rc={completed.returncode}")
    return completed.stdout


def discover_servers(
    ps_output: str | None = None,
    lsof_for_pid: Callable[[int], str] | None = None,
) -> list[LocalServer]:
    """Find same-user Antigravity loopback quota servers. App before CLI."""
    if ps_output is None:
        ps_output = _run(["ps", "-ax", "-o", "pid=,command="], PS_TIMEOUT_SECS)
    found: list[LocalServer] = []
    for line in ps_output.splitlines():
        parsed = parse_language_server_line(line)
        if parsed is None:
            continue
        pid, csrf, kind = parsed
        if kind == "app" and not csrf:
            continue
        if lsof_for_pid is None:
            lsof_payload = _run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                LSOF_TIMEOUT_SECS,
            )
        else:
            lsof_payload = lsof_for_pid(pid)
        for port in parse_lsof_ports(lsof_payload):
            found.append(LocalServer(pid, port, csrf, kind))
    found.sort(key=lambda server: (0 if server.kind == "app" else 1, server.pid, server.port))
    return found


def probe_available(
    ps_output: str | None = None,
    lsof_for_pid: Callable[[int], str] | None = None,
) -> bool:
    """True when a local Antigravity quota server is already running."""
    try:
        return bool(discover_servers(ps_output=ps_output, lsof_for_pid=lsof_for_pid))
    except AntigravityUsageError:
        return False


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the CSRF token is never forwarded off loopback."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AntigravityUsageError(f"quota endpoint refused a redirect (HTTP {code})")


def _loopback_url(port: int, scheme: str) -> str:
    return f"{scheme}://{LOOPBACK_HOST}:{port}{CONNECT_PATH}"


def _assert_loopback(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname != LOOPBACK_HOST:
        raise AntigravityUsageError("refusing non-loopback quota host")


def _open(request: urllib.request.Request, timeout: int):
    _assert_loopback(request.full_url)
    context = None
    if urlparse(request.full_url).scheme == "https":
        context = ssl._create_unverified_context()
    opener = urllib.request.build_opener(
        RejectRedirectHandler,
        urllib.request.HTTPSHandler(context=context) if context else urllib.request.HTTPHandler(),
    )
    return opener.open(request, timeout=timeout)


def fetch_quota(
    server: LocalServer,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    """POST RetrieveUserQuotaSummary to one loopback server."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
    }
    if server.csrf_token:
        headers["X-Codeium-Csrf-Token"] = server.csrf_token
    last_error: AntigravityUsageError | None = None
    for scheme in ("https", "http"):
        request = urllib.request.Request(
            _loopback_url(server.port, scheme),
            data=REQUEST_BODY,
            headers=headers,
            method="POST",
        )
        try:
            open_fn = opener if opener is not None else _open
            with open_fn(request, timeout) as response:
                status = getattr(response, "status", 200) or 200
                if status != 200:
                    last_error = AntigravityUsageError(f"quota endpoint returned HTTP {status}")
                    continue
                body = response.read(MAX_RESPONSE_BYTES)
        except AntigravityUsageError as exc:
            last_error = exc
            continue
        except (OSError, TimeoutError, URLError) as exc:
            last_error = AntigravityUsageError(
                f"quota request failed: {type(exc).__name__}"
            )
            continue
        try:
            document = json.loads(body)
        except ValueError as exc:
            raise AntigravityUsageError("quota endpoint returned invalid JSON") from exc
        return parse_quota_summary(document)
    raise last_error or AntigravityUsageError("quota endpoint unreachable")


def fetch_from_local(
    servers: Sequence[LocalServer] | None = None,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    """Return the first parseable Gemini window from discovered servers."""
    discovered = list(servers) if servers is not None else discover_servers()
    if not discovered:
        raise AntigravityUsageError("no local Antigravity quota server is running")
    last_error: AntigravityUsageError | None = None
    for server in discovered:
        try:
            return fetch_quota(server, opener=opener)
        except AntigravityUsageError as exc:
            last_error = exc
            continue
    raise last_error or AntigravityUsageError("no local Antigravity quota server is running")


def main() -> int:
    """Print the normalized Gemini window, or a credential-free error."""
    try:
        window = fetch_from_local()
    except AntigravityUsageError as exc:
        message = str(exc)
        print(f"antigravity usage error: {message}", file=sys.stderr)
        return 1
    print(json.dumps(window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

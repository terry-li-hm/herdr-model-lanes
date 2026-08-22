"""Bundled GLM (Z.ai / bigmodel coding plan) usage helper.

This module is the sole credential boundary for GLM quota. It looks up the
API key from the environment, Pi's ``models.json``, or the Zhipu/BigModel
config files, calls the Z.ai/BigModel quota endpoint, and prints only the
normalized five-hour token window as JSON. The key never appears in argv,
logs, caches, stdout errors, exception messages, or files.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

PLUGIN_VERSION = "3.2.0"
FETCH_TIMEOUT_SECS = 5
MAX_RESPONSE_BYTES = 1 << 20
USER_AGENT = f"herdr-model-lanes/{PLUGIN_VERSION} (GLM usage helper)"
BIGMODEL_BASE = "https://open.bigmodel.cn"
ZAI_BASE = "https://api.z.ai"
QUOTA_PATH = "/api/monitor/usage/quota/limit"
TOKENS_LIMIT = "TOKENS_LIMIT"
ENV_KEY_NAMES = (
    "ZHIPU_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZAI_API_KEY",
    "ZAI_KEY",
    "BIGMODEL_API_KEY",
    "GLM_API_KEY",
)
BIGMODEL_ENV_NAMES = frozenset({"ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"})
PI_MODELS_FILENAME = Path.home() / ".pi" / "agent" / "models.json"
CONFIG_KEY_FILES = (
    (Path.home() / ".config" / "zhipu" / "api_key", True),
    (Path.home() / ".config" / "bigmodel" / "api_key", True),
)


class GlmUsageError(Exception):
    """Raised when usage cannot be obtained without exposing credentials."""


def _find_key(env: Callable[[str], str] | None = None) -> tuple[str, str] | None:
    """Return ``(key, base)`` from the first source that has one."""
    getenv = env if env is not None else os.environ.get
    for name in ENV_KEY_NAMES:
        value = getenv(name)
        if value:
            base = BIGMODEL_BASE if name in BIGMODEL_ENV_NAMES else ZAI_BASE
            return value, base
    try:
        document = json.loads(PI_MODELS_FILENAME.read_text(encoding="utf-8"))
        api_key = (
            (document.get("providers") or {}).get("bigmodel-coding", {}).get("apiKey")
        )
    except (OSError, ValueError):
        api_key = None
    if isinstance(api_key, str) and api_key and not api_key.startswith("$"):
        return api_key, BIGMODEL_BASE
    for path, _is_bigmodel in CONFIG_KEY_FILES:
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if first_line:
            return first_line, BIGMODEL_BASE
    return None


def resolve_key(env: Callable[[str], str] | None = None) -> tuple[str, str]:
    """Resolve the API key and base URL, honouring ``MODEL_LANES_GLM_BASE``."""
    found = _find_key(env)
    if found is None:
        raise GlmUsageError("no GLM API key found")
    key, base = found
    override = (
        env("MODEL_LANES_GLM_BASE")
        if env is not None
        else os.environ.get("MODEL_LANES_GLM_BASE")
    )
    return key, (override or base).rstrip("/")


def key_available(env: Callable[[str], str] | None = None) -> bool:
    """Report whether a key exists, without returning it."""
    try:
        resolve_key(env)
    except GlmUsageError:
        return False
    return True


def _pick_tokens_limit(limits: list) -> dict:
    """Return the TOKENS_LIMIT entry with the nearest nextResetTime."""
    tokens = [
        entry
        for entry in limits
        if isinstance(entry, dict) and entry.get("type") == TOKENS_LIMIT
    ]
    if not tokens:
        raise GlmUsageError("quota response has no TOKENS_LIMIT entry")
    try:
        return min(tokens, key=lambda entry: entry["nextResetTime"])
    except (KeyError, TypeError) as exc:
        raise GlmUsageError("TOKENS_LIMIT entry has no numeric nextResetTime") from exc


def parse_quota(payload: dict) -> dict:
    """Normalize the quota response to a single five-hour token window."""
    if not isinstance(payload, dict):
        raise GlmUsageError("quota response is not an object")
    if payload.get("success") is not True:
        raise GlmUsageError("quota response reports success false")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GlmUsageError("quota response has no data object")
    limits = data.get("limits")
    if not isinstance(limits, list):
        raise GlmUsageError("quota response has no limits list")
    entry = _pick_tokens_limit(limits)

    percentage = entry.get("percentage")
    if isinstance(percentage, (int, float)) and not isinstance(percentage, bool):
        used = percentage
    else:
        usage = entry.get("usage")
        remaining = entry.get("remaining")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (usage, remaining)
        ):
            raise GlmUsageError("TOKENS_LIMIT entry has no usable usage figures")
        total = usage + remaining
        if total <= 0:
            raise GlmUsageError("TOKENS_LIMIT usage and remaining sum to zero")
        used = 100 * usage / total
    if not 0 <= used <= 100:
        raise GlmUsageError("TOKENS_LIMIT usage percentage out of range")

    next_reset = entry.get("nextResetTime")
    if not isinstance(next_reset, (int, float)) or isinstance(next_reset, bool):
        raise GlmUsageError("TOKENS_LIMIT entry has no nextResetTime")
    used_percent = round(used)
    return {
        "five_hour": {
            "used_percent": used_percent,
            "remaining_percent": 100 - used_percent,
            "resets_at": int(next_reset / 1000),
        }
    }


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the key is never forwarded to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GlmUsageError(f"quota endpoint refused a redirect (HTTP {code})")


def fetch_quota(
    key: str,
    base: str = BIGMODEL_BASE,
    timeout: int = FETCH_TIMEOUT_SECS,
    opener: Callable[[urllib.request.Request, int], object] | None = None,
) -> dict:
    """Fetch and normalize the five-hour token window."""
    request = urllib.request.Request(
        f"{base}{QUOTA_PATH}",
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
                raise GlmUsageError(f"quota endpoint returned HTTP {status}")
            body = response.read(MAX_RESPONSE_BYTES)
    except GlmUsageError:
        raise
    except Exception as exc:
        raise GlmUsageError(f"quota request failed: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise GlmUsageError("quota endpoint returned invalid JSON") from exc
    return parse_quota(document)


def main() -> int:
    """Print the normalized five-hour window, or a credential-free error."""
    try:
        key, base = resolve_key()
        window = fetch_quota(key, base=base, timeout=FETCH_TIMEOUT_SECS)
    except GlmUsageError as exc:
        print(f"glm usage error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only CLIProxyAPI capacity adapter. See THIRD_PARTY_NOTICES.md.

Only normalized capacity leaves this module. Never print HTTP errors, account
metadata, response bodies or credentials. The proxy remains the refresh owner.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import holds as holdlib

URLS = {
    "claude": "https://api.anthropic.com/api/oauth/usage",
    "codex": "https://chatgpt.com/backend-api/wham/usage",
}
MAX_BYTES = 2 * 1024 * 1024


class CapacityError(Exception):
    """A fixed, credential-free state code."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CapacityError("unavailable")


def epoch(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise CapacityError("unknown")
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            result = datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError, AttributeError):
            raise CapacityError("unknown") from None
    if not math.isfinite(result) or result <= 0:
        raise CapacityError("unknown")
    return result


def result(state, reset=None, remaining=None):
    return {"state": state, "reset_at": reset, "remaining_percent": remaining}


def window(value, used_key, now):
    if not isinstance(value, dict):
        raise CapacityError("unknown")
    used = value.get(used_key)
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise CapacityError("unknown")
    if not math.isfinite(used) or used < 0:
        raise CapacityError("unknown")
    reset = epoch(value.get("resets_at", value.get("reset_at")))
    if reset is None and value.get("reset_after_seconds") is not None:
        seconds = value["reset_after_seconds"]
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (float, int))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise CapacityError("unknown")
        reset = now + seconds
    return result(
        "stale" if reset is not None and reset <= now else "ok",
        reset,
        max(0, 100 - used),
    )


def combine(windows, now):
    """Known depletion wins over an unknown sibling window, until its reset."""
    depleted = [
        w
        for w in windows
        if w["state"] == "exhausted"
        or (w["state"] == "ok" and w["remaining_percent"] == 0)
    ]
    if depleted:
        resets = [w["reset_at"] for w in depleted]
        return result("exhausted", max(resets) if all(resets) else None, 0)
    for state in ("auth_error", "unavailable", "unknown", "stale"):
        if any(w["state"] == state for w in windows):
            return result(state)
    if not windows:
        return result("unknown")
    tightest = min(windows, key=lambda w: w["remaining_percent"])
    return dict(tightest)


def parse_usage(provider, body, model, now):
    """Apply shared AND relevant model windows; credits never enable a lane."""
    if not isinstance(body, dict):
        return result("unknown")
    windows = []

    def add(value, key):
        try:
            windows.append(window(value, key, now))
        except CapacityError as exc:
            windows.append(result(str(exc)))

    if provider == "claude":
        for required in ("five_hour", "seven_day"):
            add(body.get(required), "utilization")
        for name, value in body.items():
            # seven_day_breakdown splits usage by surface; it is not a limit window.
            if (
                name in ("five_hour", "seven_day", "seven_day_breakdown")
                or value is None
            ):
                continue
            if not name.startswith(("seven_day_", "five_hour_")):
                continue
            # Known named pools can be scoped. Unrecognized pools conservatively
            # constrain every model, rather than inventing independent capacity.
            if (
                name in ("seven_day_sonnet", "seven_day_opus")
                and name.removeprefix("seven_day_") not in model.lower()
            ):
                continue
            add(value, "utilization")
    else:
        rates = [body.get("rate_limit")]
        additional = body.get("additional_rate_limits")
        if additional is None:
            additional = []
        if isinstance(additional, dict):
            if any(not isinstance(v, dict) for v in additional.values()):
                windows.append(result("unknown"))
            additional = [
                dict(v, limit_name=k)
                for k, v in additional.items()
                if isinstance(v, dict)
            ]
        if not isinstance(additional, list):
            windows.append(result("unknown"))
            additional = []
        for item in additional:
            if not isinstance(item, dict):
                windows.append(result("unknown"))
                continue
            name = str(item.get("limit_name", "")).lower()
            if name.startswith("gpt-") and name != model.lower():
                continue
            rates.append(item.get("rate_limit", item))
        for rate in rates:
            if not isinstance(rate, dict):
                windows.append(result("unknown"))
                continue
            parsed = []
            for key in ("primary_window", "secondary_window"):
                if rate.get(key) is not None:
                    before = len(windows)
                    add(rate[key], "used_percent")
                    parsed.extend(windows[before:])
            if rate.get("allowed") is False or rate.get("limit_reached") is True:
                resets = [w["reset_at"] for w in parsed if w["remaining_percent"] == 0]
                reset = max(resets) if resets and all(resets) else None
                windows.append(
                    result("stale" if reset and reset <= now else "exhausted", reset, 0)
                )
            elif not parsed:
                windows.append(result("unknown"))
        usage = body.get("model_usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            windows.append(result("unknown"))
            usage = {}
        if model in usage:
            entry = usage[model]
            if not isinstance(entry, dict) or not isinstance(
                entry.get("available"), bool
            ):
                windows.append(result("unknown"))
            elif not entry["available"]:
                try:
                    reset = epoch(entry.get("available_at"))
                    windows.append(
                        result(
                            "stale" if reset and reset <= now else "exhausted", reset, 0
                        )
                    )
                except CapacityError:
                    windows.append(result("unknown"))
    return combine(windows, now)


def restriction(account, model, now):
    if account.get("disabled") or account.get("status") == "disabled":
        return result("disabled")
    blocks = []
    for cooldown in account.get("cooldowns") or []:
        if cooldown.get("scope") != "credential" and cooldown.get("model_key") != model:
            continue
        reset = epoch(cooldown.get("retry_at"))
        if reset is None or reset <= now:
            continue
        reason = cooldown.get("reason")
        state = "unavailable"
        if reason in ("credential_quota", "quota"):
            state = "exhausted"
        elif reason in ("unauthorized", "invalid_grant", "payment_required"):
            state = "auth_error"
        blocks.append(result(state, reset, 0 if state == "exhausted" else None))
    if blocks:
        return combine(blocks, now)
    if account.get("unavailable") or account.get("status") == "error":
        # Legacy APIs don't expose the reason. Never guess quota from free text.
        return result("unavailable", epoch(account.get("next_retry_after")))
    return None


def read_key(config):
    name = config.get("key_env", "CLIPROXYAPI_MANAGEMENT_KEY")
    key = os.environ.get(name)
    if not key and config.get("key_file"):
        path = Path(config["key_file"]).expanduser()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise CapacityError("insecure_key_file")
            value = stream.read(65537)
        if len(value) > 65536:
            raise CapacityError("invalid_key_file")
        # A raw key or a protected dotenv file. Do not execute/source the file.
        assignments = [
            line.split("=", 1)[1].strip().strip("\"'")
            for line in value.splitlines()
            if line.startswith(name + "=")
        ]
        key = assignments[0] if assignments else value.strip()
    if not key or "\n" in key or "\r" in key:
        raise CapacityError("missing_management_key")
    return key


class Client:
    def __init__(self, config):
        self.base = config["management_url"].rstrip("/")
        url = urllib.parse.urlsplit(self.base)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/v0/management")
        ):
            raise CapacityError("invalid_management_url")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")
        ):
            raise CapacityError("insecure_management_url")
        if not self.base.endswith("/v0/management"):
            self.base += "/v0/management"
        self.key = read_key(config)
        self.timeout = min(10, max(0.1, float(config.get("timeout_seconds", 4))))
        self.deadline = time.monotonic() + min(
            60, max(1, float(config.get("budget_seconds", 20)))
        )
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, data=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CapacityError("unavailable")
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(
                request, timeout=min(self.timeout, remaining)
            ) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise CapacityError("unknown")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise CapacityError(
                "auth_error" if exc.code in (401, 403) else "unavailable"
            ) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise CapacityError("unavailable") from None

    def usage(self, account, provider):
        headers = {"Authorization": "Bearer $TOKEN$", "Accept": "application/json"}
        if provider == "claude":
            headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            account_id = account.get("account_id") or (
                account.get("id_token") or {}
            ).get("chatgpt_account_id")
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
        data = self.request(
            "/api-call",
            {
                "auth_index": account["auth_index"],
                "method": "GET",
                "url": URLS[provider],
                "header": headers,
            },
        )
        status = data.get("status_code")
        if status in (401, 403):
            raise CapacityError("auth_error")
        if status != 200:
            # 429 on a quota endpoint is not evidence that inference is depleted.
            raise CapacityError("unavailable")
        body = data.get("body")
        try:
            return json.loads(body) if isinstance(body, str) else body
        except ValueError:
            raise CapacityError("unknown") from None


class UsageCache:
    """Normalized per-account usage, shared by every role's lane set.

    Provider usage endpoints throttle quickly (Anthropic's 429s for minutes), so a
    reading is reused for up to `fresh` seconds, less as the account nears empty,
    and for up to `stale` seconds when a refresh is throttled. Never past a
    window's reset. Proxy cooldowns are still read live on every call, so
    exhaustion seen by proxied inference blocks immediately; the reading only
    has to catch usage the proxy never sees (apps, chat, unproxied CLIs).
    `force` (--refresh) ignores every cached reading.
    """

    def __init__(self, directory, fresh=1800, stale=3600, force=False):
        self.directory = directory
        self.fresh = fresh
        self.stale = stale
        self.force = force

    def path(self, provider, identity, suffix=".json"):
        digest = hashlib.sha256(json.dumps([provider, identity]).encode()).hexdigest()
        return self.directory / ("usage-" + digest[:24] + suffix)

    def refresh_after(self, hits):
        """Full `fresh` above 50% left, a third of it from 20%, a sixth below."""
        left = min((v["remaining_percent"] or 0) for v in hits.values())
        return (
            self.fresh
            if left > 50
            else self.fresh / 3
            if left >= 20
            else self.fresh / 6
        )

    def get(self, provider, identity, models, now, limit=None):
        if self.force:
            return None
        try:
            data = json.loads(self.path(provider, identity).read_text())
            hits = {m: data["models"][m] for m in models}
            age = now - data["fetched_at"]
            # A concurrent launch may have refreshed after this one read the clock.
            if not -60 <= age < (limit or self.refresh_after(hits)):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if any(v["reset_at"] and v["reset_at"] <= now for v in hits.values()):
            return None
        return hits

    @contextlib.contextmanager
    def lock(self, provider, identity, wait=25):
        """One refresh per account at a time; other launches reuse its reading."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self.path(provider, identity, ".lock"), os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # Holders are bounded by the client budget; never wait forever.
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def put(self, provider, identity, models, now):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(
            self.path(provider, identity), {"fetched_at": now, "models": models}
        )


def write_private(path, data):
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".proxy-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect(client, requested, now, cache=None, holds=()):
    """Return per-lane anonymous account states, never account identifiers."""
    accounts = client.request("/auth-files").get("files")
    if not isinstance(accounts, list) or len(accounts) > 64:
        raise CapacityError("unknown")
    output = {f"{p}/{m}": [] for p, m in requested}
    seen = set()
    for account in accounts:
        provider = account.get("provider")
        models = [m for p, m in requested if p == provider]
        if not models or account.get("disabled") or account.get("status") == "disabled":
            continue
        identity = account.get("auth_index")
        if not identity or (provider, identity) in seen:
            continue
        seen.add((provider, identity))
        hold = holdlib.account_hold(holds, provider, account.get("name"), now)
        if hold:
            # A held account is never read, so holding it costs no usage request.
            for model in models:
                output[f"{provider}/{model}"].append(result("held", hold.get("until")))
            continue
        try:
            listed = client.request(
                "/auth-files/models?"
                + urllib.parse.urlencode({"name": account["name"]})
            ).get("models")
            if not isinstance(listed, list):
                raise CapacityError("unknown")
            supported = {item["id"] for item in listed}
        except CapacityError as exc:
            for model in models:
                output[f"{provider}/{model}"].append(result(str(exc)))
            continue
        eligible = [m for m in models if m in supported]
        pending = []
        for model in eligible:
            block = restriction(account, model, now)
            if block:
                output[f"{provider}/{model}"].append(block)
            else:
                pending.append(model)
        if not pending:
            continue
        states = cache and cache.get(provider, identity, pending, now)
        with (
            cache.lock(provider, identity)
            if cache and not states
            else contextlib.nullcontext()
        ):
            # Launches that waited on the lock reuse the reading it produced.
            states = states or (cache and cache.get(provider, identity, pending, now))
            if not states:
                try:
                    body = client.usage(account, provider)
                    # Parse every supported model so other roles can reuse this read.
                    parsed = {m: parse_usage(provider, body, m, now) for m in supported}
                    if cache:
                        cache.put(provider, identity, parsed, now)
                    states = {m: parsed[m] for m in pending}
                except CapacityError as exc:
                    # Throttled or unreachable: a bounded older reading beats none.
                    # An auth error is never masked.
                    if str(exc) == "unavailable" and cache:
                        states = cache.get(
                            provider, identity, pending, now, cache.stale
                        )
                    states = states or {m: result(str(exc)) for m in pending}
        for model in pending:
            output[f"{provider}/{model}"].append(states[model])
    return output


def snapshot(config, requested, cache_dir, force=False, now=None, holds=()):
    now = time.time() if now is None else now
    requested = sorted(set(requested))
    holds = [h for h in holdlib.active(holds, now) if h.get("account")]
    # Cache only anonymous, normalized decisions. Include source, model set and
    # account holds, so placing or lifting a hold is never hidden by the cache.
    identity = json.dumps([1, config, requested, holds], sort_keys=True).encode()
    path = cache_dir / ("proxy-" + hashlib.sha256(identity).hexdigest()[:24] + ".json")
    # Off by default: it would hide a proxy cooldown set since the last launch.
    # Usage readings, the throttled part, have their own per-account cache.
    ttl = min(60, max(0, float(config.get("cache_seconds", 0))))
    if not force:
        try:
            cached = json.loads(path.read_text())
            age = now - cached["fetched_at"]
            resets = [
                v["reset_at"]
                for values in cached["capacity"].values()
                for v in values
                if v["reset_at"]
            ]
            if 0 <= age < ttl and all(reset > now for reset in resets):
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
    client = Client(config)
    usage = UsageCache(
        cache_dir,
        max(0, float(config.get("usage_cache_seconds", 1800))),
        max(0, float(config.get("usage_stale_seconds", 3600))),
        force,
    )
    try:
        capacity = collect(client, requested, now, usage, holds)
    except CapacityError as exc:
        capacity = {f"{p}/{m}": [result(str(exc))] for p, m in requested}
    data = {"fetched_at": now, "capacity": capacity}
    # Failed reads never resurrect an older lane snapshot; only UsageCache's
    # bounded per-account readings survive a throttled refresh.
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, data)
    return data

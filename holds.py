"""Manual capacity holds: "do not launch agents on X" until a time or until lifted.

A hold names one provider (``codex``), a model prefix (``claude-fable``) or one
proxy account (its auth-file name). Holds only steer role selection; native CLIs
started by hand never read them. Expiry is lazy: a hold whose ``until`` has
passed is simply ignored by the next selection, so nothing has to run for it
to lift. ``until_reset`` marks a hold that may also lift early once the
provider reports capacity again (a manual reset); date and indefinite holds
never lift on their own before ``until``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

PROVIDERS = ("claude", "codex", "xai")


def load(path):
    """Every stored hold, expired or not. A missing file means no holds."""
    if not path:
        return []
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except FileNotFoundError:
        return []
    holds = data.get("holds") if isinstance(data, dict) else None
    if not isinstance(holds, list) or not all(isinstance(h, dict) for h in holds):
        raise ValueError("holds file must contain a holds list")
    return holds


def save(path, holds):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".holds-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "holds": holds}, stream, indent=1)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def active(holds, now=None):
    now = time.time() if now is None else now
    return [h for h in holds if h.get("until") is None or now < h["until"]]


def label(hold):
    return hold.get("account") or hold.get("model") or hold.get("provider")


def blocks_lane(hold, kind, model):
    """Provider and model holds; account holds are applied per account."""
    if hold.get("account"):
        return False
    if hold.get("model"):
        return model.lower().startswith(hold["model"].lower())
    return hold.get("provider") == kind


def lane_hold(holds, kind, model, now=None):
    return next((h for h in active(holds, now) if blocks_lane(h, kind, model)), None)


def account_hold(holds, provider, name, now=None):
    return next(
        (
            h
            for h in active(holds, now)
            if h.get("account") == name and h.get("provider") == provider
        ),
        None,
    )

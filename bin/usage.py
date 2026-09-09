#!/usr/bin/env python3
"""
Usage accounting for the bridge.

Two different things are worth showing, and they should not be confused:

1. What the *vendor* says your subscription has left. Authoritative, but only
   some vendors tell us:
     - ChatGPT  : quota headers captured by relay.py (percent used, reset time)
     - OpenCode : `opencode stats` reports its own cumulative totals
     - Cursor   : nothing in the CLI; the plan cap only shows up as an error,
                  so the last such error is remembered and surfaced
2. What *this bridge* has driven through each subscription: calls, failures,
   time spent, and — where the CLI reports it — tokens and cost.

Counter (2) is recorded here. It measures traffic the bridge caused, not your
total subscription use, and the dashboard says so rather than implying it is
the whole picture.
"""

import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_FILE = os.path.join(ROOT, "logs", "usage.json")
OPENAI_QUOTA = os.path.join(ROOT, "logs", "usage-openai.json")

_lock = threading.Lock()


def _load():
    try:
        with open(USAGE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"models": {}, "since": int(time.time())}


def _save(data):
    tmp = USAGE_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, USAGE_FILE)
    except OSError:
        pass


def record(provider, slug, ok, seconds, tokens=None, cost=None, error=None):
    """Add one call to the ledger. Never raises — accounting must not break a turn."""
    try:
        with _lock:
            data = _load()
            entry = data["models"].setdefault(slug, {
                "provider": provider, "calls": 0, "failed": 0,
                "seconds": 0.0, "tokens": 0, "cost_usd": 0.0,
                "last_used": None, "last_error": None,
            })
            entry["provider"] = provider
            entry["calls"] += 1
            if not ok:
                entry["failed"] += 1
                if error:
                    entry["last_error"] = error[:300]
            entry["seconds"] = round(entry["seconds"] + max(0.0, seconds), 1)
            if tokens:
                entry["tokens"] += int(tokens)
            if cost:
                entry["cost_usd"] = round(entry["cost_usd"] + float(cost), 6)
            entry["last_used"] = int(time.time())
            _save(data)
    except Exception:
        pass


def reset():
    _save({"models": {}, "since": int(time.time())})


def openai_quota():
    """Whatever ChatGPT last told us about the subscription's limits."""
    try:
        with open(OPENAI_QUOTA) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None

    def num(key):
        try:
            return float(raw.get(key, ""))
        except (TypeError, ValueError):
            return None

    out = {
        "captured_at": raw.get("_captured_at"),
        "plan": raw.get("x-codex-active-limit"),
        "windows": [],
    }
    for prefix, label in (("x-codex-", "Main"),
                          ("x-codex-bengalfox-", None)):
        used = num(prefix + "primary-used-percent")
        if used is None:
            continue
        name = raw.get(prefix + "limit-name") or label or "Secondary"
        mins = num(prefix + "primary-window-minutes") or 0
        out["windows"].append({
            "name": name,
            "used_percent": used,
            "window_hours": round(mins / 60, 1) if mins else None,
            "resets_at": raw.get(prefix + "primary-reset-at") or None,
        })
        sec_used = num(prefix + "secondary-used-percent")
        sec_mins = num(prefix + "secondary-window-minutes") or 0
        if sec_used is not None and sec_mins:
            out["windows"].append({
                "name": name + " (long window)",
                "used_percent": sec_used,
                "window_hours": round(sec_mins / 60, 1),
                "resets_at": raw.get(prefix + "secondary-reset-at") or None,
            })
    return out if out["windows"] else None


CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")

# Anthropic names its windows internally; only these are meaningful to show.
CLAUDE_WINDOWS = {
    "five_hour": "5-hour window",
    "seven_day": "7-day window",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
}


def claude_quota():
    """Claude Code caches the account's utilisation after each call."""
    try:
        with open(CLAUDE_CONFIG) as fh:
            cached = json.load(fh).get("cachedUsageUtilization") or {}
    except (OSError, ValueError):
        return None
    util = cached.get("utilization") or {}
    now = time.time()
    windows = []
    for key, label in CLAUDE_WINDOWS.items():
        w = util.get(key)
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        resets = _iso_to_epoch(w.get("resets_at"))
        # Once the reset time has passed the window has rolled over, so the
        # cached number describes a period that is already gone. Say so rather
        # than draw a bar that looks current.
        windows.append({
            "name": label,
            "used_percent": float(w["utilization"]),
            "resets_at": resets,
            "locked": w.get("locked_reason"),
            "stale": bool(resets and resets < now),
        })
    if not windows:
        return None
    return {
        "captured_at": int((cached.get("fetchedAtMs") or 0) / 1000) or None,
        "plan": None,
        "windows": windows,
        "note": ("Claude Code only refreshes this from an interactive session — "
                 "headless calls do not update it. Open Claude Code (or run /usage "
                 "in it) to bring these numbers up to date."),
    }


def _iso_to_epoch(value):
    if not value:
        return None
    try:
        import datetime
        txt = value.replace("Z", "+00:00")
        return int(datetime.datetime.fromisoformat(txt).timestamp())
    except (ValueError, TypeError, ImportError):
        return None


def snapshot():
    """Everything the dashboard needs, in one object."""
    data = _load()
    by_provider = {}
    for slug, e in data.get("models", {}).items():
        p = by_provider.setdefault(e.get("provider", "?"), {
            "calls": 0, "failed": 0, "seconds": 0.0,
            "tokens": 0, "cost_usd": 0.0, "models": [],
        })
        p["calls"] += e["calls"]
        p["failed"] += e["failed"]
        p["seconds"] = round(p["seconds"] + e["seconds"], 1)
        p["tokens"] += e.get("tokens", 0)
        p["cost_usd"] = round(p["cost_usd"] + e.get("cost_usd", 0.0), 4)
        p["models"].append(dict(e, slug=slug))
    for p in by_provider.values():
        p["models"].sort(key=lambda m: -m["calls"])
    return {
        "since": data.get("since"),
        "providers": by_provider,
        "quotas": {
            # provider key -> what that vendor reports about remaining quota
            "codex": openai_quota(),
            "claude": claude_quota(),
        },
        "openai_quota": openai_quota(),   # kept for older callers
    }

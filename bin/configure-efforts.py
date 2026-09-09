#!/usr/bin/env python3
"""
Collapse per-effort model variants into one selectable model each.

Codex already has a reasoning-effort selector (low / medium / high / xhigh) and
a Fast speed tier. Vendors expose the same idea two different ways:

  * Cursor bakes it into the model id — `cursor-grok-4.6-high`,
    `cursor-grok-4.6-xhigh-fast`, and so on. Those become `effort_variants`
    plus a `fast_suffix`, so one picker entry covers all eight.
  * Grok and Muse take a `--reasoning-effort` flag. Those just declare which
    efforts they accept and the provider's `effort_flag` carries the value.

Claude Code and the OpenCode free models have no effort control, so they stay
single-effort and Codex shows no slider for them.

Run this after changing which models are enabled.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, save_config, slug_for  # noqa: E402

EFFORTS = ["low", "medium", "high", "xhigh"]


def build_cursor_families(models):
    """Group `<family>-<effort>[-fast]` ids into one entry per family."""
    pattern = re.compile(r"^(?P<family>.+?)-(?P<effort>low|medium|high|xhigh)(?P<fast>-fast)?$")
    families = {}
    passthrough = []

    for m in models:
        match = pattern.match(m["id"])
        if not match:
            passthrough.append(m)
            continue
        fam = match.group("family")
        entry = families.setdefault(fam, {"variants": {}, "fast": set(), "label": None})
        effort = match.group("effort")
        if match.group("fast"):
            entry["fast"].add(effort)
        else:
            entry["variants"][effort] = m["id"]
            # Strip the effort word from the label: "Cursor Grok 4.6 Extra High" -> "Cursor Grok 4.6"
            if entry["label"] is None:
                entry["label"] = m["label"]

    out = []
    for fam, info in families.items():
        if not info["variants"]:
            continue
        label = info["label"] or fam
        for tail in (" Extra High", " Low", " Medium", " High", " Thinking"):
            if label.endswith(tail):
                label = label[: -len(tail)]
        out.append({
            "id": fam,
            "label": label.strip(),
            "enabled": False,
            "effort_variants": info["variants"],
            "fast_efforts": sorted(info["fast"]),
            "fast_suffix": "-fast",
        })
    return out + passthrough


def main():
    cfg = load_config()
    P = cfg["providers"]

    # --- Cursor: fold the 218 ids into families -------------------------
    previously_on = {m["id"] for m in P["cursor"]["models"] if m.get("enabled")}
    P["cursor"]["models"] = build_cursor_families(P["cursor"]["models"])
    for m in P["cursor"]["models"]:
        # Re-enable the family if any of its old variants was on.
        variants = set((m.get("effort_variants") or {}).values())
        variants |= {v + "-fast" for v in variants}
        m["enabled"] = bool(variants & previously_on) or m["id"] in previously_on

    # --- Grok / Muse: flag-driven effort --------------------------------
    P["grok"]["effort_flag"] = "--reasoning-effort"
    for m in P["grok"]["models"]:
        if m["id"] != "default":
            m["efforts"] = ["low", "medium", "high"]

    P["muse"]["effort_flag"] = "--reasoning-effort"
    for m in P["muse"]["models"]:
        m["efforts"] = ["low", "medium", "high", "xhigh"]

    save_config(cfg)

    for pname, spec in P.items():
        for m in spec["models"]:
            if not m.get("enabled"):
                continue
            slug = slug_for(pname, m["id"])
            if m.get("effort_variants"):
                fast = " +fast" if m.get("fast_efforts") else ""
                print("  %-34s efforts=%s%s" % (slug, sorted(m["effort_variants"]), fast))
            elif m.get("efforts"):
                print("  %-34s efforts=%s (via %s)" % (slug, m["efforts"], spec["effort_flag"]))
            else:
                print("  %-34s single effort" % slug)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Re-scan every vendor for models, keeping your choices intact.

Vendors add and retire models constantly. This asks each CLI what it offers
today, folds Cursor's per-effort ids back into families, picks up any new GPT
models from Codex's own cache, and reports exactly what changed.

Your `enabled` flags are preserved: a model you turned on stays on, a model you
turned off stays off, and anything genuinely new arrives switched off so the
picker never grows behind your back. The "unavailable" marks from a Test all
sweep are cleared for models that came back, since availability may have
changed (a Cursor cap resetting, say).
"""

import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import load_config, save_config, slug_for  # noqa: E402

ANSI = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
EFFORT_SUFFIX = re.compile(r"^(?P<family>.+?)-(?P<effort>low|medium|high|xhigh)(?P<fast>-fast)?$")


def run(argv, timeout=120):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return ANSI.sub("", proc.stdout or "")


# ---------------------------------------------------------------- scanners

def scan_cursor(binary=None):
    binary = binary or shutil.which("cursor-agent")
    if not binary:
        return None
    flat = []
    for line in run([binary, "--list-models"]).splitlines():
        m = re.match(r"\s*([A-Za-z0-9._-]+)\s+-\s+(.+?)\s*$", line)
        if m and m.group(1) != "Available":
            flat.append({"id": m.group(1), "label": m.group(2)})
    if not flat:
        return None

    families, passthrough = {}, []
    for m in flat:
        hit = EFFORT_SUFFIX.match(m["id"])
        if not hit:
            passthrough.append(m)
            continue
        fam = families.setdefault(hit.group("family"),
                                  {"variants": {}, "fast": set(), "label": None})
        effort = hit.group("effort")
        if hit.group("fast"):
            fam["fast"].add(effort)
        else:
            fam["variants"][effort] = m["id"]
            if fam["label"] is None:
                fam["label"] = m["label"]

    out = []
    for fam, info in families.items():
        if not info["variants"]:
            continue
        label = info["label"] or fam
        for tail in (" Extra High", " Low", " Medium", " High", " Thinking"):
            if label.endswith(tail):
                label = label[: -len(tail)]
        out.append({
            "id": fam, "label": label.strip(),
            "effort_variants": info["variants"],
            "fast_efforts": sorted(info["fast"]),
            "fast_suffix": "-fast",
        })
    return out + passthrough


def scan_opencode(binary=None):
    binary = binary or shutil.which("opencode")
    if not binary:
        return None
    out = []
    for line in run([binary, "models"]).splitlines():
        s = line.strip()
        # Only the `opencode/` provider's *-free models cost nothing.
        if s.startswith("opencode/") and s.endswith("-free"):
            label = s.split("/", 1)[1].replace("-free", "").replace("-", " ").title()
            out.append({"id": s, "label": label.replace(" Contributor", "").strip()})
    return out or None


def scan_grok(binary=None):
    binary = binary or shutil.which("grok")
    if not binary:
        return None
    out = [{"id": "default", "label": "Grok", "efforts": ["low", "medium", "high"]}]
    for line in run([binary, "models"]).splitlines():
        m = re.match(r"\s*[*-]\s*([A-Za-z0-9._-]+)", line)
        if m and m.group(1) not in ("Available", "Default"):
            mid = m.group(1)
            out.append({"id": mid, "label": mid.replace("grok-", "Grok ").strip(),
                        "efforts": ["low", "medium", "high"]})
    seen, uniq = set(), []
    for m in out:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        uniq.append(m)
    return uniq if len(uniq) > 1 else None


def scan_codex_gpt(binary=None):
    """New GPT models show up in Codex's own cache."""
    try:
        cache = json.load(open(os.path.expanduser("~/.codex/models_cache.json")))
    except (OSError, ValueError):
        return None
    out = []
    for m in cache.get("models", []):
        if m.get("visibility") != "list":
            continue
        out.append({
            "id": m["slug"], "label": m["display_name"],
            "efforts": [lv["effort"] for lv in (m.get("supported_reasoning_levels") or [])],
            "fast": any(t.get("id") == "priority" for t in (m.get("service_tiers") or [])),
        })
    return out or None


SCANNERS = {
    "cursor": scan_cursor,
    "opencode": scan_opencode,
    "grok": scan_grok,
    "codex": scan_codex_gpt,
}


def merge(existing, found):
    """Keep the user's on/off choices; new models arrive switched off."""
    prior = {m["id"]: m for m in existing}
    # "default" is ours, not the vendor's — it means "whatever the account is
    # already set to". A scan never returns it, so carry it across explicitly.
    if "default" in prior and not any(m["id"] == "default" for m in found):
        found = [prior["default"]] + list(found)
    merged, added = [], []
    for m in found:
        old = prior.get(m["id"])
        entry = dict(m)
        if old:
            entry["enabled"] = bool(old.get("enabled"))
            # Availability can change (a plan cap resetting), so re-test rather
            # than carry an old failure forward forever.
            entry.pop("unavailable", None)
        else:
            entry["enabled"] = False
            added.append(m["id"])
        merged.append(entry)
    removed = [i for i in prior if i not in {m["id"] for m in found}]
    return merged, added, removed


def main():
    cfg = load_config()
    report = {"added": {}, "removed": {}, "unchanged": []}
    failures = []

    for name, scanner in SCANNERS.items():
        spec = cfg["providers"].get(name)
        if spec is None:
            continue
        # The configured command already holds an absolute path to the CLI.
        configured = (spec.get("cmd") or [None])[0]
        found = scanner(configured)
        if not found:
            failures.append(name)
            print("  %-10s could not be scanned — left as is" % name)
            continue
        merged, added, removed = merge(spec.get("models", []), found)
        spec["models"] = merged
        if added:
            report["added"][name] = added
        if removed:
            report["removed"][name] = removed
        if not added and not removed:
            report["unchanged"].append(name)
        print("  %-10s %d models (%d new, %d gone)" % (name, len(merged), len(added), len(removed)))

    save_config(cfg)

    for name, ids in report["added"].items():
        print("\n  new in %s: %s" % (name, ", ".join(slug_for(name, i) for i in ids[:12])
                                     + (" …" if len(ids) > 12 else "")))
    for name, ids in report["removed"].items():
        print("\n  gone from %s: %s" % (name, ", ".join(ids[:12])
                                        + (" …" if len(ids) > 12 else "")))
    if failures:
        print("\n  Could not reach: %s. Those providers were left untouched."
              % ", ".join(failures))
    if not report["added"] and not report["removed"] and not failures:
        print("\n  Nothing changed — every vendor offers the same models as before.")
    else:
        print("\n  New models arrive switched off. Enable what you want, then Apply to Codex.")


if __name__ == "__main__":
    main()

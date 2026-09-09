#!/usr/bin/env python3
"""
Regenerate the Codex model catalog: every built-in OpenAI model, verbatim,
plus one entry per *enabled* bridge model from etc/providers.json.

Codex's `model_catalog_json` replaces the remote model list rather than merging
with it, so the built-ins are copied straight out of Codex's own live cache
(~/.codex/models_cache.json). That keeps the GPT models — and the default —
exactly as Codex shipped them. Re-run this after Codex refreshes that cache, or
just press "Apply to Codex" in the dashboard:

    python3 ~/.codex-subscription-bridge/bin/build-catalog.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CATALOG_PATH, CODEX_MODELS_CACHE,  # noqa: E402
                    enabled_models, load_config)

BASE_INSTRUCTIONS = (
    "You are a coding assistant reached through a local bridge from the Codex "
    "app. You run on this machine with your own tools and your own workspace "
    "access. Answer directly and concisely. When you change files, say which "
    "files you changed."
)


EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]
EFFORT_BLURB = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra reasoning depth for hard problems",
    "max": "Maximum reasoning depth",
    "ultra": "Deepest reasoning, slowest",
}


def reasoning_levels(model):
    """Which efforts Codex should offer for this model.

    Cursor exposes effort as separate model ids, Grok and Muse as a flag, and
    Claude not at all — so the slider only appears where it does something.
    """
    efforts = list((model.get("effort_variants") or {}).keys()) or model.get("efforts") or []
    efforts = [e for e in EFFORT_ORDER if e in efforts]
    # `fast` is set explicitly by providers whose CLI takes a speed flag;
    # `fast_efforts` covers vendors that encode speed in the model id.
    has_fast = bool(model.get("fast_efforts")) or bool(model.get("fast"))
    if not efforts:
        return ["medium"], has_fast
    return efforts, has_fast


def entry(slug, provider_spec, model, priority):
    """One catalog entry, shaped like Codex's own non-OpenAI model entries."""
    ctx = int(provider_spec.get("context_window", 128000))
    billing = provider_spec.get("billing", "local")
    efforts, has_fast = reasoning_levels(model)
    requested_default = model.get("default_effort")
    default_effort = (requested_default if requested_default in efforts
                      else ("medium" if "medium" in efforts else efforts[0]))
    # Show the model's own name only. The provider is already obvious from the
    # description line, and prefixing every entry just crowds the picker.
    display = model.get("label", model["id"])
    vendor = provider_spec.get("display_name", "")
    return {
        "additional_speed_tiers": ["fast"] if has_fast else [],
        "apply_patch_tool_type": None,
        "auto_compact_token_limit": None,
        "auto_review_model_override": None,
        "availability_nux": None,
        "base_instructions": BASE_INSTRUCTIONS,
        "comp_hash": None,
        "context_window": ctx,
        "default_reasoning_level": default_effort,
        "default_reasoning_summary": "none",
        "default_service_tier": None,
        "default_verbosity": None,
        "description": "Runs locally via %s — %s." % (vendor, billing),
        "display_name": display,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "include_skills_usage_instructions": False,
        "input_modalities": ["text"],
        "max_context_window": ctx,
        "model_messages": None,
        "multi_agent_version": None,
        "prefer_websockets": False,
        "priority": priority,
        "reasoning_summary_format": "experimental",
        "service_tiers": ([{"id": "priority", "name": "Fast",
                            "description": "Faster variant of the same model"}]
                          if has_fast else []),
        "shell_type": "default",
        "slug": slug,
        "support_verbosity": False,
        "supported_in_api": True,
        # One picker entry per model; the effort chosen here selects the
        # vendor's own variant (Cursor) or rides on its --reasoning-effort flag.
        "supported_reasoning_levels": [
            {"effort": e, "description": EFFORT_BLURB[e]} for e in efforts
        ],
        "supports_image_detail_original": False,
        "supports_parallel_tool_calls": False,
        "supports_reasoning_summaries": False,
        "supports_reasoning_summary_parameter": False,
        "supports_search_tool": False,
        "tool_mode": None,
        "truncation_policy": {"limit": 10000, "mode": "bytes"},
        "upgrade": None,
        "use_responses_lite": False,
        "visibility": "list",
        # Codex rejects a null here; "text" is what its own
        # non-OpenAI entries use. supports_search_tool stays false.
        "web_search_tool_type": "text",
    }


def main():
    mode = "gpt"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
    if not os.path.exists(CODEX_MODELS_CACHE):
        sys.exit("missing %s — open Codex once so it caches the model list"
                 % CODEX_MODELS_CACHE)

    builtin = json.load(open(CODEX_MODELS_CACHE)).get("models") or []
    if not builtin:
        sys.exit("%s has no models; refusing to build a catalog that would hide "
                 "the GPT entries" % CODEX_MODELS_CACHE)

    cfg = load_config()
    # In assistants mode OpenAI's models are unreachable through the bridge
    # provider, so hide them from the picker rather than offer dead entries.
    # They stay in the catalog so existing threads still resolve metadata.
    # assistants: OpenAI's models are unreachable through the bridge, so hide
    #             them rather than offer dead entries.
    # unified:    the relay carries them to OpenAI untouched, so they stay
    #             listed and fully native alongside the assistants.
    # gpt:        stock Codex; built-ins listed exactly as shipped.
    models = []
    for m in builtin:
        m = dict(m)
        if mode == "assistants":
            m["visibility"] = "hide"
        models.append(m)
    known = {m.get("slug") for m in models}

    added = []
    priority = 900
    for slug, _pname, spec, model in enabled_models(cfg):
        if slug in known:
            print("  ! skipping %r — collides with a built-in slug" % slug)
            continue
        priority += 1
        models.append(entry(slug, spec, model, priority))
        known.add(slug)
        added.append(slug)

    tmp = CATALOG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"models": models}, fh, indent=2)
    os.replace(tmp, CATALOG_PATH)

    print("wrote %s" % CATALOG_PATH)
    print("  %d built-in models preserved%s"
          % (len(builtin),
             " (hidden from the picker in assistants mode)" if mode == "assistants"
             else " and still listed — native via the relay" if mode == "unified"
             else " and still listed"))
    print("  %d bridge models listed: %s" % (len(added), ", ".join(added) or "(none)"))
    print("  restart the Codex app to pick this up")


if __name__ == "__main__":
    main()

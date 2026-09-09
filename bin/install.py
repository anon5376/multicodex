#!/usr/bin/env python3
"""
Set multicodex up on this machine.

Finds which assistant CLIs are installed, asks each one what models it offers,
writes etc/providers.json, backs up your Codex config, and registers the local
provider. Nothing is enabled in the Codex picker until you say so — run it,
then open the dashboard and pick.

Safe to re-run: your existing choices in etc/providers.json are preserved.
"""

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT  # noqa: E402

CODEX_DIR = os.path.expanduser("~/.codex")
CODEX_CONFIG = os.path.join(CODEX_DIR, "config.toml")

# How to drive each vendor's CLI headlessly, and which env vars to strip so a
# stray API key can never turn subscription usage into metered billing.
PROVIDERS = {
    "claude": {
        "binary": "claude",
        "display_name": "Claude Code",
        "billing": "Claude subscription",
        "cmd": ["-p", "--output-format", "json"],
        "input": "stdin",
        "model_flag": "--model",
        "effort_flag": "--effort",
        "output_json": True,
        "context_window": 200000,
        "unset_env": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                      "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"],
        "models": [
            {"id": "default", "label": "Claude Code", "efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "opus", "label": "Claude Opus", "efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "sonnet", "label": "Claude Sonnet", "efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "haiku", "label": "Claude Haiku", "efforts": ["low", "medium", "high", "xhigh", "max"]},
        ],
    },
    "cursor": {
        "binary": "cursor-agent",
        "display_name": "Cursor Agent",
        "billing": "Cursor subscription",
        # --trust covers the directory Codex is already working in. It does NOT
        # auto-approve commands (that would be --force), so Cursor's own
        # approval mode and sandbox still apply.
        "cmd": ["-p", "--trust", "--output-format", "text", "--workspace", "{cwd}"],
        "input": "stdin",
        "model_flag": "--model",
        "context_window": 200000,
        "unset_env": ["CURSOR_API_KEY", "CURSOR_API_ENDPOINT"],
        "models": [{"id": "default", "label": "Cursor Agent"}],
    },
    "grok": {
        "binary": "grok",
        "display_name": "Grok Code",
        "billing": "xAI subscription",
        "cmd": [],
        "input": "file",
        "file_flag": ["--prompt-file"],
        "model_flag": "--model",
        "effort_flag": "--reasoning-effort",
        "context_window": 256000,
        "unset_env": ["XAI_API_KEY", "GROK_API_KEY"],
        "models": [{"id": "default", "label": "Grok", "efforts": ["low", "medium", "high"]}],
    },
    "muse": {
        "binary": "muse",
        "display_name": "Muse",
        "billing": "Meta account",
        "cmd": ["exec"],
        "input": "file",
        "file_flag": ["--prompt-file"],
        "model_flag": "--model",
        "effort_flag": "--reasoning-effort",
        "context_window": 128000,
        "unset_env": ["META_API_KEY"],
        "models": [{"id": "default", "label": "Muse",
                    "efforts": ["low", "medium", "high", "xhigh"]}],
    },
    "opencode": {
        "binary": "opencode",
        "display_name": "OpenCode (free tier)",
        "billing": "free — no subscription, no metered API",
        "cmd": ["run"],
        "input": "arg",
        "model_flag": "-m",
        "context_window": 128000,
        "unset_env": [],
        "models": [],
    },
}


def say(msg):
    print("  " + msg)


def main():
    print("multicodex setup\n")

    found = {}
    for name, tpl in PROVIDERS.items():
        binary = shutil.which(tpl["binary"])
        if not binary:
            say("%-10s not installed — skipping" % name)
            continue
        spec = {k: v for k, v in tpl.items() if k != "binary"}
        spec["cmd"] = [binary] + list(tpl["cmd"])
        found[name] = spec
        say("%-10s found at %s" % (name, binary))

    if not found:
        sys.exit("\nNo supported assistant CLIs found. Install and log into at "
                 "least one of: claude, cursor-agent, grok, muse, opencode.")

    cfg_path = os.path.join(ROOT, "etc", "providers.json")
    existing = {}
    if os.path.exists(cfg_path):
        try:
            existing = json.load(open(cfg_path)).get("providers", {})
            say("keeping your existing model choices")
        except ValueError:
            pass

    for name, spec in found.items():
        prior = {m["id"]: m for m in existing.get(name, {}).get("models", [])}
        for m in spec["models"]:
            m["enabled"] = bool(prior.get(m["id"], {}).get("enabled"))

    cfg = {
        "_comment": [
            "Command templates and model lists only. NO credentials live here.",
            "Each vendor CLI authenticates with its own OAuth/subscription session.",
            "'unset_env' strips provider API keys from the child environment so a",
            "stray key can never silently turn subscription usage into metered billing.",
        ],
        "host": "127.0.0.1",
        "port": int(os.environ.get("MULTICODEX_PORT", 8848)),
        "relay_openai": False,
        "relay_timeout": 1800,
        "upstream_base_url": "https://chatgpt.com/backend-api/codex",
        "providers": found,
    }
    os.makedirs(os.path.join(ROOT, "etc"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh, indent=2)
    say("wrote etc/providers.json")

    # Back up the Codex config before anything touches it.
    if os.path.exists(CODEX_CONFIG):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(ROOT, "backups", stamp)
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(CODEX_CONFIG, os.path.join(dest, "config.toml"))
        cache = os.path.join(CODEX_DIR, "models_cache.json")
        if os.path.exists(cache):
            shutil.copy2(cache, os.path.join(dest, "models_cache.json"))
        with open(os.path.join(ROOT, "backups", "LATEST"), "w") as fh:
            fh.write(stamp + "\n")
        say("backed up your Codex config to backups/%s" % stamp)
    else:
        say("no ~/.codex/config.toml yet — run Codex once first")

    # Ask each vendor what it actually offers today.
    print()
    sys.stdout.flush()   # keep our output ahead of the subprocess's
    subprocess.run([sys.executable, os.path.join(ROOT, "bin", "refresh-models.py")])
    subprocess.run([sys.executable, os.path.join(ROOT, "bin", "configure-efforts.py")],
                   capture_output=True)

    print("\nNext:")
    print("  bin/bridgectl start        # start the service")
    print("  open http://127.0.0.1:%d/  # pick models, then Apply to Codex" % cfg["port"])
    print("  bin/bridgectl on           # register the provider with Codex")
    print("\nNothing appears in Codex until you enable models and press Apply.")


if __name__ == "__main__":
    main()

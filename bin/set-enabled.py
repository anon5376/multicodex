#!/usr/bin/env python3
"""
Master switch: bridge on, or Codex exactly as it was.

    set-enabled.py off      remove every trace from Codex and stop the service
    set-enabled.py on       put it all back
    set-enabled.py status

OFF leaves ~/.codex/config.toml with none of our keys, none of our blocks, and
none of our profile files — the stock state this machine was in before setup.
The one thing it does NOT undo is your own choice of default model: if you
picked a different GPT model since, that stays. If you were sitting on a bridge
model at the time, it falls back to the last GPT model you used, because a
bridge model would not resolve with the bridge gone.

Your original config is kept untouched at backups/<timestamp>/config.toml.
"""

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, load_config  # noqa: E402
import set_mode_helpers as H  # noqa: E402

CODEX_DIR = os.path.expanduser("~/.codex")
CODEX_CONFIG = os.path.join(CODEX_DIR, "config.toml")
LEDGER = os.path.join(ROOT, "etc", "created-profiles.txt")
BRIDGECTL = os.path.join(ROOT, "bin", "bridgectl")


def builtin_gpt_slugs():
    try:
        cache = json.load(open(os.path.expanduser("~/.codex/models_cache.json")))
        return {m["slug"] for m in cache.get("models", [])}
    except (OSError, ValueError):
        return set()


def our_profiles():
    names = []
    if os.path.exists(LEDGER):
        names = [ln.strip() for ln in open(LEDGER) if ln.strip()]
    return names + ["gpt-native"]


def turn_off():
    text = H.read_config()

    # 1. strip every block we ever wrote
    text = H.strip_all_blocks(text)

    # 2. keep the user's model unless it is one of ours
    current = H.current_default_model(text)
    if current not in builtin_gpt_slugs():
        restore = H.gpt_model_to_restore()
        text = H.set_default_model(text, restore)
        print("  default model: %s -> %s (a bridge model cannot resolve when off)"
              % (current, restore))
    else:
        print("  default model: %s (your own choice, kept)" % current)

    H.write_config(text)
    print("  config.toml: all bridge blocks removed")

    # 3. remove only the profile files we created
    removed = 0
    for name in our_profiles():
        p = os.path.join(CODEX_DIR, "%s.config.toml" % name)
        if os.path.exists(p):
            os.unlink(p)
            removed += 1
    print("  removed %d profile file(s) we created" % removed)

    # 4. stop the service
    subprocess.run([BRIDGECTL, "stop"], capture_output=True, timeout=60)
    print("  bridge stopped")
    print("\nCodex is back to its stock state. Quit and reopen the app.")


def turn_on():
    subprocess.run([BRIDGECTL, "start"], capture_output=True, timeout=90)

    cfg = load_config()
    relay = bool(cfg.get("relay_openai"))
    mode = "unified" if relay else "assistants"

    for script, args in (("build-catalog.py", ["--mode", mode]),
                         ("sync-profiles.py", [])):
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", script)] + args,
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            sys.exit((proc.stdout + proc.stderr).strip())

    text = H.read_config()
    text = H.strip_all_blocks(text)
    body = ("# Bridge ON (%s). Turn it off from the dashboard or with:\n"
            "#   bridgectl off\n"
            "model_catalog_json = \"%s\"\n"
            "model_provider = \"subscription_bridge\"\n"
            % (mode, os.path.join(ROOT, "etc", "model-catalog.json")))
    text = H.put_root_block(text, body)
    text = H.put_provider_block(text, requires_openai_auth=relay)

    # With the relay on, OpenAI's models still work, so the user's default
    # model needs no changing at all.
    if not relay:
        current = H.current_default_model(text)
        if current in builtin_gpt_slugs():
            enabled = H.enabled_slugs()
            if enabled:
                text = H.set_default_model(text, enabled[0])
                print("  default model: %s -> %s (GPT is unreachable without the relay)"
                      % (current, enabled[0]))
    H.write_config(text)

    print("  bridge on in %s mode%s" % (mode, " (GPT native via relay)" if relay else ""))
    print("\nQuit and reopen the Codex app.")


def status():
    text = H.read_config()
    on = 'model_provider = "subscription_bridge"' in text
    print("bridge: %s" % ("on" if on else "off"))
    print("default model: %s" % H.current_default_model(text))
    if on:
        print("relay (native GPT): %s" % ("on" if load_config().get("relay_openai") else "off"))


def main():
    what = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if what == "off":
        turn_off()
    elif what == "on":
        turn_on()
    elif what == "status":
        status()
    else:
        sys.exit("usage: set-enabled.py {on|off|status}")


if __name__ == "__main__":
    main()

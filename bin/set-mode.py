#!/usr/bin/env python3
"""
Switch what the Codex desktop app's model picker offers.

Why a switch at all: Codex resolves `model_provider` once per conversation and
the desktop picker only sends a model *name*, never a provider. So the app can
serve OpenAI's models or the local assistants, but not both at once — unless
something proxies GPT traffic, which would mean handling your ChatGPT token.
We don't do that. Instead you flip a mode.

  gpt         (default, and how the machine shipped)
              Nothing of ours is active: no custom provider, no custom catalog,
              GPT stays the default model. The bridge is still usable from the
              terminal with `codex --profile claude` and friends.

  assistants  The Codex app talks to the local bridge. The picker lists your
              logged-in assistants; OpenAI's models are hidden because they are
              not reachable through this provider.

Quit and reopen the Codex app after switching, and start a NEW conversation —
Codex remembers the model per conversation.

Note on TOML: bare keys after a [table] header belong to that table, so the
root keys are written above the first table and the provider table is appended
at the end. Two separate marked blocks, one job each.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, CATALOG_PATH, enabled_models, load_config  # noqa: E402

CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
ORIG_MODEL_FILE = os.path.join(ROOT, "etc", "original-default-model.txt")
LAST_GPT_FILE = os.path.join(ROOT, "etc", "last-gpt-model.txt")

R_BEGIN = "# ---- BEGIN codex-subscription-bridge root keys (safe to delete) ----"
R_END = "# ---- END codex-subscription-bridge root keys ----"
T_BEGIN = "# ---- BEGIN codex-subscription-bridge provider (safe to delete) ----"
T_END = "# ---- END codex-subscription-bridge provider ----"

PROVIDER_TABLE = (
    "[model_providers.subscription_bridge]\n"
    "name = \"Local subscription bridge\"\n"
    "base_url = \"http://127.0.0.1:8848/v1\"\n"
    "wire_api = \"responses\"\n"
    "request_max_retries = 0\n"
    "stream_max_retries = 0\n"
    "stream_idle_timeout_ms = 900000\n"
)


def read_config():
    with open(CODEX_CONFIG) as fh:
        return fh.read()


def write_config(text):
    tmp = CODEX_CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, CODEX_CONFIG)


def strip_block(text, begin, end):
    return re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)


def strip_legacy(text):
    """Remove the earlier single-block form, if this machine still has it."""
    return re.sub(
        r"# ---- BEGIN codex-subscription-bridge \(added by setup; safe to delete\) ----"
        r".*?# ---- END codex-subscription-bridge ----\n?", "", text, flags=re.S)


def put_root_block(text, body):
    """Insert above the first [table] header, where bare keys are still root keys."""
    text = strip_block(text, R_BEGIN, R_END)
    block = "%s\n%s%s\n" % (R_BEGIN, body, R_END)
    m = re.search(r"^\[", text, re.M)
    if m:
        return text[:m.start()] + block + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def put_provider_block(text):
    text = strip_block(text, T_BEGIN, T_END)
    return text.rstrip("\n") + "\n\n%s\n%s%s\n" % (T_BEGIN, PROVIDER_TABLE, T_END)


def current_default_model(text):
    m = re.search(r'^model = "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def remember_original(text):
    if os.path.exists(ORIG_MODEL_FILE):
        return open(ORIG_MODEL_FILE).read().strip()
    model = current_default_model(text) or "gpt-6-astra"
    with open(ORIG_MODEL_FILE, "w") as fh:
        fh.write(model + "\n")
    return model


def set_default_model(text, model):
    return re.sub(r'^model = "[^"]+"', 'model = "%s"' % model, text, count=1, flags=re.M)


def remember_gpt_choice(text, bridge_slugs):
    """Note the GPT model in use, so returning to GPT mode restores *that* one.

    Codex writes the picker's choice straight into config.toml, so the model
    here may well have changed since install — snapping back to the install-time
    default would quietly undo the user's own choice.
    """
    current = current_default_model(text)
    if current and current not in bridge_slugs:
        with open(LAST_GPT_FILE, "w") as fh:
            fh.write(current + "\n")


def gpt_model_to_restore(original):
    if os.path.exists(LAST_GPT_FILE):
        saved = open(LAST_GPT_FILE).read().strip()
        if saved:
            return saved
    return original


def current_mode(text):
    seg = text.split(R_BEGIN)[-1].split(R_END)[0] if R_BEGIN in text else ""
    return "assistants" if 'model_provider = "subscription_bridge"' in seg else "gpt"


def build_catalog(mode):
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "build-catalog.py"),
                           "--mode", mode], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SystemExit((proc.stdout + proc.stderr).strip())
    return proc.stdout.strip()


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    text = read_config()
    original = remember_original(text)

    if mode not in ("gpt", "assistants", "status"):
        raise SystemExit("usage: set-mode.py {gpt|assistants|status} [default-model]")

    cfg_slugs = {s for s, _p, _sp, _m in enabled_models(load_config())}

    if mode == "status":
        print("mode: %s" % current_mode(text))
        print("default model: %s" % current_default_model(text))
        print("GPT model restored by 'gpt' mode: %s" % gpt_model_to_restore(original))
        return

    text = strip_legacy(text)

    if mode == "gpt":
        body = ("# GPT mode: no root overrides — Codex behaves exactly as it shipped.\n"
                "# The bridge stays usable from the terminal:  codex --profile claude \"...\"\n"
                "# Switch the app over with:  bridgectl mode assistants\n")
        restore = gpt_model_to_restore(original)
        text = put_root_block(text, body)
        text = put_provider_block(text)
        text = set_default_model(text, restore)
        write_config(text)
        print("mode: gpt — stock behaviour restored (default model %s, provider openai)." % restore)
        print("Quit and reopen the Codex app.")
        return

    cfg = load_config()
    slugs = [s for s, _p, _sp, _m in enabled_models(cfg)]
    if not slugs:
        raise SystemExit("no assistants enabled — pick some in the dashboard first")
    want = sys.argv[2] if len(sys.argv) > 2 else slugs[0]
    if want not in slugs:
        raise SystemExit("%r is not enabled; enabled: %s" % (want, ", ".join(slugs)))

    remember_gpt_choice(text, cfg_slugs)
    print(build_catalog("assistants"))
    body = ("# ASSISTANTS mode: the Codex app talks to the local bridge.\n"
            "# OpenAI's models are hidden from the picker — they are not reachable\n"
            "# through this provider. Restore stock with:  bridgectl mode gpt\n"
            "model_catalog_json = \"%s\"\n"
            "model_provider = \"subscription_bridge\"\n" % CATALOG_PATH)
    text = put_root_block(text, body)
    text = put_provider_block(text)
    text = set_default_model(text, want)
    write_config(text)
    print("mode: assistants — default model is now %r." % want)
    print("Quit and reopen the Codex app, then start a NEW conversation.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Shared editing of ~/.codex/config.toml.

Everything this setup writes lives inside clearly marked blocks, so removing it
is exact rather than best-effort. Two blocks are used because TOML puts a bare
key under whatever [table] precedes it: root keys go above the first table, the
provider table goes at the end.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, enabled_models, load_config  # noqa: E402

CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
ORIG_MODEL_FILE = os.path.join(ROOT, "etc", "original-default-model.txt")
LAST_GPT_FILE = os.path.join(ROOT, "etc", "last-gpt-model.txt")

R_BEGIN = "# ---- BEGIN codex-subscription-bridge root keys (safe to delete) ----"
R_END = "# ---- END codex-subscription-bridge root keys ----"
T_BEGIN = "# ---- BEGIN codex-subscription-bridge provider (safe to delete) ----"
T_END = "# ---- END codex-subscription-bridge provider ----"
L_BEGIN = "# ---- BEGIN codex-subscription-bridge (added by setup; safe to delete) ----"
L_END = "# ---- END codex-subscription-bridge ----"


def read_config():
    with open(CODEX_CONFIG) as fh:
        return fh.read()


def write_config(text):
    tmp = CODEX_CONFIG + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, CODEX_CONFIG)


def _strip(text, begin, end):
    return re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)


def strip_all_blocks(text):
    """Remove every block this setup has ever written, including older forms."""
    for begin, end in ((R_BEGIN, R_END), (T_BEGIN, T_END), (L_BEGIN, L_END)):
        text = _strip(text, begin, end)
    return re.sub(r"\n{4,}", "\n\n\n", text)


def put_root_block(text, body):
    """Insert above the first [table] header, where bare keys are still root keys."""
    text = _strip(text, R_BEGIN, R_END)
    block = "%s\n%s%s\n" % (R_BEGIN, body, R_END)
    m = re.search(r"^\[", text, re.M)
    if m:
        return text[:m.start()] + block + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def put_provider_block(text, requires_openai_auth=False):
    text = _strip(text, T_BEGIN, T_END)
    table = ("[model_providers.subscription_bridge]\n"
             "name = \"Local subscription bridge\"\n"
             "base_url = \"http://127.0.0.1:8848/v1\"\n"
             "wire_api = \"responses\"\n")
    if requires_openai_auth:
        table += ("# Codex signs its own request; relay.py forwards it verbatim to OpenAI.\n"
                  "requires_openai_auth = true\n")
    table += ("request_max_retries = 0\n"
              "stream_max_retries = 0\n"
              "stream_idle_timeout_ms = 900000\n")
    return text.rstrip("\n") + "\n\n%s\n%s%s\n" % (T_BEGIN, table, T_END)


def current_default_model(text):
    m = re.search(r'^model = "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def set_default_model(text, model):
    return re.sub(r'^model = "[^"]+"', 'model = "%s"' % model, text, count=1, flags=re.M)


def remember_gpt_choice(text, bridge_slugs):
    """Note the GPT model in use so turning the bridge off restores *that* one."""
    current = current_default_model(text)
    if current and current not in bridge_slugs:
        with open(LAST_GPT_FILE, "w") as fh:
            fh.write(current + "\n")


def gpt_model_to_restore():
    for path in (LAST_GPT_FILE, ORIG_MODEL_FILE):
        if os.path.exists(path):
            value = open(path).read().strip()
            if value:
                return value
    return "gpt-6-astra"


def enabled_slugs():
    return [s for s, _p, _sp, _m in enabled_models(load_config())]

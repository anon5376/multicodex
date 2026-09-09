#!/usr/bin/env python3
"""
Keep ~/.codex/<slug>.config.toml in step with whatever is enabled.

One profile per enabled model, so the terminal works in either app mode:

    codex --profile claude "..."

Only files this tool created are ever removed — the list is tracked in
etc/created-profiles.txt, so your own profiles are never touched.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, enabled_models, load_config, profile_name  # noqa: E402

CODEX_DIR = os.path.expanduser("~/.codex")
LEDGER = os.path.join(ROOT, "etc", "created-profiles.txt")


def main():
    previous = []
    if os.path.exists(LEDGER):
        previous = [ln.strip() for ln in open(LEDGER) if ln.strip()]

    wanted = []
    for slug, pname, spec, model in enabled_models(load_config()):
        name = profile_name(slug)
        path = os.path.join(CODEX_DIR, "%s.config.toml" % name)
        with open(path, "w") as fh:
            profile_text = (
                "# codex-subscription-bridge profile (safe to delete)\n"
                "# Use with:  codex --profile %s\n"
                "# Routes to %s via its own logged-in CLI (%s).\n"
                "model = \"%s\"\n"
                "model_provider = \"subscription_bridge\"\n"
                % (name, spec.get("display_name", pname), spec.get("billing", ""), slug))
            default_effort = model.get("default_effort")
            if default_effort:
                profile_text += 'model_reasoning_effort = "%s"\n' % default_effort
            fh.write(profile_text)
        wanted.append(name)

    removed = []
    for slug in previous:
        if slug in wanted:
            continue
        path = os.path.join(CODEX_DIR, "%s.config.toml" % slug)
        if os.path.exists(path):
            os.unlink(path)
            removed.append(slug)

    with open(LEDGER, "w") as fh:
        fh.write("\n".join(wanted) + "\n")

    print("  profiles in sync: %d (%s)" % (len(wanted), ", ".join(wanted)))
    if removed:
        print("  removed %d stale: %s" % (len(removed), ", ".join(removed)))


if __name__ == "__main__":
    main()

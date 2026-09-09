"""Shared helpers for the Codex subscription bridge."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "etc", "providers.json")
CATALOG_PATH = os.path.join(ROOT, "etc", "model-catalog.json")
CODEX_MODELS_CACHE = os.path.expanduser("~/.codex/models_cache.json")


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def save_config(cfg):
    """Write atomically so a half-written file can never break the bridge."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


def slug_for(provider_name, model_id):
    """Codex-facing model slug. Stable, readable, and safe in a URL.

    The provider name is only prefixed when the model id doesn't already carry
    it, so we get `grok-4.6` rather than `grok-grok-4.6`.
    """
    if model_id == "default":
        base = provider_name
    else:
        tail = model_id.split("/")[-1]
        base = tail if tail.startswith(provider_name) else "%s-%s" % (provider_name, tail)
    return re.sub(r"[^A-Za-z0-9._-]", "-", base)


def enabled_models(cfg):
    """Yield (slug, provider_name, provider_spec, model_dict) for enabled models."""
    for pname, spec in cfg["providers"].items():
        for model in spec.get("models", []):
            if model.get("enabled"):
                yield slug_for(pname, model["id"]), pname, spec, model


def resolve(cfg, slug):
    """Map a Codex model slug back to (provider_spec, model_id). None if unknown."""
    for candidate, _pname, spec, model in enabled_models(cfg):
        if candidate == slug:
            return spec, model["id"]
    return None, None


def resolve_model(cfg, slug):
    """Map a Codex model slug to (provider_spec, model_dict). (None, None) if unknown.

    The returned spec carries `_provider`/`_slug` so downstream accounting can
    attribute a call without threading extra arguments through every layer.
    """
    for candidate, pname, spec, model in enabled_models(cfg):
        if candidate == slug:
            spec = dict(spec, _provider=pname, _slug=candidate)
            return spec, model
    return None, None


def profile_name(slug):
    """Codex only accepts a plain `--profile` name, so dots become dashes.

    The model slug itself keeps its dots (`grok-4.6`); only the profile file
    name is flattened (`grok-4-6.config.toml`).
    """
    return re.sub(r"[^A-Za-z0-9_-]", "-", slug)

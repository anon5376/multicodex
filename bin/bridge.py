#!/usr/bin/env python3
"""
codex-subscription-bridge
=========================
A local, OpenAI-Chat-Completions-compatible HTTP shim that lets the Codex CLI /
desktop app talk to *other* coding assistants that are already logged in on this
machine, using each vendor's own subscription session.

How it preserves subscription billing
-------------------------------------
The bridge never sees, stores, or forwards any credential. For each request it
shells out to the vendor's own CLI in headless mode (`claude -p`,
`cursor-agent -p`, `grok -p`). Each CLI authenticates itself with its own OAuth
session, exactly as it does when you run it by hand. No API keys are read,
written, or transmitted by this process.

Protocol note
-------------
Codex speaks OpenAI Chat Completions. The vendor CLIs speak "prompt in, text
out" and run their *own* agent loop (their own tools, their own sandbox). So the
bridge translates: it flattens the Codex conversation into a single prompt,
runs the vendor agent in the caller's working directory, and returns the reply
as one assistant message. Codex is the shell; the vendor CLI does the work.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (ROOT, CONFIG_PATH, load_config, save_config,  # noqa: E402
                    enabled_models, resolve, resolve_model)
from common import slug_for as common_slug  # noqa: E402
from common import profile_name  # noqa: E402
from relay import relay_post  # noqa: E402
import usage  # noqa: E402

LOG_PATH = os.path.join(ROOT, "logs", "bridge.log")
DASHBOARD = os.path.join(ROOT, "share", "dashboard.html")

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CWD_RE = re.compile(r"<cwd>(.*?)</cwd>", re.S)

_log_lock = threading.Lock()

# Set when the dashboard asks to stop testing. Probes check it before starting
# and any in-flight vendor process is killed, so "Stop" is immediate rather
# than "finish the current one first".
_CANCEL_TESTS = threading.Event()
_probe_procs = set()
_probe_lock = threading.Lock()


def cancel_tests():
    _CANCEL_TESTS.set()
    with _probe_lock:
        procs = list(_probe_procs)
    for proc in procs:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    return len(procs)


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg)
    with _log_lock:
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(line)
        except OSError:
            pass
    sys.stderr.write(line)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def _content_to_text(content):
    """Chat Completions content may be a string or a list of typed parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "input_text", "output_text"):
                    out.append(part.get("text") or "")
                elif part.get("type") in ("image_url", "input_image"):
                    out.append("[image omitted — the bridge is text-only]")
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(x for x in out if x)
    return str(content)


def extract_cwd(messages):
    """Codex injects <environment_context><cwd>...</cwd>. Run the vendor CLI there."""
    for msg in messages:
        text = _content_to_text(msg.get("content"))
        match = CWD_RE.search(text or "")
        if match:
            candidate = match.group(1).strip()
            if candidate and os.path.isdir(candidate):
                return candidate
    return None


def build_prompt(messages, forward_system):
    """Flatten the conversation into one prompt for a vendor agent.

    Codex's own system prompt describes Codex's tool protocol and would only
    confuse a different agent, so by default it is dropped rather than
    forwarded. The user/assistant turns are what actually carry intent.
    """
    parts = []
    turns = []
    for msg in messages:
        role = msg.get("role")
        text = _content_to_text(msg.get("content")).strip()
        if not text:
            continue
        if role == "system" or role == "developer":
            if forward_system:
                parts.append("# Context from the calling harness\n" + text)
            continue
        if role == "assistant":
            turns.append("## Previous assistant reply\n" + text)
        elif role == "tool":
            turns.append("## Tool result\n" + text)
        else:
            turns.append("## User\n" + text)

    if len(turns) == 1 and not parts:
        # Single-shot: send it verbatim so simple prompts stay simple.
        return turns[0][len("## User\n"):] if turns[0].startswith("## User\n") else turns[0]

    parts.extend(turns)
    return "\n\n".join(parts)



def build_prompt_responses(instructions, items):
    """Flatten a Responses-API `input` array into one prompt.

    `instructions` is Codex's own base prompt describing Codex's tool protocol;
    forwarding it would only confuse a different agent, so it is dropped and the
    conversation items are what get sent.
    """
    turns = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype in ("function_call", "reasoning"):
            continue
        if itype == "function_call_output":
            text = _content_to_text(item.get("output")).strip()
            if text:
                turns.append("## Tool result\n" + text)
            continue
        role = item.get("role", "user")
        text = _content_to_text(item.get("content")).strip()
        if not text or role in ("system", "developer"):
            continue
        turns.append(("## Previous assistant reply\n" if role == "assistant"
                      else "## User\n") + text)

    if len(turns) == 1 and turns[0].startswith("## User\n"):
        return turns[0][len("## User\n"):]
    return "\n\n".join(turns)


# --------------------------------------------------------------------------
# Vendor invocation
# --------------------------------------------------------------------------

def pick_variant(model, effort, fast):
    """Resolve one Codex effort/speed choice to a concrete vendor model id.

    Cursor encodes effort in the model name (grok-4.6-high, -xhigh-fast, ...),
    so one picker entry fans out to its whole family here.
    """
    variants = model.get("effort_variants") or {}
    if not variants:
        return model["id"]
    # Highest first, so an effort the vendor lacks (Codex offers max/ultra,
    # Cursor stops at xhigh) steps *down* to the closest real one rather than
    # silently landing on medium.
    order = ["ultra", "max", "xhigh", "high", "medium", "low"]
    chosen = effort if effort in variants else None
    if chosen is None:
        start = order.index(effort) if effort in order else order.index("medium")
        for cand in order[start:] + list(reversed(order[:start])):
            if cand in variants:
                chosen = cand
                break
    if chosen is None:
        return model["id"]
    concrete = variants[chosen]
    if fast and chosen in (model.get("fast_efforts") or []):
        concrete += model.get("fast_suffix", "-fast")
    return concrete


def _extract_json_result(text):
    """Some CLIs can emit a JSON envelope with usage alongside the reply.

    Returns (reply_text, tokens, cost) — falling back to the raw text when the
    output is not the envelope we expected, so a format change degrades to
    "no accounting" rather than to a broken reply.
    """
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return text, None, None
    if not isinstance(d, dict):
        return text, None, None
    reply = d.get("result")
    if not isinstance(reply, str):
        return text, None, None
    u = d.get("usage") or {}
    tokens = sum(int(u.get(k) or 0) for k in
                 ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"))
    return reply, (tokens or None), d.get("total_cost_usd")


def run_provider(spec, model, prompt, cwd, effort=None, fast=False):
    """Run a vendor CLI headlessly and return (text, error_or_None)."""
    argv = list(spec["cmd"])
    if isinstance(model, str):  # tolerate a bare id
        model = {"id": model}

    # Effort either selects a different vendor model, or rides on a flag.
    if model.get("efforts") and spec.get("effort_flag") and effort in model["efforts"]:
        # Some CLIs take a bare value (--reasoning-effort high), others a
        # key=value config override (-c model_reasoning_effort=high).
        template = spec.get("effort_template")
        argv.extend([spec["effort_flag"],
                     template.format(effort=effort) if template else effort])

    # Codex's "Fast" tier. Vendors that encode speed in the model name are
    # handled by pick_variant instead; this is for the flag-driven ones.
    if fast and model.get("fast") and spec.get("tier_flag"):
        tier_tpl = spec.get("tier_template", "{tier}")
        argv.extend([spec["tier_flag"], tier_tpl.format(tier=spec.get("fast_tier", "priority"))])
    model_id = pick_variant(model, effort, fast)

    # "default" means: let the vendor CLI use whatever model the account
    # is already set to, so we never override the user's own choice.
    if model_id and model_id != "default" and spec.get("model_flag"):
        argv.extend([spec["model_flag"], model_id])
    mode = spec.get("input", "stdin")
    timeout = int(spec.get("timeout", 900))

    env = dict(os.environ)
    for key, val in (spec.get("env") or {}).items():
        env[key] = val
    # Never let a stray key turn this into metered API usage.
    for banned in spec.get("unset_env", []):
        env.pop(banned, None)

    out_path = None
    if spec.get("output") == "file":
        out_path = os.path.join(ROOT, "logs", "out-%s.txt" % uuid.uuid4().hex[:12])
        argv.extend(spec.get("output_flag", ["-o"]))
        argv.append(out_path)

    tmp_path = None
    stdin_data = None
    if mode == "stdin":
        stdin_data = prompt.encode("utf-8")
    elif mode == "arg":
        argv.append(prompt)
    elif mode == "file":
        tmp_path = os.path.join(
            ROOT, "logs", "prompt-%s.txt" % uuid.uuid4().hex[:12]
        )
        with open(tmp_path, "w") as fh:
            fh.write(prompt)
        argv.extend(spec.get("file_flag", ["--prompt-file"]))
        argv.append(tmp_path)

    workdir = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")
    for i, token in enumerate(argv):
        argv[i] = token.replace("{cwd}", workdir)

    log("exec %s (cwd=%s)" % (shlex.join(argv), workdir))
    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return "", "could not start %r: %s" % (argv[0], exc)

    if spec.get("_is_probe"):
        with _probe_lock:
            _probe_procs.add(proc)

    try:
        out, err = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return "", "vendor CLI timed out after %ss" % timeout
    finally:
        if spec.get("_is_probe"):
            with _probe_lock:
                _probe_procs.discard(proc)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if out_path:
        try:
            with open(out_path) as fh:
                out = fh.read().encode("utf-8")
        except OSError:
            pass  # fall back to stdout below
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    text = ANSI_RE.sub("", (out or b"").decode("utf-8", "replace")).strip()
    errtext = ANSI_RE.sub("", (err or b"").decode("utf-8", "replace")).strip()
    tokens = cost = None
    if spec.get("output_json") and text:
        text, tokens, cost = _extract_json_result(text)
    log("done rc=%s in %.1fs, %d chars" % (proc.returncode, time.time() - started, len(text)))

    elapsed = time.time() - started
    pname = spec.get("_provider", spec.get("display_name", "?"))
    slug = spec.get("_slug", model.get("id", "?"))

    if proc.returncode != 0 and not text:
        msg = "vendor CLI exited %s: %s" % (proc.returncode, errtext[:800] or "(no stderr)")
        usage.record(pname, slug, False, elapsed, error=msg)
        return "", msg
    if not text:
        msg = "vendor CLI produced no output. stderr: %s" % (errtext[:800] or "(empty)")
        usage.record(pname, slug, False, elapsed, error=msg)
        return "", msg
    usage.record(pname, slug, True, elapsed, tokens, cost)
    return text, None


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "codex-subscription-bridge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    # -- helpers ----------------------------------------------------------
    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, message):
        self._send_json(code, {"error": {"message": message, "type": "bridge_error"}})

    # -- routes -----------------------------------------------------------
    def _guard_host(self):
        """Reject cross-origin/DNS-rebinding hits; this server is localhost-only."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host and host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self._send_error(403, "refusing request for host %r" % host)
            return False
        return True

    def do_GET(self):
        if not self._guard_host():
            return
        cfg = load_config()
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ("/", "/dashboard"):
            try:
                with open(DASHBOARD, "rb") as fh:
                    body = fh.read()
            except OSError as exc:
                self._send_error(500, "dashboard missing: %s" % exc)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/healthz", "/v1/healthz"):
            self._send_json(200, {
                "status": "ok",
                "enabled_models": [s for s, _p, _sp, _m in enabled_models(cfg)],
            })
            return

        if path == "/api/state":
            self._send_json(200, self._state(cfg))
            return

        if path == "/api/mode":
            self._send_json(200, self._mode_status())
            return

        if path == "/api/enabled":
            self._send_json(200, self._enabled_status())
            return

        if path == "/api/usage":
            snap = usage.snapshot()
            snap["opencode_stats"] = self._opencode_stats()
            self._send_json(200, snap)
            return

        if path == "/api/logs":
            try:
                with open(LOG_PATH) as fh:
                    lines = fh.readlines()[-200:]
            except OSError:
                lines = []
            self._send_json(200, {"lines": [ln.rstrip("\n") for ln in lines]})
            return

        if path in ("/v1/models", "/models"):
            now = int(time.time())
            self._send_json(200, {"object": "list", "data": [
                {"id": slug, "object": "model", "created": now,
                 "owned_by": cfg["providers"][pname].get("billing", "local")}
                for slug, pname, _spec, _m in enabled_models(cfg)
            ]})
            return

        self._send_error(404, "no such endpoint: %s" % self.path)

    def _opencode_stats(self):
        """OpenCode keeps its own cumulative ledger; ask it rather than guess."""
        cfg = load_config()
        spec = cfg["providers"].get("opencode") or {}
        binary = (spec.get("cmd") or [None])[0]
        if not binary:
            return None
        try:
            proc = subprocess.run([binary, "stats"], capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        out = ANSI_RE.sub("", proc.stdout or "")
        # Rows look like "│Total Cost                    $9.13 │" — one cell,
        # label and value separated by a run of spaces.
        wanted = ("Sessions", "Messages", "Days", "Total Cost",
                  "Input", "Output", "Avg Cost/Day")
        rows = {}
        for line in out.splitlines():
            m = re.match(r"^\s*\u2502\s*(.+?)\s{2,}([^\s\u2502]+)\s*\u2502\s*$", line)
            if m and m.group(1).strip() in wanted:
                rows[m.group(1).strip()] = m.group(2)
        return rows or None

    def _state(self, cfg):
        out = []
        for pname, spec in cfg["providers"].items():
            out.append({
                "provider": pname,
                "display_name": spec.get("display_name", pname),
                "billing": spec.get("billing", ""),
                "available": bool(spec.get("cmd") and spec["cmd"][0]),
                "models": [{
                    "id": m["id"],
                    "label": m.get("label", m["id"]),
                    "enabled": bool(m.get("enabled")),
                    "slug": common_slug(pname, m["id"]),
                    # Codex rejects dots in --profile, so the file name is flattened
                    "profile": profile_name(common_slug(pname, m["id"])),
                    # what Codex's reasoning selector will offer for this model
                    "efforts": [e for e in ("low", "medium", "high", "xhigh", "max", "ultra")
                                if e in (m.get("effort_variants") or m.get("efforts") or [])],
                    "fast": bool(m.get("fast_efforts")) or bool(m.get("fast")),
                    # set by the sweep when a vendor refused the model
                    "unavailable": m.get("unavailable"),
                } for m in spec.get("models", [])],
            })
        return {"providers": out, "catalog_path": os.path.join(ROOT, "etc", "model-catalog.json")}

    def do_POST(self):
        if not self._guard_host():
            return
        path = self.path.split("?")[0].rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError) as exc:
            self._send_error(400, "malformed request body: %s" % exc)
            return

        if path == "/api/state":
            self._save_state(body)
            return
        if path == "/api/apply":
            self._apply_catalog()
            return
        if path == "/api/refresh":
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "bin", "refresh-models.py")],
                capture_output=True, text=True, timeout=300)
            ok = proc.returncode == 0
            log("model refresh ok=%s" % ok)
            self._send_json(200 if ok else 500,
                            {"ok": ok, "output": (proc.stdout + proc.stderr).strip()})
            return
        if path == "/api/mode":
            self._set_mode(body)
            return
        if path == "/api/enabled":
            want = body.get("enabled")
            if want not in (True, False):
                self._send_error(400, "enabled must be true or false")
                return
            proc = subprocess.run(
                [sys.executable, os.path.join(ROOT, "bin", "set-enabled.py"),
                 "on" if want else "off"],
                capture_output=True, text=True, timeout=180)
            ok = proc.returncode == 0
            log("master switch -> %s (ok=%s)" % (want, ok))
            self._send_json(200 if ok else 500,
                            {"ok": ok, "output": (proc.stdout + proc.stderr).strip()})
            return

        if path == "/api/test/stop":
            killed = cancel_tests()
            log("testing stopped by dashboard (%d in flight killed)" % killed)
            self._send_json(200, {"ok": True, "killed": killed})
            return
        if path == "/api/test/start":
            _CANCEL_TESTS.clear()
            self._send_json(200, {"ok": True})
            return

        if path == "/api/usage/reset":
            usage.reset()
            self._send_json(200, {"ok": True})
            return
        if path == "/api/test":
            self._probe(body)
            return
        if path in ("/v1/probe-any", "/probe-any"):
            self._probe(body, any_model=True)
            return
        if path in ("/v1/chat/completions", "/chat/completions"):
            self._chat(body)
            return
        if path in ("/v1/responses", "/responses"):
            # `raw` is passed through unparsed so an OpenAI-owned model can be
            # relayed verbatim when relay_openai is enabled.
            self._responses(body, raw)
            return
        self._send_error(404, "no such endpoint: %s" % self.path)

    # -- dashboard actions ------------------------------------------------
    def _save_state(self, body):
        """body: {"selections": {"<provider>": ["<model id>", ...]}}"""
        cfg = load_config()
        selections = body.get("selections") or {}
        changed = 0
        for pname, spec in cfg["providers"].items():
            wanted = set(selections.get(pname, []))
            for model in spec.get("models", []):
                new = model["id"] in wanted
                if bool(model.get("enabled")) != new:
                    model["enabled"] = new
                    changed += 1
        save_config(cfg)
        log("dashboard saved selections (%d changes)" % changed)
        self._send_json(200, {"ok": True, "changed": changed,
                              "enabled": [s for s, _p, _sp, _m in enabled_models(cfg)]})

    def _apply_catalog(self):
        """Rebuild the catalog and re-sync the terminal profiles together, so
        the picker and `codex --profile` can never disagree."""
        out, ok = [], True
        for script in ("build-catalog.py", "sync-profiles.py"):
            proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", script)],
                                  capture_output=True, text=True, timeout=120)
            out.append((proc.stdout + proc.stderr).strip())
            ok = ok and proc.returncode == 0
        log("apply catalog+profiles ok=%s" % ok)
        self._send_json(200 if ok else 500,
                        {"ok": ok, "output": "\n".join(x for x in out if x)})

    def _probe(self, body, any_model=False):
        cfg = load_config()
        slug = body.get("model") or ""
        spec, model_def = resolve_model(cfg, slug)
        if spec is None and any_model:
            # Let the dashboard test a model before enabling it.
            from common import slug_for as _sf
            for pn, sp in cfg["providers"].items():
                for m in sp.get("models", []):
                    if _sf(pn, m["id"]) == slug:
                        spec, model_def = sp, m
        if spec is None:
            self._send_error(404, "unknown or disabled model %r" % slug)
            return
        if _CANCEL_TESTS.is_set():
            self._send_json(200, {"ok": False, "model": slug, "reply": "",
                                  "error": "testing stopped", "cancelled": True})
            return
        probe = dict(spec)
        probe["_is_probe"] = True
        probe["timeout"] = min(int(spec.get("timeout", 900)), 240)
        text, err = run_provider(probe, model_def, "Reply with exactly: BRIDGE_OK",
                                 None, body.get("effort"), bool(body.get("fast")))
        self._send_json(200, {"ok": err is None and "BRIDGE_OK" in text,
                              "model": slug, "reply": (text or "")[:400], "error": err})

    # -- chat completions -------------------------------------------------
    def _chat(self, body):
        cfg = load_config()
        model = body.get("model") or ""
        spec, model_def = resolve_model(cfg, model)
        if spec is None:
            known = ", ".join(s for s, _p, _sp, _m in enabled_models(cfg))
            self._send_error(404, "unknown model %r; enabled: %s" % (model, known))
            return

        messages = body.get("messages") or []
        cwd = extract_cwd(messages)
        prompt = build_prompt(messages, spec.get("forward_system", False))
        if not prompt.strip():
            self._send_error(400, "no usable prompt content in request")
            return

        text, err = run_provider(spec, model_def, prompt, cwd,
                                 body.get("reasoning_effort"),
                                 body.get("service_tier") in ("priority", "fast"))
        if err:
            text = ("The %s bridge could not complete this turn.\n\n%s"
                    % (spec.get("display_name", model), err))

        if body.get("stream"):
            self._stream_response(model, text)
        else:
            self._whole_response(model, text)



    # -- Codex app mode ---------------------------------------------------
    def _run_mode(self, *args):
        script = os.path.join(ROOT, "bin", "set-mode.py")
        return subprocess.run([sys.executable, script, *args],
                              capture_output=True, text=True, timeout=120)

    def _enabled_status(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "bin", "set-enabled.py"), "status"],
            capture_output=True, text=True, timeout=60)
        out = {}
        for line in (proc.stdout or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip().replace(" ", "_").replace("(", "").replace(")", "")] = v.strip()
        return out

    def _mode_status(self):
        proc = self._run_mode("status")
        out = {}
        for line in (proc.stdout or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip().replace(" ", "_")] = v.strip()
        return out

    def _set_mode(self, body):
        mode = body.get("mode")
        if mode not in ("gpt", "assistants"):
            self._send_error(400, "mode must be 'gpt' or 'assistants'")
            return
        args = [mode] + ([body["default_model"]] if body.get("default_model") else [])
        proc = self._run_mode(*args)
        ok = proc.returncode == 0
        log("set mode %s rc=%s" % (mode, proc.returncode))
        self._send_json(200 if ok else 500,
                        {"ok": ok, "output": (proc.stdout + proc.stderr).strip(),
                         "status": self._mode_status()})

    def _relay_or_error(self, path, raw):
        """A model the bridge does not own belongs to OpenAI, so relay it.

        Only active when `relay_openai` is true in etc/providers.json. With it
        off, an unknown model is a plain 404 and nothing leaves this machine
        except through a vendor CLI. See relay.py for what the relay does and
        does not do with the request's headers.
        """
        cfg = load_config()
        if not cfg.get("relay_openai"):
            known = ", ".join(s for s, _p, _sp, _m in enabled_models(cfg))
            self._send_error(404, "unknown model; enabled: %s" % known)
            return
        upstream = cfg.get("upstream_base_url", "https://chatgpt.com/backend-api/codex")
        tail = path.split("/v1/", 1)[-1] if "/v1/" in path else path.lstrip("/")
        relay_post(self, "%s/%s" % (upstream.rstrip("/"), tail), raw, log,
                   int(cfg.get("relay_timeout", 900)))

    # -- responses API (the wire protocol Codex actually speaks) -----------
    def _responses(self, body, raw=b""):
        cfg = load_config()
        model = body.get("model") or ""
        spec, model_def = resolve_model(cfg, model)
        if spec is None:
            self._relay_or_error(self.path, raw)
            return

        items = body.get("input")
        if isinstance(items, str):
            items = [{"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": items}]}]
        cwd = extract_cwd(items or [])
        prompt = build_prompt_responses(body.get("instructions"), items)
        if not prompt.strip():
            self._send_error(400, "no usable prompt content in request")
            return

        effort = ((body.get("reasoning") or {}).get("effort")
                  or body.get("reasoning_effort"))
        fast = body.get("service_tier") in ("priority", "fast")
        text, err = run_provider(spec, model_def, prompt, cwd, effort, fast)
        if err:
            text = ("The %s bridge could not complete this turn.\n\n%s"
                    % (spec.get("display_name", model), err))

        if body.get("stream"):
            self._responses_stream(model, text)
        else:
            self._send_json(200, self._response_obj(model, text, "completed"))

    def _response_obj(self, model, text, status, msg_id=None, with_output=True):
        obj = {
            "id": "resp_%s" % uuid.uuid4().hex[:24],
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": model,
            "output": [],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": max(1, len(text) // 4),
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": max(1, len(text) // 4),
            },
        }
        if with_output:
            obj["output"] = [{
                "id": msg_id or "msg_%s" % uuid.uuid4().hex[:24],
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }]
        return obj

    def _responses_stream(self, model, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        msg_id = "msg_%s" % uuid.uuid4().hex[:24]
        base = self._response_obj(model, text, "in_progress", msg_id, with_output=False)
        seq = [0]

        def emit(kind, payload):
            payload = dict(payload)
            payload["type"] = kind
            payload["sequence_number"] = seq[0]
            seq[0] += 1
            data = ("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload))).encode("utf-8")
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        try:
            emit("response.created", {"response": base})
            emit("response.in_progress", {"response": base})
            emit("response.output_item.added", {
                "output_index": 0,
                "item": {"id": msg_id, "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []}})
            emit("response.content_part.added", {
                "item_id": msg_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}})

            step = 400
            for i in range(0, len(text), step):
                emit("response.output_text.delta", {
                    "item_id": msg_id, "output_index": 0, "content_index": 0,
                    "delta": text[i:i + step]})

            emit("response.output_text.done", {
                "item_id": msg_id, "output_index": 0, "content_index": 0, "text": text})
            emit("response.content_part.done", {
                "item_id": msg_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}})
            emit("response.output_item.done", {
                "output_index": 0,
                "item": {"id": msg_id, "type": "message", "status": "completed",
                         "role": "assistant",
                         "content": [{"type": "output_text", "text": text,
                                      "annotations": []}]}})
            final = self._response_obj(model, text, "completed", msg_id)
            final["id"] = base["id"]
            emit("response.completed", {"response": final})

            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")

    # -- response shaping -------------------------------------------------
    def _usage(self, text):
        return {"prompt_tokens": 0, "completion_tokens": max(1, len(text) // 4),
                "total_tokens": max(1, len(text) // 4)}

    def _whole_response(self, model, text):
        self._send_json(200, {
            "id": "chatcmpl-%s" % uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": self._usage(text),
        })

    def _stream_response(self, model, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        cid = "chatcmpl-%s" % uuid.uuid4().hex[:24]
        created = int(time.time())

        def chunk(delta, finish=None):
            payload = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            data = ("data: %s\n\n" % json.dumps(payload)).encode("utf-8")
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        try:
            chunk({"role": "assistant", "content": ""})
            step = 400
            for i in range(0, len(text), step):
                chunk({"content": text[i:i + step]})
            chunk({}, finish="stop")
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(done), done))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")


def main():
    cfg = load_config()
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8848))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log("listening on http://%s:%d  dashboard=/  enabled=%s"
    % (host, port, [s for s, _p, _sp, _m in enabled_models(cfg)]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Transparent relay for OpenAI's own models.

Why this exists
---------------
Codex resolves `model_provider` once per conversation and the desktop picker
sends only a model *name*. So for the local assistants to appear in the picker,
the bridge has to be the provider for the whole conversation — GPT included.
Sending GPT through a translation layer would degrade it, so instead its
requests are relayed byte-for-byte to the very endpoint Codex would have called
itself. GPT stays fully native: real streaming, real tool calls, real diffs.

What this does and does not do with credentials
----------------------------------------------
Codex attaches its own ChatGPT authorization to the request. This module copies
the request's headers to the outbound call and copies the response back. It
does NOT read, parse, log, cache, persist, or transmit those values anywhere
other than the same OpenAI host Codex already talks to. Header values never
reach the log file — only the URL and byte counts are logged.

The user explicitly approved this relay. Removing it is one line: see
`_relay_or_error` in bridge.py, or just run `bridgectl mode gpt`.
"""

import http.client
import json
import os
import re
import time
import urllib.parse

# Connection-level headers that must not be forwarded verbatim.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}

CHUNK = 65536

# Response headers that describe remaining quota. Strictly a whitelist: it can
# never match `authorization` or any other credential-bearing header, and only
# these names/values are ever written to disk.
QUOTA_HEADER_RE = re.compile(
    r"^(x-codex-|x-)?(ratelimit|rate-limit|usage|quota)[a-z0-9_-]*$|"
    r"^x-codex-[a-z0-9_-]*(limit|used|remaining|reset|window)[a-z0-9_-]*$",
    re.I)

USAGE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "logs", "usage-openai.json")


def _record_quota(resp):
    """Persist any quota headers the upstream volunteered, nothing else."""
    found = {name: value for name, value in resp.getheaders()
             if QUOTA_HEADER_RE.match(name)}
    if not found:
        return
    found["_captured_at"] = int(time.time())
    try:
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(found, fh, indent=2)
        os.replace(tmp, USAGE_FILE)
    except OSError:
        pass


def relay_post(handler, url, body, log, timeout=900):
    """Relay one POST upstream and stream the response straight back.

    `handler` is the BaseHTTPRequestHandler serving the client. Nothing is
    buffered: SSE frames are forwarded as soon as they arrive, which is what
    keeps GPT's streaming identical to talking to OpenAI directly.
    """
    parts = urllib.parse.urlparse(url)
    target = parts.path + (("?" + parts.query) if parts.query else "")

    headers = {}
    for name, value in handler.headers.items():
        if name.lower() not in HOP_BY_HOP:
            headers[name] = value
    # Ask for an unencoded body so bytes can pass through untransformed.
    headers["Accept-Encoding"] = "identity"
    headers["Host"] = parts.netloc

    log("relay -> %s (%d bytes up)" % (url, len(body or b"")))

    conn = http.client.HTTPSConnection(parts.netloc, timeout=timeout)
    try:
        conn.request("POST", target, body=body, headers=headers)
        resp = conn.getresponse()
    except OSError as exc:
        try:
            conn.close()
        except OSError:
            pass
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        msg = b'{"error":{"message":"upstream unreachable","type":"bridge_error"}}'
        handler.send_header("Content-Length", str(len(msg)))
        handler.end_headers()
        handler.wfile.write(msg)
        log("relay failed: %s" % exc)
        return

    _record_quota(resp)

    try:
        handler.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() not in HOP_BY_HOP:
                handler.send_header(name, value)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()

        total = 0
        while True:
            # read1 returns as soon as any data is available, so streamed
            # events are not held back waiting for a full buffer.
            piece = resp.read1(CHUNK) if hasattr(resp, "read1") else resp.read(CHUNK)
            if not piece:
                break
            total += len(piece)
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            handler.wfile.flush()
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
        log("relay <- %s (%d bytes down)" % (resp.status, total))
    except (BrokenPipeError, ConnectionResetError):
        log("client disconnected during relay")
    finally:
        try:
            resp.close()
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

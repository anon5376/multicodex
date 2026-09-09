# multicodex

Use every coding assistant you already pay for from inside Codex — Claude,
Cursor, Grok, Muse, OpenCode — in one model picker, on their own
subscriptions. No API keys. No metered billing. GPT stays native.

<!-- Add a screenshot at docs/dashboard.png to show the dashboard here. -->

## Why

You are probably paying for several coding assistants and can only use one at a
time, in its own app. Codex has the better shell — projects, threads, plugins,
approvals — but only talks to OpenAI's models.

multicodex is a small local shim that makes the others available inside it.
Each request shells out to that vendor's own CLI, which authenticates with the
session you already have. Nothing here ever sees a credential.

## What you get

- **One picker.** Claude Opus, Cursor's models, Grok, Muse, OpenCode's free
  tier, and OpenAI's own models, side by side in the Codex app.
- **Reasoning levels that work.** Codex's effort selector maps to whatever each
  vendor actually supports — Claude's `--effort`, Grok's `--reasoning-effort`,
  and Cursor's per-effort model ids (so one entry covers its whole family
  instead of eight picker rows). The Fast tier too, where it exists.
- **Terminal profiles.** `codex exec --profile claude-opus "…"` — usable as
  workers, so an agent can spawn help on a *different* subscription.
- **A dashboard.** Pick models, test them, watch quota, start and stop.
- **A real off switch.** One toggle puts Codex back exactly as it was.

## How it works

```
Codex ──Responses API──► multicodex ──shells out──► claude -p
       (127.0.0.1:8848)                             cursor-agent -p
                          │                         grok --prompt-file
                          │                         muse exec
                          │                         opencode run
                          └──relays verbatim──────► OpenAI (GPT stays native)
```

Codex speaks OpenAI's Responses API. The other assistants speak "prompt in,
text out" and run their own agent loop, so the shim translates: it flattens the
conversation into a prompt, runs the vendor agent in the caller's working
directory, and returns the reply.

Because Codex resolves one provider per conversation and its picker sends only
a model *name*, having everything in one list means everything routes through
the shim — GPT included. Rather than translate GPT and degrade it, its requests
are **relayed byte-for-byte** to the same OpenAI endpoint Codex would have
called itself. Real streaming, real tool calls, real diffs.

### Credentials

- Every vendor CLI authenticates itself, exactly as when you run it by hand.
- The shim strips `ANTHROPIC_API_KEY`, `CURSOR_API_KEY`, `XAI_API_KEY`,
  `META_API_KEY` and friends from the child environment, so a stray key cannot
  silently turn subscription usage into metered API billing.
- `etc/providers.json` holds command templates only. No secrets, ever.
- **The relay is opt-in.** With it on, Codex's own authorization header is
  copied onto the outbound call to OpenAI and the response is copied back. It
  is never parsed, logged, or stored, and never goes anywhere else — but it
  does pass through this code, so decide deliberately. Off by default;
  `bridgectl mode gpt` removes the shim from the GPT path entirely.

## Install

Requires macOS or Linux, Python 3.9+, Codex, and at least one assistant CLI
you are already logged into.

```bash
git clone https://github.com/<you>/multicodex.git
cd multicodex
./install.sh
bin/bridgectl start
open http://127.0.0.1:8848/
```

The installer finds your CLIs, asks each what models it offers, and backs up
`~/.codex/config.toml` before touching anything. **Nothing appears in Codex
until you enable models in the dashboard and press Apply.**

Then `bin/bridgectl on` registers the provider, and you quit and reopen the
Codex app.

## Using it

**In the app** — pick any model from the picker. Codex remembers the model per
conversation, so start a new one to switch. The app reads config only at
launch, so quit and reopen after any change.

**In the terminal** — one profile per enabled model, working whether the app
side is on or off:

```bash
codex exec --profile claude-opus "explain this repo"
```

Profile names contain no dots: the model `grok-4.6` gets the profile
`grok-4-6`, because Codex rejects dotted `--profile` values.

## Dashboard

`http://127.0.0.1:8848/`

- **Models** — enable what you want. **Update** re-scans vendors for new
  models; **Test all** probes each one and marks what actually answers, with
  the vendor's own error on hover, and a **Stop** that kills the in-flight call
  immediately; **Apply to Codex** writes the catalog and syncs the profiles.
- **Codex app** — the master on/off switch.
- **Usage** — see below.
- **Service** — activity log and controls.

## Usage reporting

Two different things, deliberately not conflated:

**What the vendor reports** — your real remaining quota, with meters and a
countdown accurate to the second:

| Provider | Source |
|---|---|
| ChatGPT | quota headers, captured as the relay passes responses through |
| Claude | `cachedUsageUtilization` in `~/.claude.json` (5-hour and 7-day) |
| OpenCode | `opencode stats` |
| Cursor, Grok, Muse | nothing published — so nothing is shown |

Percentages render to two decimals but are only as precise as the vendor sends;
today OpenAI and Anthropic both report whole percent. Claude refreshes its cache
only from an interactive session, so a window whose reset time has passed is
greyed out and marked **stale** rather than drawn as if current.

**What the shim drove** — calls, failures, time, and where the CLI reports it,
tokens and cost. Traffic through the shim only, not your whole subscription.

## Turning it off

```bash
bin/bridgectl off
```

Removes every key and block it added from `~/.codex/config.toml`, deletes only
the profile files it created, and stops the service. Your own profiles and your
chosen default model are left alone. `bin/bridgectl on` puts it back.

```bash
bin/uninstall --dry-run   # show exactly what would change
bin/uninstall             # do it
```

Your untouched original config stays in `backups/<timestamp>/config.toml`.

## Commands

```bash
bridgectl on | off                 # master switch
bridgectl start | stop | restart | status
bridgectl logs [n]
bridgectl default [model]          # show or set Codex's default model
bridgectl mode gpt | assistants    # what the app picker offers
bridgectl enable-autostart         # launch at login (launchd, macOS)
```

## Known limits

- **Vendor caps are vendor caps.** If your Cursor plan has spent its premium
  allowance, those models return "You've hit your usage limit" and there is
  nothing this can do. Test all marks them so you do not pick a dead one.
- **A bridged model is an agent, not a model.** It does its own tool use in the
  working directory and returns one finished message; its intermediate tool
  calls are not streamed back into Codex. Only relayed OpenAI models behave
  natively.
- **macOS-first.** Autostart uses launchd. Everything else is portable, but
  Linux autostart is not written yet.
- Images are not forwarded to bridged models; they are text-only.

## Contributing

Adding a vendor means one entry in `bin/install.py` describing how to drive its
CLI headlessly, and a scanner in `bin/refresh-models.py` if it can list models.
Issues and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with OpenAI, Anthropic, Cursor, xAI, Meta or OpenCode. Use it
within each provider's terms of service.

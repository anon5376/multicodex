# Context for an orchestrating agent

Paste this (edited to match what you enabled) into the memory or system context
of whatever agent coordinates work on your machine, so it knows the extra
models exist and how they behave.

---

## Extra models in Codex (multicodex)

This machine runs a local bridge that makes other logged-in coding assistants
available inside Codex — in the desktop picker and as terminal profiles. All on
existing subscriptions: no API keys, no metered usage.

| Model | Profile | Effort | Fast | Billed to |
|---|---|---|---|---|
| Claude Opus | `claude-opus` | low–max | — | Claude subscription |
| Cursor Grok 4.6 | `cursor-grok-4-6` | low–xhigh | yes | Cursor subscription |
| Grok 4.6 | `grok-4-6` | low–high | — | xAI subscription |
| Muse | `muse` | low–xhigh | — | Meta account |
| (free tier) | `opencode-…-free` | — | — | free |

GPT is untouched and fully native.

These are usable as agents and workers — spawn them, message them, build a team
across subscriptions instead of only copies of yourself.

**They behave differently from a model.** Each bridge profile runs that vendor's
own CLI, which does its own tool use in the working directory and returns one
finished message. Give it a task, not a chat turn. Set cwd deliberately. You get
the final answer only — its intermediate tool calls aren't streamed back, so ask
it to state which files it changed. Calls are separate processes, so workers run
in parallel. 15-minute timeout each.

**Vendor caps apply.** If a subscription has spent its allowance those models
refuse the call. Check the dashboard before a long run.

**The bridge must be running** (`localhost:8848`). If it's off, every profile
above fails while native GPT keeps working.

**Don't add API keys** for these providers — the bridge strips them so a stray
key can't turn subscription use into metered billing.

Each provider has its own quota, so spreading bulk work off ChatGPT leaves that
quota for what needs it.

# herdr-model-quota

Herdr 0.8 plugin that shows the remaining weekly subscription capacity for
**ChatGPT Codex** and **Claude Max** in the focused workspace row. A normal
value looks like:

```text
Cx 6%!! · 2d17h | Cl 91% · 5d13h
```

`!` means below 20% remaining, `!!` means below 10%, and `~` marks stale data.
Claude's five-hour or Sonnet allowance appears only when it is tighter than the
overall weekly allowance or below 20%.

## Data sources and credential boundary

Codex is queried through the official local app-server over stdio JSON-RPC:

```text
codex app-server --listen stdio://
```

The plugin selects the rate-limit window near 10,080 minutes and rejects
API-key authentication rather than presenting it as subscription usage. It
does not read `auth.json` or Codex transcripts. Codex support is
credential-blind and cross-platform.

Claude Max usage comes from the bundled `claude_max_usage.py` helper in this
repository. **Claude support is experimental and macOS-only.** The helper is
the sole credential boundary: on macOS it reads the Claude Code OAuth token
from the login Keychain and calls
`https://api.anthropic.com/api/oauth/usage`. That endpoint is **undocumented
and unofficial**; it may change or disappear without notice. The main plugin
is credential-blind: it runs the helper as a bounded subprocess and receives
only the normalized `five_hour`, `seven_day`, and `seven_day_sonnet` windows.
No OAuth token or Keychain value is ever placed in argv, logs, caches, error
messages, or files. Where the helper cannot run (Linux, a locked Keychain, or
a signed-out session), Claude displays as `Cl n/a` while Codex keeps working.

Grok quota comes from the bundled `grok_usage.py` helper in the same shape.
It is the sole credential boundary for Grok: it reads the login key from the
nested `~/.grok/auth.json` shape (top-level auth-host keys; only the `key`
field of the first entry) and calls
`https://cli-chat-proxy.grok.com/v1/billing?format=credits`, printing only the
normalized weekly credits window from `config.creditUsagePercent` and
`config.currentPeriod.end`. A monthly period is an error, never a weekly
window. The key never appears in argv, logs, caches, or errors. The `Gk`
segment uses the same `!`/`!!`/`~` rules and 30-minute cadence as Claude with
its own atomic cache, shows `Gk n/a` when the helper cannot run, and appears
only when a Grok login exists on the machine.

## Refresh, caching, and failure behavior

Codex refreshes at most every five minutes. Claude refreshes at most every 30
minutes because Anthropic's usage endpoint rate-limits aggressively. A file
lock prevents concurrent Herdr events from duplicating either request.

Each source has a separate, atomically replaced normalized cache under
`HERDR_PLUGIN_STATE_DIR`. A source failure preserves its last value with `~`;
the other source remains live. Values older than six hours are discarded and
shown as `n/a`. A stale helper fallback retains its original age rather than
being made artificially fresh.

The `refresh` action bypasses both intervals. The Codex exchange has a
15-second absolute deadline; the helper subprocess has a 12-second bound, and
inside it the Keychain read and the HTTP request each have a five-second
timeout. Raw source responses and errors are not logged.

## Herdr behavior and configuration

The plugin publishes `$model_quota` only to the focused workspace and clears
it from every other workspace. Herdr 0.8 has no global header or footer plugin
slot, so the clearest available surface is a dedicated line beneath the active
workspace. A 36-column minimum keeps both providers visible:

```toml
[ui]
sidebar_width = 36
sidebar_min_width = 36

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["$model_quota"],
  ["branch", "git_status"],
]
```

## Model-class routing

`route <class>` picks a lane for a new agent tab from the normalized caches
(refreshing them first if stale). Lanes are defined in `classes.toml` beside
the manifest; `medium` is Sol on the Codex subscription with Grok 4.6 on
SuperGrok as the fallback. For each lane it computes
`health = (remaining/100) / ((resets_at - now) / window_seconds)` and picks
the first lane in order with `health >= 1` and `remaining >= 20`; otherwise
the lane with the highest health; a lane whose quota is `n/a` ranks last;
ties keep order. `--explain` prints one line per lane plus the pick and shows
it as a Herdr notification; `--launch` additionally creates the tab, echoes
the rationale line into the new pane, and starts the agent. Routing happens
once at launch; nothing re-routes a running pane.

Kill rule: if Herdr's plugin action log shows no `route` invocation in the
two weeks after landing, delete the route action.

## Installation

Requirements: Python 3.11+, Herdr 0.8+, and `codex` on `PATH`. Claude support
additionally requires macOS with the Claude Code CLI signed in. Grok support
additionally requires a Grok CLI login under `~/.grok`.

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-model-quota --ref v2.2.0
```

Then add the sidebar configuration above, run `herdr server reload-config`, and
populate both values with
`herdr plugin action invoke terry.herdr-model-quota.refresh`.

Track `main` only if you accept unreleased changes. For local development,
clone the repository and use `herdr plugin link /path/to/herdr-model-quota`
instead of installing.

## Rollback

Run `herdr plugin action invoke terry.herdr-model-quota.clear`, then
`herdr plugin uninstall terry.herdr-model-quota` (or `herdr plugin unlink` for a
linked checkout) and reload Herdr. The normalized
cache remains under the Herdr-managed plugin state directory and contains no
credentials; deleting that directory removes every trace.

## Limitations

- The Anthropic usage endpoint is unofficial and may break at any time; Claude
  numbers are best-effort and experimental.
- Claude usage requires macOS because the token lives in the macOS Keychain.
- Codex, Claude, and Grok are the only supported providers, and only
  subscription (non-API-key) plans are reported.

See `SECURITY.md` for the trust model and reporting channels.

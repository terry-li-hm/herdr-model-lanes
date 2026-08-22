# herdr-model-lanes

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

GLM (Z.ai / bigmodel coding plan) quota comes from the bundled `glm_usage.py`
helper in the same shape. It is the sole credential boundary for GLM: the key
is looked up from `ZHIPU_API_KEY`, `ZHIPUAI_API_KEY`, `ZAI_API_KEY`, `ZAI_KEY`,
`BIGMODEL_API_KEY`, or `GLM_API_KEY` in the environment, then
`~/.pi/agent/models.json` (`providers["bigmodel-coding"]["apiKey"]`, a literal
key; `$`-prefixed references are ignored), then the first line of
`~/.config/zhipu/api_key` or `~/.config/bigmodel/api_key`. The helper calls
`GET {base}/api/monitor/usage/quota/limit` with `Authorization: Bearer <key>`
and prints only the normalized five-hour token window (the `TOKENS_LIMIT`
entry with the nearest reset). The base is `https://open.bigmodel.cn` when the
key came from a Zhipu/BigModel source and `https://api.z.ai` otherwise,
overridable with `MODEL_LANES_GLM_BASE`. No key is ever stored, logged, or
cached. The `Gl` segment uses the same `!`/`!!`/`~` rules, refreshes at a
five-minute cadence (the window is five hours), shows `Gl n/a` when no key is
available, and the router judges it on a five-hour pace, not a weekly one.

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
the manifest. Two classes exist:

- **`high`** — Fable 5 (`claude --model claude-fable-5`, quota claude), then
  Sol 5.6 (`pi` on `openai-codex`/`gpt-5.6-sol`, quota codex). Fable keeps
  the Claude Max window as its headroom.
- **`medium`** — Sol 5.6, Grok 4.6 (grok), Opus 5 (`claude --model
  claude-opus-5`, quota claude), GLM-5.3 (`pi` on `bigmodel-coding`, quota
  glm, not classified-safe), Cursor Agent (cursor, quota cursor). Opus sits
  after Sol and Grok because Fable and Opus share the one Claude Max window;
  medium spends the independent pools first so high keeps its headroom.

For each lane the router computes
`health = (remaining/100) / ((resets_at - now) / window_seconds)` and picks:

1. among candidates (`health >= 1`, `remaining >= 20`), the highest-health
   lane whose reset is within `SURPLUS_RESET_WINDOW_SECS` (48h) and whose
   health is at least `SURPLUS_HEALTH` (2.0) — spend headroom that expires
   with the window ("surplus before reset");
2. otherwise the first candidate in class order ("first healthy lane in
   order");
3. otherwise the lane with the highest health ("least unhealthy lane").

A lane whose quota has no reader (`cursor`) is `n/a` and ranks last,
so Cursor is the last resort and never chosen while any other lane is
healthy. `--explain` prints one line per lane plus the pick and shows it as
a Herdr notification (non-fatal without Herdr); `--launch` additionally
creates the tab, echoes the rationale line into the new pane, and starts
the agent; `--argv` prints the chosen lane's shell-quoted command on stdout
with the rationale on stderr and needs no Herdr environment. Routing
happens once at launch; nothing re-routes a running pane.

### `ag`

`bin/ag` wraps `route --argv` for the current shell: `ag [class]
[--explain] [--classified]` (default class `medium`) prints the rationale
and `exec`s the chosen lane in the current pane — no new tab, own cwd,
Herdr detects the agent normally. It sets
`HERDR_PLUGIN_STATE_DIR` to
`${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/terry.herdr-model-lanes`
when unset. Install it as a symlink so updates are picked up:

```bash
ln -s "$PWD/bin/ag" ~/.local/bin/ag
```

Kill rule: if Herdr's plugin action log shows no `route` invocation in the
two weeks after landing, delete the route action.

## Installation

Requirements: Python 3.11+, Herdr 0.8+, and `codex` on `PATH`. Claude support
additionally requires macOS with the Claude Code CLI signed in. Grok support
additionally requires a Grok CLI login under `~/.grok`. GLM support
additionally requires a Z.ai/BigModel API key from one of the sources above.

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-model-lanes --ref v3.2.0
```

Then add the sidebar configuration above, run `herdr server reload-config`, and
populate both values with
`herdr plugin action invoke terry.herdr-model-lanes.refresh`.

Track `main` only if you accept unreleased changes. For local development,
clone the repository and use `herdr plugin link /path/to/herdr-model-lanes`
instead of installing.

## Rollback

Run `herdr plugin action invoke terry.herdr-model-lanes.clear`, then
`herdr plugin uninstall terry.herdr-model-lanes` (or `herdr plugin unlink` for a
linked checkout) and reload Herdr. The normalized
cache remains under the Herdr-managed plugin state directory and contains no
credentials; deleting that directory removes every trace.

## Limitations

- The Anthropic usage endpoint is unofficial and may break at any time; Claude
  numbers are best-effort and experimental.
- Claude usage requires macOS because the token lives in the macOS Keychain.
- Codex, Claude, and Grok are the only supported providers, and only
  subscription (non-API-key) plans are reported. GLM and Cursor lanes have
  no quota reader yet, so they route as `n/a` until readers land.

See `SECURITY.md` for the trust model and reporting channels.

`ag` pauses `AG_DELAY` seconds (default 3) before exec so the rationale can be read, renames the Herdr pane to `<class>: <lane>`, and shows the pick as a Herdr notification when run inside Herdr.

## Using `ag` without Herdr

Herdr is only the front end. The quota readers talk to Codex, Anthropic and Grok
directly, lane selection is a pure function over their caches, and `ag` makes no
Herdr call unless `HERDR_PANE_ID` is set. In any shell:

```sh
git clone https://github.com/terry-li-hm/herdr-model-lanes ~/code/herdr-model-lanes
ln -s ~/code/herdr-model-lanes/bin/ag ~/.local/bin/ag
export MODEL_LANES_STATE_DIR=~/.local/state/model-lanes   # optional; any writable dir
ag --explain
ag            # execs the chosen lane in this shell
```

The Claude Max reader is macOS-only (Keychain); on Linux the Claude lanes show
`n/a` and rank last, while Codex and Grok lanes route normally.

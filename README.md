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
`BIGMODEL_API_KEY`, or `GLM_API_KEY` in the process environment, then the same
names in `~/.env.resolved`, then `~/.pi/agent/models.json`
(`providers["bigmodel-coding"]["apiKey"]`; `$`-prefixed env refs and
`!`-prefixed sialyl wrappers are ignored), then the first line of
`~/.config/zhipu/api_key` or `~/.config/bigmodel/api_key`. The helper calls
`GET {base}/api/monitor/usage/quota/limit` with `Authorization: Bearer <key>`
and prints only the normalized five-hour token window (the `TOKENS_LIMIT`
entry with the nearest reset). The base is `https://open.bigmodel.cn` when the
key came from a Zhipu/BigModel source and `https://api.z.ai` otherwise,
overridable with `MODEL_LANES_GLM_BASE`. No key is ever stored, logged, or
cached. The `Gl` segment uses the same `!`/`!!`/`~` rules, refreshes at a
five-minute cadence (the window is five hours), shows `Gl n/a` when no key is
available, and the router judges it on a five-hour pace, not a weekly one.

Antigravity (Google `agy` CLI) quota comes from the bundled
`antigravity_usage.py` helper in the same shape. It is the sole credential
boundary for Antigravity: it discovers a same-user Antigravity.app
`language_server` (preferred) or a running `agy` process from `ps`/`lsof`,
reads the CSRF token from that process's argv when present, and POSTs
`RetrieveUserQuotaSummary` to `127.0.0.1` only. The CSRF token never appears
in argv constructed by this plugin, logs, caches, or errors. Self-signed TLS
is accepted only for that loopback host. The helper does **not** spawn `agy`
or the desktop app; if neither is already running, Antigravity displays as
`Ag n/a`. The `Ag` segment uses the same `!`/`!!`/`~` rules and 30-minute
cadence as Grok, and the router uses the tightest Gemini bucket (weekly, or
five-hour when that is more constrained). Claude/GPT buckets on the same
plan are not the routing window because a default `agy` session spends Gemini.

Kimi Code quota comes from the bundled `kimi_usage.py` helper in the same
shape. It is the sole credential boundary for Kimi Code: the key is looked
up from `KIMI_CODE_API_KEY` in the process environment, then
`~/.env.resolved`, then a still-fresh `access_token` in
`~/.kimi-code/credentials/kimi-code.json`. Moonshot Open Platform keys
(`MOONSHOT_API_KEY`, `KIMI_API_KEY`) are a different product and are
ignored. The helper calls `GET https://api.kimi.com/coding/v1/usages` and
prints only the tightest coding window (weekly membership, or the five-hour
rate limit when that remaining fraction is lower). It never reads or uses
the CLI refresh token, and the key never appears in argv, logs, caches, or
errors. The `Km` segment uses the same `!`/`!!`/`~` rules and a five-minute
cadence because a five-hour window can be the routing meter.

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
- **`medium`** — Sol 5.6, Opus 5 (`claude --model claude-opus-5`, quota
  claude), GLM-5.3 (`pi` on `bigmodel-coding`, quota glm, not
  classified-safe), Grok 4.6 (grok), Antigravity (`agy`, quota antigravity, not
  classified-safe), Kimi Code (`kimi`, quota kimi, not classified-safe),
  Cursor Agent (cursor, quota cursor). The order follows Terminal-Bench 3.0
  and vendor cards (Opus 5 42.7 vs Grok 4.6 26.5; GLM-5.3 28–32); Fable and
  Opus share the one Claude Max window, so the health gate, not order,
  protects `high`: Opus is picked only while Claude is on pace and above
  20%. `agy` and `kimi` are in the picker so unused Google and Kimi Code
  quota can be spent by confirmation or number override even when an earlier
  lane is healthy.

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

`bin/ag` is a thin argparse wrapper around `herdr_model_lanes.py ag`.
`ag --help` is the contract. Default class is `medium`. It prints each
lane's quota, stars the suggestion, and execs the chosen agent in this
shell — no new tab, own cwd, Herdr detects the agent normally.

```text
ag              # picker, then exec
ag high         # Fable or Sol
ag --classified # hide glm, agy, kimi
ag medium -y    # skip the prompt
```

Enter starts the star, a number starts that lane, `q` quits. After
`AG_TIMEOUT` seconds (default 10) it starts the suggestion. `AG_YES=1`
skips the prompt. It sets `HERDR_PLUGIN_STATE_DIR` to
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
Antigravity support additionally requires Antigravity.app or `agy` already
running so the loopback quota server can be probed. Kimi Code support
additionally requires `KIMI_CODE_API_KEY` or a signed-in Kimi Code CLI.

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-model-lanes --ref v3.2.1
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
- Codex, Claude, Grok, GLM, Antigravity, and Kimi Code are the supported
  quota providers. The Cursor lane still has no reader, so it routes as
  `n/a` until one lands. Antigravity quota is available only while
  Antigravity.app or `agy` is already running. Kimi Code uses the coding
  membership API, not Moonshot Open Platform balance.

See `SECURITY.md` for the trust model and reporting channels.

`ag` prints each lane's quota with the suggested lane marked `*`, then waits: Enter (or `AG_TIMEOUT` seconds of silence, default 10) starts the suggestion, a number starts that lane instead, `q` quits; `ag -y` (or `AG_YES=1`) skips the prompt. Inside Herdr it also renames the pane to `<class>: <lane>` and shows the pick as a notification.

## Using `ag` without Herdr

Herdr is only the front end. The quota readers talk to Codex, Anthropic, Grok, GLM, and a local
Antigravity server
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
`n/a` and rank last, while Codex, Grok, GLM, and Antigravity lanes route
normally when their sources are present.

Surplus applies only to windows longer than 48 hours; a five-hour window (GLM) always resets soon and refills anyway, so it never overrides class order on surplus alone.

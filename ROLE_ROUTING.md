# Launch a role through CLIProxyAPI

`bin/ag-role` selects once, before starting an ordinary native Claude or Codex
CLI. It reads the requested role's ordered routes, skips unqualified/disallowed
routes, and chooses the first route with an eligible proxy account above the
configured remaining-capacity floor. Surplus never promotes a later route.
An exhausted chain exits **75 without creating a tab or process**. There is no
session switching, resume, context handoff or scheduler.

The original `route` and `ag` class picker keep their existing behavior. Use the
new `role` command when strict priority and proxy-account capacity are required.

## Configuration

Python 3.11+ and the standard library are sufficient. Start with
[examples/roles.toml](examples/roles.toml), kept outside the checkout. Point
`policy_file` at an existing role JSON document, or use the small
[example policy](examples/role-policy.json). Example models illustrate the schema;
choose your allowed, qualified models. No user-specific policy ships as a default.

Each role has `primary`, ordered `fallback`, and optional `maxTier`. A role can
instead reference an entry in `policies` with `policy`. Routes require
`qualified: true`; increasing tier numbers represent lower tiers. Later routes
cannot upgrade the previous allowed tier. Native routes have `kind`, `model`,
optional `effort`, and optional additive `args`.

Existing studio-style route documents work directly: `claude_local` maps to
native Claude; `adapterConfig.provider: codex` maps to native Codex.
`adapterConfig.model`, `effort` and `reasoningEffort` are read. Existing
`credentialPool` labels are **not** treated as separate quota pools. Adapter
lifecycle/permission fields are not imported into ordinary Herdr agents. Native
launchers retain their normal permission and sandbox settings; explicit additive
permission flags can be set under `launchers.KIND.args`.

`launchers.KIND.command` contains one executable, preferably the absolute path to
an established managed launcher. It must route inference through the same proxy
and pool queried by `[proxy]`. Set `proxy_routed = true` after checking that
contract. This does not configure credentials, rewrite native homes, pin an
account, or bypass the proxy's rotation. Custom client-key account restrictions
not exposed in `auth-files/models` are unsupported: do not attest a broader
management pool than the launcher can use. Model aliases must expose the same
quota semantics as their native model; prefer canonical model IDs.

The management key comes from the named environment variable or a protected file,
never a command flag. A file must be regular, owned by the current user, mode
0600, and not a symlink. A dotenv file is parsed, never sourced. Do not put the
key in TOML, shell history, plugin manifests or a checked-in file. HTTPS is
required except for loopback HTTP. Redirects are refused.

## Ordinary Herdr callers

From a normal shell in the intended working directory:

```sh
/path/to/herdr-model-lanes/bin/ag-role reviewer --config /path/to/roles.toml --explain
/path/to/herdr-model-lanes/bin/ag-role reviewer --config /path/to/roles.toml --exec
```

To create a new ordinary Herdr tab from a coordinator or another pane:

```sh
/path/to/herdr-model-lanes/bin/ag-role reviewer \
  --config /path/to/roles.toml --launch --workspace w1 \
  --cwd /path/to/project --prompt-file /path/to/task.txt
```

Discover the target workspace with `herdr workspace list`; the example `w1` is
not a default. `--launch` requires an explicit workspace, creates one unfocused
tab in the supplied cwd, and runs the exact managed executable with shell-quoted
arguments. It leaves the tab's generated label alone so Herdr's native detection,
hooks, and conversation/activity-title plugins retain ownership. Success reports
the created pane/tab IDs and means the command was submitted, not that the CLI
became ready or executed a task. Inspect the returned pane if startup fails.
Never automatically retry a partially completed launch.

`--plan` emits the sanitized decision plus a `plan` object with route name, native
kind, model, managed command and native argument array, without any prompt. A
project lifecycle adapter can validate this against its allowed native profiles
before starting a session. Selection itself never creates a session.

`--exec` replaces the current process, preserving cwd/environment/stdin. `--argv`
emits a JSON argv array for an existing creation caller that already owns pane
creation. Unlike explain output, it includes the supplied task and instructions;
keep it out of logs. The same APIs can serve future project coordinators without
a dependency on herdr-projects.

There is **no interception hook** in this plugin for Herdr's built-in new-agent
menu or `herdr agent start --kind ...`. Those direct concrete-kind launches,
existing `ag`/`route` actions, and project coordinators that have not adopted this
entry point remain unwired. To adopt it, replace the caller's choice-and-start
step with `ag-role ROLE --launch ...`, or use `--argv` before its existing launch
step. No global binary replacements or Herdr server restart are required.

## Instructions and arguments

`instructions_root` reads `ROLE/AGENTS.md` from the existing role tree at launch.
Repeat `--instructions-file` for additional instruction files. Contents are
preserved in the initial native prompt, with absolute source paths and the base
for relative references, followed by the unchanged task. This retains native
project instruction discovery; it does not copy files, change native system
prompts or infer role names from cwd. Without instruction files the task is
passed verbatim. `--prompt-file` avoids shell escaping and shell-history copies.

Native syntax differs. `--args-file` accepts a JSON object such as:

```json
{"codex": ["--sandbox", "workspace-write"], "claude": ["--permission-mode", "default"]}
```

The selected runtime's arguments are passed in order. Supported additive flags
are `--sandbox`/`-s`, `--ask-for-approval`/`-a`, `--permission-mode`, `--add-dir`,
`--allowedTools`, `--disallowedTools`, `--no-alt-screen`, `--verbose`, `--debug`.
Use repeated flag/value pairs for lists. Other native flags are rejected rather
than silently dropping them. Model/effort come from the route; prompt and
instructions have dedicated fields. Resume, secondary positional prompts,
endpoint/provider overrides and bypass flags are not accepted.

## Capacity decisions and limits

The adapter reads `GET /v0/management/auth-files`, each enabled account's
`GET /auth-files/models?name=...`, and uses `POST /api-call` with an upstream
**GET** to the official Claude OAuth usage or ChatGPT wham usage endpoint.
`$TOKEN$` substitution stays inside the proxy. The proxy remains token-refresh
owner. These unofficial quota endpoints can change; shape failures close the
lane. No inference, usage queue, cumulative token counts, reset-quota mutation,
paid credits or local native OAuth files are used.

Account eligibility uses exact registry model IDs, disabled state, and current
credential/model cooldowns. Known quota cooldowns block until their reset;
authentication errors are separately reported. Legacy APIs without structured
cooldowns report generic unavailable status rather than guessed exhaustion.
One depleted account does not block a model supported by another usable account.

All Claude shared windows constrain every model; named Sonnet/Opus windows apply
to their model family. Other nonempty quota windows conservatively constrain all
models. Codex shared primary/secondary windows, additional limits, and
`model_usage.available` are checked. Explicit additional `gpt-*` limit names
match that model; other limit names conservatively constrain all models.
Missing/malformed mandatory windows, expired observations and failed reads are
unknown, never zero usage. An unknown window cannot erase a still-valid depleted
window. Another model cannot escape a shared exhausted pool.

Every relevant window must have **more than** `headroom_percent` remaining.
Default is 5%; zero still rejects actual depletion. `unknown_capacity = "skip"`
skips unknown/stale/unavailable lanes and tries the next allowed route;
`"hold"` stops at the first such lane. Neither policy launches an unknown lane.
Authentication errors never count as capacity, and appear separately in explain
output. Exit 2 indicates a configuration/read/launch error; 75 means no eligible
route. Explain is JSON with anonymous account states and epoch reset hints.
Recheck at reset or fix the reported failure; do not loop spawning agents.

Quota/account snapshots are advisory, not reservations. Concurrent agents can
consume headroom after selection; proxy routing remains authoritative and may
retry an account that only the usage endpoint knows is depleted. No account
selection is injected into inference. `retry_at` is a recheck hint, not a promise
of restored capacity.

Requests use a 4-second default timeout and a 20-second scheduling budget; at
most 64 account records are accepted. Each enabled relevant account is queried
once per invocation, regardless of how many roles/models share its pool. There
are no automatic retries. An optional atomic mode-0600 lane cache stores only
anonymous normalized capacity, keyed by source/config/requested model set, for
`cache_seconds` (default 0, capped at 60). Leave it off: it would hide a proxy
cooldown set since the previous launch. Reset boundaries invalidate it.
`--refresh` bypasses it. Failed refreshes replace rather than revive old data.

Provider usage endpoints throttle quickly (Anthropic returns 429 for minutes
after a dozen reads), so each account's usage reading is cached separately and
shared by every role. It is re-read after `usage_cache_seconds` (default 1800)
while more than 50% is left, a third of that from 20%, and a sixth below 20%
(30/10/5 minutes by default). Concurrent launches take a per-account lock, so
one refreshes and the rest reuse its reading. `usage_stale_seconds` (default
3600) of fallback covers a re-read that is throttled or unreachable, never past
a window reset and never over an auth error. Proxy cooldowns are read live on
every launch, so a session that hit its limit through the proxy makes the next
launch skip that account immediately; the usage reading only has to catch usage
the proxy never sees (apps, chat, unproxied CLIs). After a manual reset
(credits added, limits restored), run any role with `--refresh`; it ignores
every cached reading.
This cache bounds normal repeated launch queries; it is not a distributed lock
or a reservation service. Set `MODEL_LANES_STATE_DIR` to override its directory.

## Verification

```sh
python3 -m unittest discover -s tests -v
ruff check herdr_model_lanes.py claude_max_usage.py proxy_capacity.py role_launch.py tests
ruff format --check herdr_model_lanes.py claude_max_usage.py proxy_capacity.py role_launch.py tests
```

Deterministic tests cover priority, provider fallback, multiple accounts,
shared/model windows, cooldowns/authentication, stale/failed reads, cache reset
boundaries, all-exhausted refusal, policy reuse, arguments, instructions, cwd and
shell quoting. Live read-only checks should use `--explain --refresh`; do not
launch a paid task just to test capacity. See [attribution](THIRD_PARTY_NOTICES.md).

## Manual holds

`holds_file` (relative to the config) lists manual holds. A hold names a
provider (`{"provider": "codex"}`), a model prefix (`{"provider": "claude",
"model": "claude-fable"}`) or one proxy account (`{"provider": "codex",
"account": "<auth-file name>"}`), with `until` as an epoch or `null` for
"until lifted". Provider and model holds skip lanes before any capacity read;
an account hold is never read and counts as `held`. A lane whose accounts are
all held is `held`. Both show in `--explain` with the hold and its `until`.
Expiry is lazy: a hold whose `until` has passed is ignored at the next
selection. A missing file means no holds. A malformed file is an error, not
"no holds". Holds only steer this selector. Native CLIs started directly
never read them.


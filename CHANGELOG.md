# Changelog

## 3.8.0 - 2026-08-25

### Added

- `ag usage` now includes the existing Codex weekly quota reader. The new
  read-only `--plan` projection classifies each provider as `unknown`,
  `conserve`, `on_pace`, or `surplus` and caps any pull-forward recommendation
  at one ready, verifiable task and one accepted artifact. Capacity does not
  create demand. Planning uses a 5% pace tolerance to absorb rounded provider
  data; routing thresholds are unchanged.

## 3.7.0 - 2026-08-22

### Added

- Cursor quota reader: the bundled `cursor_usage.py` helper is the sole
  credential boundary. It reads a still-fresh `cursorAuth/accessToken` from
  Cursor.app's local `state.vscdb`, calls `GET https://cursor.com/api/usage-summary`
  with a `WorkosCursorSessionToken=::<jwt>` cookie, and prints only the
  monthly plan window. The refresh token is never read. The existing `cursor`
  lane now ranks by remaining quota instead of always `n/a`.

## 3.6.0 - 2026-08-22

### Changed

- `ag` and the plugin CLI now use argparse (stdlib only, no Typer/Click
  dependency). `ag --help` documents the picker; `route`, `refresh`, and
  `clear` are real subcommands. The numbered picker moved into Python, and
  `bin/ag` is a thin exec into `herdr_model_lanes.py ag`. Starting a lane
  uses `os.execvp` instead of `eval exec`.

## 3.5.0 - 2026-08-22

### Added

- Kimi Code (`kimi`) quota reader: the bundled `kimi_usage.py` helper is the
  sole credential boundary. It reads `KIMI_CODE_API_KEY` from the process
  environment, then `~/.env.resolved`, then a still-fresh access token from
  `~/.kimi-code/credentials/kimi-code.json`, calls
  `GET https://api.kimi.com/coding/v1/usages`, and prints only the tightest
  coding window (weekly, or the five-hour rate limit when that is more
  constrained). Moonshot Open Platform keys are ignored. The helper never
  reads or uses the CLI refresh token. The `kimi` lane sits in `medium`
  after Antigravity and before Cursor, is not classified-safe, and `ag` can
  number-pick it to spend unused Kimi Code quota.

## 3.4.0 - 2026-08-22

### Added

- Antigravity (`agy`) quota reader: the bundled `antigravity_usage.py` helper is
  the sole credential boundary. It probes a same-user Antigravity.app
  `language_server` (or a running `agy` CLI) on loopback, POSTs
  `RetrieveUserQuotaSummary`, and prints only the tightest Gemini window. The
  CSRF token is read from process argv and never logged, cached, or echoed.
  The helper does not spawn `agy`; if the app and CLI are both closed, `Ag`
  shows `n/a`. The `agy` lane sits in `medium` after Grok and before Cursor,
  is not classified-safe, and `ag` can number-pick it to spend unused Google
  quota instead of leaving it on the table.

## 3.2.1 - 2026-08-22

### Fixed

- GLM key lookup skips Pi `models.json` values that start with `!` (sialyl
  wrappers) as well as `$` (env refs), and reads `~/.env.resolved` after the
  process environment so the coding-plan `ZHIPU_API_KEY` is used instead of a
  token that 500s on the quota API.

## 3.3.0 - 2026-08-22

`ag` shows each lane's quota with the suggestion marked and waits for Enter / a
number / q (auto-accept after `AG_TIMEOUT`, `-y` to skip); `route --lane NAME`
overrides the pick; the surplus-before-reset rule now applies only to windows
longer than 48 hours, so GLM's five-hour window no longer overrides class order.

## 3.2.0 - 2026-08-22

### Added

- GLM (Z.ai / bigmodel coding plan) quota reader: the bundled `glm_usage.py`
  helper is the sole credential boundary, reading the key from the `ZHIPU_API_KEY`/
  `ZHIPUAI_API_KEY`/`ZAI_API_KEY`/`ZAI_KEY`/`BIGMODEL_API_KEY`/`GLM_API_KEY`
  environment order, then `~/.pi/agent/models.json`, then the Zhipu/BigModel
  config key files, and printing only the normalized five-hour token window from
  `{base}/api/monitor/usage/quota/limit`. `QuotaWindow` gained an explicit
  `window_seconds` (weekly by default, five hours for GLM) so `_lane_health`
  judges each window on its own pace; the `Gl` segment refreshes at a five-minute
  cadence and the `glm` lane is routable when it is the healthy choice.

## 3.1.0 - 2026-08-22

`ag` and the readers work without Herdr: `MODEL_LANES_STATE_DIR` is honoured when
`HERDR_PLUGIN_STATE_DIR` is unset, and the README documents the standalone install.

## 3.0.0 - 2026-08-22

Renamed from `herdr-model-lanes` to `herdr-model-lanes`: the plugin id is now
`terry.herdr-model-lanes`, the module is `herdr_model_lanes.py`, and the state
directory moves with the id. This is a breaking change for existing installs:
uninstall `terry.herdr-model-lanes`, then install `terry-li-hm/herdr-model-lanes`.
Behaviour is unchanged from 2.3.x (three quota readers, two classes, `ag`).

## 2.3.0 - 2026-08-22

### Added

- `high` model class (Fable 5 on Claude Max, Sol 5.6 on Codex) and a
  five-lane `medium` class (Sol, Grok 4.6, Opus 5, GLM-5.3, Cursor Agent);
  Opus sits after Sol and Grok so medium spends independent pools before
  the Claude Max window that high reserves. Lanes with no quota reader
  (`glm`, `cursor`) are `n/a` and rank last.
- Surplus rule in `select_lane`: among healthy candidates, a lane with
  `health >= SURPLUS_HEALTH` (2.0) whose reset falls within
  `SURPLUS_RESET_WINDOW_SECS` (48h) is picked first — spend headroom that
  expires with the window.
- `route <class> --argv`: prints the chosen lane's shell-quoted command on
  stdout with the rationale on stderr, no Herdr environment required; the
  Herdr notification in the `--explain` path is now non-fatal.
- `bin/ag` launcher: `ag [class] [--explain] [--classified]` execs the
  chosen lane in the current pane; install with
  `ln -s "$PWD/bin/ag" ~/.local/bin/ag`.
- `route-high` plugin action.

## 2.2.0 - 2026-08-22

### Added

- Bundled `grok_usage.py` helper: the sole credential boundary for Grok,
  reading the login key from the nested `~/.grok/auth.json` shape and
  normalizing the Grok billing endpoint to one weekly credits window. A
  monthly period is an error, never shown as a weekly window.
- `Gk` segment in the workspace row with the same `!`/`!!`/`~` rules and
  30-minute cadence as Claude, its own atomic cache, and `Gk n/a` when the
  helper cannot run. Codex and Claude behavior is unchanged; the segment
  appears only when a Grok login exists.
- `route` subcommand with `classes.toml`: session-start model-class routing
  (first lane with health >= 1 and remaining >= 20, else highest health,
  `n/a` ranks last), `--explain`, `--launch`, and `--classified`.
- `route-medium` and `route-explain` plugin actions and a Grok data-source
  section, routing rule, and kill rule in the README.

## 2.1.0 - 2026-08-17

### Added

- Bundled `claude_max_usage.py` helper: the sole credential boundary for
  Claude Max usage, reading the Claude Code OAuth token from the macOS
  Keychain and normalizing the undocumented Anthropic OAuth usage endpoint to
  the `five_hour`, `seven_day`, and `seven_day_sonnet` windows.
- Apache-2.0 `LICENSE`, `SECURITY.md`, and a GitHub Actions verifier for
  Python 3.11 and 3.13.
- Documented installation as a pinned `herdr plugin install` release, and
  carried the Claude experimental, macOS-only, undocumented-endpoint caveat
  into the plugin manifest description so it reaches a marketplace card.

### Changed

- Claude Max usage no longer depends on any private helper repository; the
  plugin is self-contained. `query_claude`/`refresh` accept an injectable
  `claude_command` list (default: current Python executable plus the bundled
  helper resolved from `__file__`) instead of the previous private-helper
  binary parameter.
- Documentation now states plainly that the Anthropic usage endpoint is
  undocumented, that Claude support is experimental and macOS-only, and that
  Codex remains credential-blind and cross-platform.

### Fixed

- All cache, backoff, expiry, subprocess-bound, and focused-workspace
  publication behavior is preserved unchanged.

## 2.0.0

- Initial release: Codex and Claude Max weekly capacity in the focused
  workspace row, independent refresh intervals, atomic normalized caches,
  failure backoff with `~` staleness, and a six-hour display limit.

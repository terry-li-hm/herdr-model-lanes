# Changelog

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

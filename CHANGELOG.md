# Changelog

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

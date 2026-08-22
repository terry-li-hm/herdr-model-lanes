# Security policy

## Trust model and credential boundary

This plugin is credential-blind. `herdr_model_lanes.py` never reads the macOS
Keychain, never constructs an `Authorization` header, and never sees an OAuth
token. It queries the local Codex app-server over stdio and runs one bounded
helper subprocess for Claude usage.

`claude_max_usage.py` is the sole credential boundary. On macOS it:

- runs `/usr/bin/security find-generic-password -s "Claude Code-credentials"
  -w` directly, without a shell, with a timeout of at most five seconds;
- parses only `claudeAiOauth.accessToken` and `claudeAiOauth.expiresAt`;
- sends the token only in the `Authorization: Bearer` header of a single
  request to `https://api.anthropic.com/api/oauth/usage` with a five-second
  timeout, and refuses HTTP redirects so the header can never be forwarded to
  another host;
- prints only the normalized `five_hour`, `seven_day`, and `seven_day_sonnet`
  windows and discards every other response field.

The OAuth token is never placed in argv, logs, caches, stdout errors,
exception messages, or files. Error messages name failure types and exit
codes only; they never echo Keychain output, HTTP bodies, or tokens. Caches
under `HERDR_PLUGIN_STATE_DIR` contain normalized percentages and reset
timestamps only.

## Experimental, unofficial Claude endpoint

The Anthropic OAuth usage endpoint is undocumented. It is not a public,
versioned API and may change or stop working without notice. Claude support is
therefore experimental and macOS-only. If it breaks, the plugin degrades to
`Cl n/a` and keeps reporting Codex, which is official and cross-platform.

## Reporting a vulnerability

Please open a private security advisory on this repository (GitHub:
Security tab -> Report a vulnerability) rather than a public issue. Include
the affected commit and a reproduction. Do not include real OAuth tokens,
Keychain dumps, or account credentials in a report.

## Scope

No network calls are made except the two described above (local Codex
app-server stdio and the Anthropic usage endpoint). The plugin does not read
Codex transcripts, does not install dependencies, and uses only the Python
3.11+ standard library.

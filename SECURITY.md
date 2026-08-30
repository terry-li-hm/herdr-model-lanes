# Security policy

## Trust model and credential boundary

This plugin is credential-blind. `herdr_model_lanes.py` never reads the macOS
Keychain or the Linux Claude credentials file, never constructs an
`Authorization` header, and never sees an OAuth or CSRF token. It queries the
local Codex app-server over stdio and runs one
bounded helper subprocess per provider that needs credentials.

`claude_max_usage.py` is the sole credential boundary. Its primary source
is the documented Claude Code `statusLine` feed, captured by the bundled
`claude_statusline.py` collector: it reads at most 1 MiB of the status-line
JSON from stdin, validates the `five_hour` and `seven_day`
`used_percentage` values (0..100) and reset epochs (bounded to no more than
one year ahead), normalizes them to
`utilization`, and persists only `captured_at` plus
those two normalized windows to `claude-statusline.json` in the plugin state
directory (`HERDR_PLUGIN_STATE_DIR`, then `MODEL_LANES_STATE_DIR`, then
`XDG_STATE_HOME`/`~/.local/state` under
`herdr/plugins/terry.herdr-model-lanes`), written atomically with mode
`0600`. It never persists `session_id`, `model`, `cwd`, workspace,
transcript paths, or prompt data, and never echoes stdin in errors. When
arguments follow `--`, the original stdin bytes are forwarded to that exact
argv without a shell and with a short timeout; on failure the collector
renders its own concise status line. The helper reads this cache before any
credential or network access and uses it while `captured_at` is at most 30
minutes old and the weekly window is live; `--refresh` does not bypass a
valid cache.

When the cache is missing, stale, malformed, or expired, the helper falls
back to the undocumented OAuth endpoint. On macOS it:

- runs `/usr/bin/security find-generic-password -s "Claude Code-credentials"
  -w` directly, without a shell, with a timeout of at most five seconds;
- parses only `claudeAiOauth.accessToken` and `claudeAiOauth.expiresAt`;
- sends the token only in the `Authorization: Bearer` header of a single
  request to `https://api.anthropic.com/api/oauth/usage` with a five-second
  timeout, and refuses HTTP redirects so the header can never be forwarded to
  another host;
- prints only the normalized `five_hour`, `seven_day`, and `seven_day_sonnet`
  windows and discards every other response field.

On Linux, the same helper resolves `CLAUDE_CONFIG_DIR` (or `~/.claude`) and
reads `.credentials.json` instead of the Keychain. The read is opened without
following symlinks, bounded to 64 KiB, and refuses non-regular files, files
owned by another effective user, and files with group or world permission
bits. The payload, parser, expiry checks, single-request usage call, redirect
refusal, and normalized output are identical to macOS.

The OAuth token is never placed in argv, logs, caches, stdout errors,
exception messages, or files. Error messages name failure types and exit
codes only; they never echo Keychain output, HTTP bodies, or tokens. Caches
under `HERDR_PLUGIN_STATE_DIR` contain normalized percentages and reset
timestamps only.

## Experimental, unofficial Claude endpoint fallback

The Anthropic OAuth usage endpoint is undocumented. It is not a public,
versioned API and may change or stop working without notice. Claude support is
therefore experimental, available on macOS (Keychain) and Linux (credentials
file). If it breaks, the plugin degrades to
`Cl n/a` and keeps reporting Codex, which is official and cross-platform.

## Reporting a vulnerability

Please open a private security advisory on this repository (GitHub:
Security tab -> Report a vulnerability) rather than a public issue. Include
the affected commit and a reproduction. Do not include real OAuth tokens,
Keychain dumps, credential files, or account credentials in a report.

## Scope

The plugin does not read Codex transcripts, does not install dependencies,
and uses only the Python 3.11+ standard library. Network calls are limited
to the local Codex app-server, the Anthropic usage endpoint (Claude helper),
the Grok billing endpoint (Grok helper), the Z.ai/BigModel quota endpoint
(GLM helper), loopback `127.0.0.1` Connect RPC (Antigravity helper), and
`https://api.kimi.com/coding/v1/usages` (Kimi Code helper), and
`https://cursor.com/api/usage-summary` (Cursor helper). The Kimi helper
sends the key only as `Authorization: Bearer` and refuses redirects. It
never reads the CLI refresh token. The Cursor helper reads the access token
from Cursor.app's local SQLite state database and sends it only as a
`WorkosCursorSessionToken` cookie; it never reads the refresh token.
The Antigravity helper reads a CSRF token from a same-user process listing
and sends it only as `X-Codeium-Csrf-Token` to loopback; it never logs or
caches that token, and it refuses redirects and non-loopback hosts. Self-signed
TLS is accepted only for `127.0.0.1`.

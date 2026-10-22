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

## Opt-in quota handoff

`quota_handoff.py` is a separate Linux-only control feature. It is disabled
without explicit configuration and a workspace allowlist. It uses the existing
Claude credential helper with isolated per-account state and the official local
Codex app-server. It does not read credential files or print provider responses.

The handoff controller may inspect bounded terminal output and structured error
metadata from an exact Claude or Pi session file. It does not recursively search
transcript stores. Runtime receipts contain session and process identifiers,
phases and destination references, but no terminal text, transcript content or
credentials. Those private receipts have owner-only permissions and must stay
outside the public checkout.

Passive observation uses `enabled: false` and `observe_only: true`. It may
record corroborated, content-free quota evidence for configured sources, but it
cannot stop a process, create a tab, submit a prompt, change account exclusions
or send notifications. Unknown or contradictory mode flags fail closed. The
control flag stays false so older installations cannot activate accidentally.

Control requires a confirmed current usage-limit error and stable process
identity. The source process group is stopped before a replacement is created.
The controller never closes or automatically resumes the original. Ambiguous
mutations are quarantined. New sessions receive no task prompt by default;
protected or unknown work requires the owner's confirmation in the replacement.
An explicit session-and-task-bound public marker permits one Pi continuation
from a private owner-authored unclassified checkpoint. Native successors never
receive an automatic task prompt. Full transcripts are never forwarded. See
[quota handoff](QUOTA_HANDOFF.md)
for the operational boundary and recovery procedure.

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

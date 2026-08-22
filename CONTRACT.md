# Contract: credential-boundary review of herdr-model-lanes v3.3.0

Workspace: this worktree (branch qa/grok-security). Write only here. No network. Never print, log, or commit a real credential; do not read ~/.grok, ~/.pi, ~/.zcode, ~/.codex, the Keychain, or ~/.env*.

Scope: claude_max_usage.py, grok_usage.py, glm_usage.py, and the parts of herdr_model_lanes.py that invoke them and cache their output; bin/ag. Review ONLY for credential and data-boundary defects and prove each with a runnable test or repro:
1. Can a key or token reach argv, environment of a child, a log line, an exception message, stderr, a cache file, or the Herdr token/row/notification under any error path? Check every `raise`, `print`, f-string, and subprocess call; test error paths with fixture inputs containing a sentinel key like SENTINEL_SECRET_123 and grep all outputs and files for it.
2. File handling: cache files and lock files mode/umask; atomic replace; does any helper write anything other than normalized numbers? Does `ag`'s mktemp file ever contain a secret?
3. Response handling: 1 MiB cap enforced before json.loads; timeouts on every request; redirects to other hosts refused or harmless; non-JSON / huge / malicious JSON (deeply nested) does not crash the row publisher.
4. Key discovery: glm_usage reads ~/.pi/agent/models.json; confirm it reads only providers["bigmodel-coding"]["apiKey"], never other providers, and treats "$VAR" refs as missing. grok_usage reads only the `key` of ~/.grok/auth.json entries. claude helper: Keychain payload parsing never echoes the payload.
5. `ag`: eval exec quoting; AG_* env injection; lane names with shell metacharacters from classes.toml (user-editable) must not reach eval unquoted.

Output ./REPORT.md: Confirmed defects (repro + fix applied with regression test), Suspected (unconfirmed), Verified-safe list, verifier summary. Fix only confirmed defects; no refactors. Verifier:
  python3 -m unittest discover -s tests -v ; ruff check claude_max_usage.py grok_usage.py glm_usage.py herdr_model_lanes.py tests ; ruff format --check claude_max_usage.py grok_usage.py glm_usage.py herdr_model_lanes.py tests ; sh -n bin/ag
Commit when green (do not push). Write ./DONE last.

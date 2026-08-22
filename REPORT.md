# Credential-boundary review: herdr-model-lanes v3.3.0

Sentinel `SENTINEL_SECRET_123` was used on every error path. Confirmed leaks and crashes were fixed with tests in `tests/test_credential_boundary.py`.

## Confirmed defects

1. **Grok helper printed unbounded `period.end`.** `_parse_resets_at` only checked a timestamp prefix, so `2026-08-22T00:00:00Z` plus the sentinel was written to stdout. Fix: full ISO parse, 48-character cap, no echo. Regression: `test_timestamp_suffix_is_rejected_before_stdout`.

2. **Claude helper passed `utilization` and `resets_at` through.** A sentinel in either field was printed. Fix: require a 0–100 number and a short timezone-aware ISO timestamp. Regression: `test_window_passthrough_of_sentinel_is_rejected`, `test_fetch_rejects_sentinel_window_and_main_stays_clean`.

3. **1 MiB cap did not reject before `json.loads`.** Helpers `read(MAX)` then parsed, including truncated or padded bodies. Fix: `read(MAX+1)` and raise before parse. Regression: `test_oversized_body_is_rejected_before_json_loads` and the matching Claude/GLM cases.

4. **Nested JSON crashed the helpers.** 200,000 nested arrays (under 1 MiB) raised `RecursionError` from `json.loads`. Fix: catch it as invalid JSON. Regression: `test_nested_json_is_invalid_not_a_crash` and siblings.

5. **Non-UTF-8 helper stdout crashed the row publisher.** `subprocess.run(..., text=True)` raised `UnicodeDecodeError`; `_refresh_*` only caught `QuotaError`, so `refresh()` died. Fix: `_load_helper_payload` turns decode, size, and recursion failures into `QuotaError`. Regression: `test_non_utf8_helper_stdout_does_not_crash_refresh`, `test_nested_helper_stdout_is_quota_error_not_crash`, `test_huge_helper_stdout_is_rejected_before_json_loads`.

6. **`ag` left the mktemp rationale file after a successful start.** `eval exec` replaces the shell, so the EXIT trap did not run. Fix: `rm` the file and clear the trap before exec. Regression: `test_ag_eval_does_not_run_lane_name_or_ag_timeout_injection`.

## Suspected (unconfirmed)

- Claude and Grok helper subprocesses inherit the parent environment, so a GLM key already in the process env is visible to those children. It was not observed in argv, stderr, cache, or the Herdr token.
- Cache lock files are created `0o644` (umask) and are empty. Not a credential sink.
- Codex app-server JSON-RPC lines are not 1 MiB-capped. Outside this contract's helper scope.

## Verified-safe

- Token or key never placed in argv. `security` is invoked without a shell. Helper commands are interpreter plus module path.
- Errors name types, HTTP status, or helper rc only. `URLError` text that contains the sentinel is not forwarded. Redirect handlers refuse without echoing the destination.
- Every HTTP request has a 5s timeout. Helper subprocesses time out at 12s.
- `glm_usage` reads only `providers["bigmodel-coding"]["apiKey"]`. `"$VAR"` and `"!"` values are missing. Other providers' keys are ignored.
- `grok_usage` reads only the `key` field of `auth.json` entries.
- Keychain payload parse never echoes `accessToken`, `refreshToken`, or `security` stderr.
- Cache files are `0o600` via `mkstemp` and `os.replace`. They store normalized percentages, reset times, and `fetched_at` only. Helper stderr containing the sentinel is not cached or published.
- `route --argv` uses `shlex.join`. Lane names from `classes.toml` are not part of the eval'd command. `AG_TIMEOUT` is passed as `$1` to a single-quoted `bash -c` script, not interpolated into shell text.

## Verifier summary

```
python3 -m unittest discover -s tests -v
  Ran 116 tests in 0.960s  OK
ruff check claude_max_usage.py grok_usage.py glm_usage.py herdr_model_lanes.py tests
  All checks passed!
ruff format --check claude_max_usage.py grok_usage.py glm_usage.py herdr_model_lanes.py tests
  10 files already formatted
sh -n bin/ag
  (exit 0)
```

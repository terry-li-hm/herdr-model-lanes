# Contract: add Grok reader + model-class router to herdr-model-quota

Workspace: this git worktree (branch feat/class-router). Write only here. Do not touch ~/code or any other path. Do not run `herdr` commands that create tabs or start agents; `herdr plugin action list` and `herdr notification show` are fine if you want, but not required. No network calls except reading the two fixture files already staged in tests/fixtures/grok/.

Read SPEC.md fully first; it is the specification. Then read herdr_model_quota.py, claude_max_usage.py, herdr-plugin.toml, README.md, tests/, ruff.toml. Match the existing style exactly: stdlib only, Python 3.11-compatible syntax, dataclasses, the same cache/lock/helper-subprocess pattern as the Claude path, same test style (unittest, mock), ruff-clean.

Deliverables, in this order (write code, don't plan in prose):
1. grok_usage.py — bounded helper, sole credential boundary, per SPEC §1. Unit-test its parsers with tests/fixtures/grok/credits-weekly.json (accepted) and credits-monthly.json (rejected) and a nested auth.json shape {"https://auth.x.ai::<id>": {"key": "...", ...}} reading only `key`.
2. Grok window in herdr_model_quota.py: GrokUsage dataclass, cache save/load, _refresh_grok with 30-minute cadence, `Gk` segment in format_quota per SPEC §2. Existing Codex/Claude behaviour unchanged; existing tests must still pass.
3. classes.toml per SPEC §3 and a `route` subcommand: pure `select_lane(class_spec, quotas, now, classified=False)` returning (lane, rationale_lines); `--explain` and `--launch` per SPEC §3. Use tomllib. The launch path shells out to `herdr tab create`, `herdr pane get`, `herdr agent start` via subprocess with a 30s timeout each; build the argv lists exactly as SPEC §3 says. Routing happens once at launch.
4. herdr-plugin.toml actions per SPEC §4, version 2.2.0; CHANGELOG entry; README sections for the Grok data source, the routing rule and the kill rule.
5. tests per SPEC §5.

Verifier (run it yourself before reporting, paste the summary lines in your final message):
  python3 -m py_compile herdr_model_quota.py claude_max_usage.py grok_usage.py
  python3 -m unittest discover -s tests -v
  ruff check herdr_model_quota.py claude_max_usage.py grok_usage.py tests
  ruff format --check herdr_model_quota.py claude_max_usage.py grok_usage.py tests
  python3 herdr_model_quota.py route medium --explain   (should print a pick; n/a lanes are fine)
Commit on this branch with a conventional message when green. Do not push. Final message: files changed, test count, anything in SPEC you could not do and why.

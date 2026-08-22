# Contract: GLM (Z.ai / bigmodel coding plan) quota reader for herdr-model-lanes

Workspace: this git worktree (branch feat/glm-reader). Write only here. Network: none during tests (mock); the live call is made only by the plugin under Herdr. Do not print, log, cache, or commit any API key.

Read grok_usage.py, claude_max_usage.py, and in herdr_model_lanes.py: GrokUsage, _refresh_grok, the cache save/load helpers, format_quota, _lane_health, route_command (quotas dict), classes.toml, README.md, tests/test_grok_usage.py, tests/test_route.py. Match the existing pattern exactly; stdlib only; Python 3.11; ruff-clean; write code, don't plan in prose.

Mechanism (verified against CodexBar and cclimits, 2026-08-22):
- Key lookup order: env ZHIPU_API_KEY, ZHIPUAI_API_KEY, ZAI_API_KEY, ZAI_KEY, BIGMODEL_API_KEY, GLM_API_KEY; then ~/.pi/agent/models.json -> providers["bigmodel-coding"]["apiKey"] (a literal key; if it starts with "$" treat as missing); then first line of ~/.config/zhipu/api_key or ~/.config/bigmodel/api_key. Region: base https://open.bigmodel.cn when the key came from a bigmodel/zhipu source or Pi's bigmodel-coding provider, else https://api.z.ai. Override with env MODEL_LANES_GLM_BASE.
- Request: GET {base}/api/monitor/usage/quota/limit, headers Authorization: Bearer <key>, Accept: application/json, User-Agent like the other helpers; 5s timeout; 1 MiB cap.
- Response: {"success": bool, "code": int, "data": {"limits": [ {"type": "TOKENS_LIMIT"|"CREDIT_LIMIT"|"TIME_LIMIT", "unit": ..., "number": ..., "percentage": <used percent>, "usage": ..., "currentValue": ..., "remaining": ..., "nextResetTime": <unix ms>} ... ]}}. Take the TOKENS_LIMIT entry with the nearest nextResetTime (the 5-hour token window). used_percent = percentage if present else round(100*usage/(usage+remaining)) when both present; remaining_percent = 100 - used; resets_at = nextResetTime/1000 as int. Missing TOKENS_LIMIT or success false -> error. Build two fixtures in tests/fixtures/glm/ from this shape (one healthy, one with only CREDIT_LIMIT).

Deliverables:
1. glm_usage.py helper (sole credential boundary, prints {"five_hour": {"used_percent","remaining_percent","resets_at"}} JSON), with tests/test_glm_usage.py (key lookup order incl. Pi models.json and "$" rejection, region choice, parse happy path, no TOKENS_LIMIT, success false, missing key -> unavailable).
2. herdr_model_lanes.py: GlmUsage dataclass with a `five_hour` QuotaWindow, cache file, _refresh_glm at a 5-minute cadence (the window is five hours), `Gl` segment in format_quota after Gk with the same !/!!/~ rules, "glm" key in route_command's quotas dict. IMPORTANT: _lane_health currently assumes a weekly window; make the window duration explicit (QuotaWindow gets an optional window_seconds, default the current weekly value; GLM uses 5*3600) and use it in _lane_health, so a 5-hour window is judged on its own pace. Keep every existing test passing.
3. README: GLM data source paragraph (key sources, endpoint, 5-hour window, no key stored) and remove the "glm has no reader" wording; CHANGELOG 3.2.0; manifest version 3.2.0 + test; README install pin --ref v3.2.0.
4. Tests for the Gl row segment and for route picking glm when it is the only healthy lane (use --classified false default).

Verifier (run, paste summary lines):
  python3 -m py_compile herdr_model_lanes.py claude_max_usage.py grok_usage.py glm_usage.py
  python3 -m unittest discover -s tests -v
  ruff check herdr_model_lanes.py claude_max_usage.py grok_usage.py glm_usage.py tests
  ruff format --check herdr_model_lanes.py claude_max_usage.py grok_usage.py glm_usage.py tests
  sh -n bin/ag
Commit on this branch when green; do not push. Final message: files changed, test count, anything not done and why.

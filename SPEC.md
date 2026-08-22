---
status: dispatched
created: 2026-08-22
owner: active-cockpit
executor: glm-5.3 via Pi (Herdr executors workspace)
repo: ~/code/herdr-model-quota (local-linked Herdr plugin terry.herdr-model-quota)
branch: feat/class-router
review_due: 2026-09-05
kill_rule: delete the route action if Herdr's plugin action log shows no invocation in the two weeks after landing
related:
  - "[[2026-08-22-grok-4-6-leverage-plan]]"
  - "[[feedback-delegate-first-trial-glm53-executes-cockpit-supervises]]"
---

# Model-class router inside herdr-model-quota

## Decision

Terry asked (2026-08-22) whether Herdr should let him pick a model *class* the way Amp's Dial does, and auto-route a new agent tab to Grok Build or to Pi running Sol based on remaining subscription quota before reset. A Cilium sweep run by Grok Build (`/private/tmp/grok-cilium-model-class-router/REPORT.md`, 30-day collector plus first-party READMEs) found no ready-made tool that routes across different agent CLIs by remaining quota. The closest pieces are readers: `herdr-agent-quota` (Claude, Codex, Grok, Agy remaining and reset), `cclimits` (JSON for six providers), and Terry's own `herdr-model-quota` (Codex and Claude weekly in the workspace row). The policy worth copying is explicit, session-start routing (Copilot Auto, ampi, OpenClaw); the anti-pattern is proxy-side silent fallback after a 429 (9Router, claude-code-router).

Decision: build thin, as an extension of `herdr-model-quota`, not a new plugin. One class to start, a Grok reader, and one action. Kill rule above.

## Scope

1. **Grok weekly reader** `grok_usage.py`, a bounded helper in the same shape as `claude_max_usage.py`: the sole credential boundary. It reads the login key from `~/.grok/auth.json` (nested shape: the top-level keys are auth-host strings; take the `key` field of the first entry, never print it), calls `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` with `Authorization: Bearer <key>`, `X-XAI-Token-Auth: xai-grok-cli`, `Accept: application/json`, 5-second timeout, 1 MiB cap, and prints one normalized JSON window: `{"weekly": {"used_percent", "remaining_percent", "resets_at"}}` from `config.creditUsagePercent` and `config.currentPeriod.end` only when `config.currentPeriod.type == "USAGE_PERIOD_TYPE_WEEKLY"`; a monthly period is an error, not a weekly window. Mechanism and field shape verified against Grok Build's own billing extension via `levi-qiao/herdr-agent-quota` (`src/providers/grok.rs`, `docs/research/codexbar-grok-usage.md`, MIT). The token never appears in argv, logs, caches, or errors.
2. **Grok in the workspace row**: `Cx 75% · 4d22h | Cl 9%!! · 1d1h | Gk 62% · 3d2h`, same `!`/`!!`/`~` rules, same 30-minute refresh cadence as Claude, own atomic cache, `Gk n/a` when the helper cannot run. The existing Codex and Claude paths are unchanged.
3. **Class routing** `route` subcommand:
   - `classes.toml` beside the manifest, one class to start:
     ```toml
     [classes.medium]
     description = "Sol on the Codex subscription, else Grok 4.6 on SuperGrok"
     [[classes.medium.lanes]]
     name = "sol"
     kind = "pi"
     args = ["--provider", "openai-codex", "--model", "gpt-5.6-sol"]
     quota = "codex"
     classified_ok = true
     [[classes.medium.lanes]]
     name = "grok"
     kind = "grok"
     args = []
     quota = "grok"
     classified_ok = true
     ```
   - Selection, computed from the normalized caches (refreshing first if stale): for each lane, `time_left = (resets_at - now) / window_seconds`, `quota_left = remaining/100`, `health = quota_left / time_left` (the herdr-agent-quota formula). Pick the first lane in order whose `health >= 1` and `remaining >= 20`; otherwise the lane with the highest `health`; a lane whose quota is `n/a` ranks last; ties keep order. `--classified` drops lanes with `classified_ok = false`. Pure function, unit-tested.
   - `route <class> --explain` prints one line per lane with remaining, reset ETA, health, and the pick, and also shows it as `herdr notification show "Route: <class>" --body "<pick>: <why>"`.
   - `route <class> --launch` does the above, then `herdr tab create --workspace $HERDR_WORKSPACE_ID --cwd <cwd of the invoking pane, from herdr pane get $HERDR_PANE_ID, falling back to $HOME> --label "<class>: <lane>"`, prints the rationale line into that pane (`herdr agent`-free: the tab's shell gets `printf` of the line first), then `herdr agent start <class>-<lane>-<tabnum> --kind <kind> --pane <new pane id> -- <args>`. Routing happens once, at launch; nothing re-routes a running pane.
4. **Manifest**: two actions, contexts `["workspace","tab","pane"]`: `route-medium` "New agent: medium (Sol or Grok by quota)" → `python3 herdr_model_quota.py route medium --launch`; `route-explain` "Show class routing" → `python3 herdr_model_quota.py route medium --explain`. Version bump to 2.2.0, CHANGELOG and README sections (data source for Grok, the routing rule, the kill rule).
5. **Tests** in `tests/`: Grok parse (weekly fixture accepted, monthly rejected, nested auth shape reads only `key`, missing auth → unavailable), row formatting with three sources, and the selection function across: both healthy (order wins), first unhealthy, both unhealthy (highest health), one `n/a`, `--classified` filter.

## Out of scope

No proxy, no mid-task switching, no second class until the first is used, no Claude Code lane in the class (Fable/Opus stay the cockpit tier), no changes to `claude_max_usage.py` or the Codex path, no network beyond the one Grok billing call.

## Verifier

`python -m unittest discover -s tests -v`, `ruff check` and `ruff format --check` on all sources and tests, `python -m py_compile` on the three modules, and a manual `python3 herdr_model_quota.py route medium --explain` from the plugin directory printing a pick with a rationale. Landing: review diff in the worktree, rerun the verifier, merge to main, `herdr server reload-config`, confirm the two actions appear in `herdr plugin action list`.

# Quota handoff

Status: experimental. Automatic control is disabled by default. Passive
observation can collect a genuine quota witness while control remains disabled.
Offline verification and observation are separate from live handoff acceptance.
Account restrictions must never be treated as usage limits or bypassed by rotation.

Quota handoff opens a replacement top-level cockpit when a supported Claude or Pi session reaches a confirmed subscription usage limit. It is disabled until the owner supplies `handoff.json` in the Model Lanes plugin config directory. This is a Linux-only control feature. The existing quota display and launch-time router remain independent.

## Passive observation

Set `enabled` to `false` and `observe_only` to `true` to record future genuine limits without stopping a process, creating a tab, submitting a prompt or changing account exclusions. Only configured native profiles are eligible for observation. Pi observations also require fresh official subscription evidence that a relevant window is exhausted. Excluded accounts are not probed. No notifications are sent.

The controller writes a private, content-free witness under `handoff/observations/`. Records contain safe source IDs, timestamps and evidence hashes. They contain no task text, terminal output, transcript path, environment or credentials. Duplicate evidence does not create another record. `status` reports observation mode and the number of recorded witnesses.

The two mode flags are deliberately separate. `enabled: true` with `observe_only: false` allows control. `enabled: false` with `observe_only: true` allows observation. Both false disables everything. Both true or invalid flag types fail closed. An older installation that ignores `observe_only` still sees `enabled: false` and cannot control a source. An observation is evidence, not permission to activate handoff.

## Routing policy

The supported destinations are native Claude Code on explicitly configured login profiles and Pi on `openai-codex/gpt-6.1-sol`. The Claude model is pinned in configuration. Each Claude profile gets its own normalized quota cache. The router checks the five-hour, weekly and applicable model-scoped limits, keeps a small capacity reserve, and prefers the Claude account with the most headroom. Sol is the fallback when no Claude account qualifies.

Unknown, stale, malformed or exhausted quota is ineligible. The destination must use subscription authentication. API keys, alternate endpoints and other providers are outside this feature. A zero-use Claude rolling window with no reset time represents an unused window. Codex can expose a weekly window with no secondary window; the router checks every window the complete official snapshot exposes.

## Trigger and parking

A terminal notice alone is insufficient. The router also requires a current assistant API-error record from the exact source session, a stable agent identity, and a supported foreground process group. Generic HTTP 429 responses, overload, network errors, context exhaustion, quoted notices and old errors do not trigger replacement.

The controller stops the verified source process group with `SIGSTOP` and verifies that it is stopped before it creates a successor. The original tab, transcript and filesystem work remain in place. It never closes the original tab or automatically resumes its process group. A source with unaccounted-for live subprocesses, an ambiguous identity, or an unsupported session shape is left untouched.

A private, atomic receipt records intent before each consequential operation. Duplicate events do not create duplicate successors. A timeout during creation, startup or prompt delivery is quarantined for inspection. Retrying the scan does not blindly repeat the operation.

## Task authority

A fresh replacement opens without focus. By default it receives no prompt and reads no predecessor transcript. The owner must ask it to continue the old task. This preserves the confirmation gate for protected work and for tasks whose payload class is unknown.

`mark-public --pane <id> --checkpoint <file>` permits one automatic Pi continuation from an owner-authored unclassified checkpoint. The private JSON file must contain `classification` set to `unclassified`, `authority` equal to the configured owner provenance, and a nonempty `checkpoint` string. The marker binds its hash, the terminal, the session and the latest user task. It is not permission to handle protected work. A new task or replacement session does not inherit it. The owner-authored prompt template accepts only `{checkpoint}`. No predecessor transcript is sent to the successor.

Native Claude successors always await confirmation. Their normal prompt hooks can load unrelated context, so the public marker does not authorize automatic native prompting. Session discovery never recursively searches transcript stores.

Automated continuation transfers neither new approval nor authority from an agent's output. The successor must reconcile repository state and existing workers before doing more work. Original task constraints and every send, deletion, publication and protected-action gate remain in force.

## Operation

Herdr invokes `quota_handoff.py event` after agent-status changes. There is no polling daemon or scheduled job. An explicit read-only scan provides a bounded check after a missed event. Automatic control is limited to the configured workspace labels and unambiguous top-level cockpits. Named delegated workers and split tabs are excluded.

Run commands from the linked plugin checkout:

```sh
python3 quota_handoff.py status
python3 quota_handoff.py scan
python3 quota_handoff.py mark-public --pane <pane-id> --checkpoint <private-checkpoint.json>
python3 quota_handoff.py reconcile --pane <pane-id>
```

`status`, `scan` and `reconcile` inspect state. They do not thaw a parked source or repeat an ambiguous mutation. Runtime state belongs under `HERDR_PLUGIN_STATE_DIR/handoff`, with owner-only permissions. Config belongs under `HERDR_PLUGIN_CONFIG_DIR/handoff.json`. Keep runtime state private. The optional checkpoint must be a private, owner-controlled regular file without a symlink.

## Recovery and rollback

Set `enabled` to `false` in `handoff.json` to prevent new handoffs. Set `observe_only` to `false` as well to stop passive observation. Existing source processes stay parked and existing replacement tabs stay open. Inspect the receipt and both sessions before deciding which one owns the task. Only after confirming that the successor has stopped should the owner consider resuming an original process group. Verify PID start times before using any saved process ID. This tool deliberately provides no automatic thaw or close command.

A Herdr server restart is not a tested recovery mechanism for parked agents. Do not restart Herdr to resolve an ambiguous handoff. Reconcile the original and successor first.

The initial version refuses continuation-pointer records, oversized transcripts or lines, expired error evidence, unprovable server identity and named agents. It does not recursively search for a more convenient session. Check the fixed refusal reason and handle an unsupported source manually. A new user task or an account hold supersedes an earlier quota error.

The installed Pi SDK can turn a generic HTTP 429 into a friendly ChatGPT usage-limit message. Pi-source control therefore also requires a fresh official subscription snapshot proving an applicable window is exhausted. Friendly error text alone is insufficient.

## Verification

Run the focused offline suite and the existing plugin regression suite:

```sh
python3 -m unittest discover -s tests -p 'test_quota_handoff.py' -v
python3 -m unittest discover -s tests
ruff check quota_handoff.py tests/test_quota_handoff.py
```

Fake quota and Herdr adapters must prove routing, refusal, process identity, parking order, duplicate suppression and ambiguous-failure behavior without touching a real user tab. A Linux integration case creates one owned synthetic child, exercises the real `SIGSTOP` and `/proc` verification path, proves duplicate suppression, then terminates and reaps only that child. Its native-agent and Herdr metadata remain explicitly synthetic.

Observer tests must prove that stop, create, start, prompt, account-exclusion and notification paths are unreachable, including through direct control-method calls. They must cover real-source corroboration, excluded profiles, generic throttling, stale evidence, malformed configuration, corrupt journals, duplicate suppression and privacy of recorded fields. Test totals, passive observations and a synthetic process test do not prove a completed real quota-exhaustion handoff.

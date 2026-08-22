# Adversarial test pass: herdr-model-lanes v3.3.0

Expired reset timestamps were treated as infinitely healthy, so a stale Claude window beat a live Grok lane. Missing `herdr` also aborted `ag` and `route --argv` with a traceback. Both are fixed.

## Confirmed bugs

### 1. Past `resets_at` ranked as infinitely healthy

**Repro** (clock-skew caches, Claude reset one hour ago at 90% remaining, Grok healthy at 80%):

```sh
HERDR_PLUGIN_STATE_DIR=<skew> python3 herdr_model_lanes.py route medium --explain
HERDR_PLUGIN_STATE_DIR=<skew> python3 herdr_model_lanes.py route high --explain
```

Before the fix: `opus` / `fable` showed `health 900000000.00` and won. After: those lanes are `n/a` and medium picks `grok`, high picks `sol`.

**Fix:** `_lane_health` returns `None` when `resets_at <= now`. Regression: `ExpiredWindowTests`.

### 2. Missing or failing `herdr` aborted routing

**Repro:**

```sh
PATH=<python-only> HERDR_PLUGIN_STATE_DIR=<healthy> python3 herdr_model_lanes.py route medium --argv
```

Before: `FileNotFoundError: 'herdr'`. With a stub `herdr` that exits 1: `route error: herdr list failed: rc=1`. `ag` then printed `no argv produced`.

**Fix:** `_herdr` turns `OSError` / timeout into `QuotaError`. `refresh` swallows publish failures so the quota line and pick still emit. Regression: `PublishResilienceTests`, `StandaloneRouteTests`.

### 3. `--classified --lane` could return a classified-unsafe lane

**Repro:**

```sh
python3 herdr_model_lanes.py route medium --classified --argv --lane glm
```

Before: `pi --provider bigmodel-coding --model glm-5.3` (glm has `classified_ok = false`). After: rc 2, `route: lane 'glm' is not classified-ok`.

**Fix:** reject a `--lane` override that fails `classified_ok` when `--classified` is set. Regression: `ClassifiedOverrideTests`.

### 4. Sub-hour remaining quota displayed as `0h`

**Repro:** GLM cache with 99% remaining and reset in 30 minutes.

```sh
HERDR_PLUGIN_STATE_DIR=<glm_99_30m> python3 herdr_model_lanes.py route medium --explain
```

Before: `Gl 99% · 0h` and `resets in 0h`. After: minutes (`Gl 99% · 30m`). Sol still wins. Surplus does not fire on the five-hour window.

**Fix:** `_countdown` uses `Nm` when remaining is under one hour. Regression: `CountdownTests`.

### 5. Manifest version left at 3.2.1 for a 3.3.0 changelog

`CHANGELOG.md` and the v3.3.0 commit describe 3.3.0. `herdr-plugin.toml` still said 3.2.1, so Herdr would show the old version.

**Fix:** bump the manifest to `3.3.0` and the identity test.

## Suspected issues you could not confirm

- README still lists medium as Sol, Grok, Opus, GLM, Cursor. `classes.toml` is Sol, Opus, GLM, Grok, Cursor after commit `80d9e02`. The tests lock the toml order. Not changed.
- Routing uses only Claude's weekly window. A drained five-hour or Sonnet constraint is shown on the row and ignored by `select_lane`.
- Cursor stays `n/a` in production because `route_command` never supplies a cursor window. Exhausted lanes with data beat it. README calls Cursor a last resort. Tests say `n/a` ranks last against any data.
- On macOS, `PATH=/usr/bin:/bin` can find a `python3` without `tomllib`. `ag` then fails with `ModuleNotFoundError` rather than a missing-interpreter message. `PATH=/bin` fails at the `readlink -f` fallback (`python3: command not found`).
- `format_quota` still prints cached remaining for an expired window (`Cl 90% · 0m`). Routing now treats that window as `n/a`.
- `GrokRefreshTests.test_refresh_includes_gk_segment_only_when_enabled` does not mock `query_claude` and can invoke the real helper.

## Tested-and-fine

- All-healthy caches pick the first healthy lane in class order (`sol` / `fable`).
- Claude remaining 0 falls through. Codex older than six hours becomes `n/a`. Missing Grok is `n/a`. Corrupt Claude cache is `n/a`. Remaining 0 and 100 format and select correctly.
- `route nosuch` rc 1. `route medium --lane nosuch` rc 2. `--argv --lane grok` overrides. `--classified` omits glm.
- Startup with a stub `herdr` publishes `model_quota` on the focused workspace.
- `bin/ag`: `sh -n` clean. `--lane` is an unknown option. `ag q` is an unknown class. Non-tty stdin auto-starts. `glm-5` numbers as item 2. `eval` preserves spaces and quotes in args. `AG_YES=1` with `MODEL_LANES_STATE_DIR` and `HERDR_PLUGIN_STATE_DIR` unset works.
- Seeded `select_lane` properties hold over 200 cases (seed 20260822). The pick is always in-class. All-n/a picks the first lane. Remaining below 20 is never chosen while a candidate exists. Surplus never fires for `window_seconds <= 48h`. `--classified` never returns `classified_ok=false`.
- Two processes writing the Codex cache under the file lock do not corrupt it. A stale attempt file does not block `force=True`.
- Cross-platform: `readlink -f` has a Python fallback. `mktemp` uses `ag.XXXXXX`. No `sed -i ''`. No `date`. `fcntl.flock` is the lock.

## Verifier summary

```text
python3 -m unittest discover -s tests -v
Ran 106 tests in 0.792s
OK

ruff check herdr_model_lanes.py claude_max_usage.py grok_usage.py glm_usage.py tests
All checks passed!

ruff format --check herdr_model_lanes.py claude_max_usage.py grok_usage.py glm_usage.py tests
9 files already formatted

sh -n bin/ag
(exit 0)
```

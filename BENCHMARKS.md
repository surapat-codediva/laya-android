# Phase 3 benchmarks: before / after

All numbers below were measured on 2026-10-02 on the setup described here. Nothing is
projected or estimated. **No real phone was tested**: every device number comes from an
emulator, which shares the host's CPU and GPU with Laya.

## Setup

- **Host:** Mac, 8 cores (4P + 4E). The machine was busy with other applications: load average
  was 28–57 during the runs, so expect noise. Before and after were run back to back
  (interleaved), so both saw the same conditions.
- **Device:** Android emulator, Pixel 8 AVD, Android 16 (API 36), arm64, 1080×2400, headless,
  over `adb` on localhost.
- **Laya:** `convaiinnovations/laya`, `ml` checkpoint, running on the host's GPU (MPS, Laya's
  default on macOS).
- **Before:** commit `3c8eb84` (Phase 2). **After:** this branch.
- **Scripts:** in [`benchmarks/`](benchmarks/). `run_all.sh` runs everything, `compare_run.py`
  covers the run loop, `compare_cli.sh` covers CLI commands, and `la benchmark` covers
  components.

### The run-loop scenario

Both versions drive the same 6 steps on the Settings app:

1. tap *Network & internet*
2. BACK
3. tap *Connected devices*
4. BACK
5. tap *Apps*
6. BACK

The steps are non-destructive and repeated 4 times, for 24 steps per configuration. Laya is
called for real on every step, so decision latency is real. Its answer is then replaced by the
script, so both versions perform identical actions. The "fake Laya" rows skip the model
entirely, which isolates the device-side cost.

## Results

### Autonomous loop (`run_goal`), per step

| Metric | Before | After | |
|---|---|---|---|
| **avg step, real Laya** | **6614 ms** | **4743 ms** | **1.39× faster** |
| per-run averages | 6790 / 6617 / 6555 / 6494 | 4828 / 5082 / 4528 / 4535 | |
| p50 / p95 step (after) | — | 4030 / 7832 ms | |
| tap steps / BACK steps (after) | — | 5790 / 3696 ms | |
| avg step, fake Laya (device side only) | 5148 ms | 4309 ms | 1.19× |
| fixed wait after each action | 1500 ms | removed | |
| avg settle (adaptive, after) | 1500 ms (fixed) | 1691 ms | see bottlenecks |
| observation time per step | 4204 ms | 2655 ms | −37% |
| full dumps per step | 1.67 | 1.17 | −30% |
| adb calls per step (all kinds) | 2.67 | 6.08 | +3.4 cheap probes |
| Laya calls per step | 1 | 1 | unchanged, one forward pass |
| Laya decision p50 | 819 ms (in-process) | 260 ms (daemon) | 3.1× |
| taps on the fast path | — | 12 / 12 | no pre-tap re-dump |
| settle results (after) | — | 20 stable, 4 dynamic | |

Where the time goes in an *after* step: observe 2759 ms, settle 1691 ms, decision 249 ms,
execute 43 ms (averages).

### CLI commands (wall time, including process start), Settings app

| Command | Before | After | |
|---|---|---|---|
| `pick "…"` | 11005 / 9544 ms | 3260 / 2570 ms | 3.5× (model loaded once, in the daemon) |
| `check "…"` | 10407 ms | 2381 ms | 4.4× |
| `tap N` (after `screen`) | 5371 / 5368 / 5886 ms | 4068 / 3757 / 3881 ms | 1.42× (fast path + adaptive settle) |
| `key back` | 3480 / 3381 / 3402 ms | 3287 / 3365 / 3483 ms | ≈ same |
| `screen` | 2322 / 2232 / 2202 ms | 3071 / 2684 / 2576 ms | **0.8× (slower)** |

`screen` is slower because it now takes the screen signature before the dump, which costs
about 0.15 s idle and up to 0.5 s on this loaded host. That signature is what lets the next
`tap N` skip a ~2.2 s re-dump. So `screen` followed by `tap N` went from 7.8 s to 6.7 s.

### Components (`la benchmark --iterations 20 --step`, after)

| Component | p50 | p95 |
|---|---|---|
| Laya warm decision (daemon) | 206 ms | 210 ms |
| Laya first decision after load | 299 ms (one sample) | |
| snapshot parse + fingerprint | 1 ms | 1 ms |
| adb shell round trip | 19 ms | 28 ms |
| screen probe (light observation) | 151 ms | 503 ms |
| full observation (`uiautomator dump`) | 2232 ms | 2300 ms |
| full safe step (no-op key event) | 2791 ms | 4529 ms |

### Laya startup (separate measurements on this machine)

| | Time |
|---|---|
| daemon cold start incl. model load + warm-up (`la daemon start --load`) | 9.4 s (load 8.7 s, warm-up 0.49 s) |
| first inference without warm-up (in-process, two runs) | 1841 ms, 525 ms |
| warm inference (in-process) | 175–183 ms p50 |
| in-process `pick`/`check` (Phase 2: every command) | 9.5–14.4 s |

The first inference without warm-up was 3–10× slower than a warm one, so the daemon runs a warm-up after loading.
Startup latency (~10 s, once per daemon lifetime) and steady-state latency (~0.2 s per
decision) are now separate costs. Before, every `pick`/`check`/`run` paid the startup cost.

## Measured bottlenecks

1. **`uiautomator dump`: ~2.2 s per full observation.** On the device this breaks down into
   ~0.7 s to start the uiautomator JVM (`app_process`), a built-in `waitForIdle` that needs 1 s
   with no accessibility events, and ~0.3 s of traversal. Parsing the result takes 1.5 ms.
   Nothing reachable over adb avoids it. `dumpsys activity top` is fast (~35 ms) but has no
   text. **This is now the largest cost of every step, and removing it requires Phase 4:** an
   on-device AccessibilityService that keeps the tree and answers in tens of milliseconds.
2. **Settling: ~1.7 s on this emulator.** That is mostly the app's own transition. Settings
   pages kept repainting for 1.3–1.8 s after a tap, plus one probe to confirm. While the
   device is busy, a probe slows from ~0.15 s to ~0.45 s (screencap readback on a GPU that the
   emulator shares with the host and with Laya). On a quiet screen, settle took ~0.35 s
   (benchmark `--step`), compared with the fixed 1.5 s it replaced. A Phase 4 service could
   receive window and content change events instead of polling pixels.
3. **Laya: ~0.2–0.26 s per decision** once warm. This is small now. Before, 9.5–14 s of model
   loading dominated every `pick`/`check`.
4. **adb process spawn: ~20 ms per call.** It is not a bottleneck: the extra probes cost their
   screencap, not the process start.

The KPI was a 2× faster average step. On this emulator the autonomous loop reached 1.39×,
because the remaining time is the dump plus the app's real transition time. `pick` and `check`
are 3.5–4.4× faster. Telemetry (`run` → `timing`, `-v`, `la benchmark`) shows the split per step.
Phase 4 should target the dump and the polling.

## Safety and correctness under faster execution

None of this skips the policy gate, stable-key resolution, sensitive-action handoff or
trajectory redaction. The fast path only skips a redundant dump, and only when the
pre-dump screen signature still matches. Sensitive targets, sensitive screen wording,
password fields, permission dialogs, secure windows, ambiguous identities and old snapshots
all take the safe path. That rule set has unit tests. In this benchmark all 12 taps were
eligible for the fast path and took it. Both versions completed the same 24 scripted actions.
The "after" loop added stuck detection (`repeated_action_without_progress`).

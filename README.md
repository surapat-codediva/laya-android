# laya-android

An agent skill (Claude Code and other [skills](https://github.com/vercel-labs/skills)-compatible agents)
that controls an Android phone or emulator over `adb`: read the screen, tap, type, swipe, and
navigate toward a goal. It uses the local [Laya](https://huggingface.co/convaiinnovations/laya)
classifier for cheap decisions (which element to tap, yes/no screen checks), and the agent takes
over whenever Laya is unsure.

## Install

```bash
npx skills add surapat-codediva/laya-android          # project-level
npx skills add surapat-codediva/laya-android -g       # user-level (~/.claude/skills)
```

## Requirements

- `adb` on PATH (Android platform-tools), with a device connected over USB or wireless debugging
- `python3` 3.9+ (set `LAYA_PYTHON` to pick a specific interpreter)

The first time the skill runs, `la` creates a `.venv` inside the skill folder and installs
`laya` (pulls in torch). The first Laya call then downloads the model (~650 MB) and starts the
local Laya daemon, which keeps the model loaded for later commands.

## Usage

Ask your agent to do something on the phone, e.g. "open Settings on my Android and turn on
dark mode". Or run the CLI directly:

```bash
bash skills/laya-android/la screen              # list tappable elements
bash skills/laya-android/la screen --json       # the full structured snapshot
bash skills/laya-android/la tap 3               # tap element 3
bash skills/laya-android/la run "open Wi-Fi settings" --until-text "Wi-Fi preferences" --record
bash skills/laya-android/la -v run "..."        # diagnostics and per-step timings on stderr
bash skills/laya-android/la daemon status        # the persistent Laya daemon (started automatically)
bash skills/laya-android/la benchmark --step     # measure Laya, adb, observation and settle latency
```

See [`skills/laya-android/SKILL.md`](skills/laya-android/SKILL.md) for all commands, the
reliability notes (zero-shot Laya is close to random — the agent verifies), and safety rules.

## How it works

Still just `adb` + `uiautomator dump`, with no companion app or AccessibilityService. One dump (plus the focused activity, in
the same adb round trip) becomes a `MobileSnapshot`:

- **Elements with semantic state.** Each element has its raw class and a normalized `role`
  (`button`, `switch`, `checkbox`, `radio`, `input`, `list`, `scroll`, `item`, …), plus `text`,
  `content_desc`, `label`, `resource_id`, the clickable/editable/scrollable/checkable/enabled/
  checked/selected/focused/password flags, `bounds` and `center`.
- **Surrounding context.** Each element gets the nearby text that tells repeated controls apart,
  e.g. `button "Buy" context="iPhone Case B | ฿159"`. That text comes from the closest ancestor
  with text, nearest parts only, capped at 200 chars, and leaves out other tap targets.
- **Stable keys.** Each element's `stable_key` is a hash of role, resource id, name, first
  context part and enclosing container. It never uses position or index, so the same switch keeps
  its key when the list shifts or the switch toggles.
- **Semantic fingerprint.** The screen id is built from app, activity, visible text, and each
  element's key and state. It ignores XML order, formatting, coordinates, the status bar and
  clock times.
- **Candidate pruning.** `candidates_for_operation(snapshot, op, limit, goal)` uses rules and a
  small score, never a model. Tap targets for `CLICK`, inputs for `TYPE`, scrollable containers
  for `SCROLL`. Laya only sees those, each as one compact line.
- **Policy gate.** Every autonomous action passes a deterministic check of the target (payment,
  transfer, delete, logout, credentials, permission grants, factory reset; English and Thai).
  The result is `allow`, `handoff` (exit 3, with the reason in the JSON) or `block`.
- **Trajectories.** Recording is opt-in and writes versioned JSONL. Each step holds the before
  snapshot, Laya's decision, the host's action, what was executed, the policy result and the
  outcome.

## Performance architecture (Phase 3)

The cost of one step is the whole cycle: observe, decide, execute, let the UI settle, and
observe again. Phase 3 measured each part on an emulator first. Most of the time was in
`uiautomator dump` and fixed sleeps. Laya inference itself was a small share, except when every
command reloaded the model.

```
 agent / CLI ──► DeviceSession (laya_mobile.adb.Device)      DecisionClient ──► Laya daemon
                 cached device info, last snapshot,           (Unix socket,      model loaded once,
                 one AdbClient for every adb process            0600, local)     warm, serialized
                        │
   FULL OBSERVE  ◄──────┘  dump + activity + secure flag + screen signature, one adb call (~2 s)
        │
      DECIDE     one Laya request per step (operation, target, done in one forward pass)
        │
   POLICY GATE   unchanged: payment, delete, credentials ... hand off
        │
   FAST / SAFE   fast: the screen signature still matches (one ~0.15 s probe), the target is
        │        unique and nothing is sensitive. Safe: re-dump and re-find the element by key
     EXECUTE
        │
     SETTLE      LIGHT OBSERVE (screen hash, computed on the device) until it holds still
        │        A A B C C -> stable on C; never still -> continue at the timeout
   FULL OBSERVE  this observation is the next step's: nothing is dumped twice
```

- **Persistent Laya daemon.** `pick`, `check` and `run` send their questions to a local daemon
  (`laya_mobile/daemon.py`) over a Unix socket. It binds to the local user only (mode 0600) and
  never to a TCP port. The client starts it on first use, so you never have to. A lock file
  stops a second daemon from starting, and stale socket or pid files are cleaned up. If the
  daemon crashes or stops answering, the client restarts it once and retries once. After that
  it exits 2 with a clear error. Daemon trouble never turns into a Laya "unsure" handoff. One
  daemon serves every device and terminal, one inference at a time. It loads `ml` and `en` only
  when first asked for, and exits after 30 idle minutes. `la daemon start|status|stop` exist
  but are optional. `--no-daemon` loads Laya in-process instead.
- **Warm-up.** After loading, the daemon runs one tiny inference. On this machine the first
  inference after a load took 0.5–1.8 s and later ones about 0.2 s, so warm-up pays for itself.
- **Adaptive settle** (`laya_mobile/settle.py`) replaces the fixed 1.0 s / 1.5 s waits. After an
  action, the light probe runs `screencap | md5sum` on the device, without the status bar
  (~0.12–0.15 s idle). It repeats until two probes in a row match. Each action has its own
  expectation: a tap, BACK or scroll is expected to change the screen, and an unchanged screen
  is waited on for a grace period. An intermediate screen (spinner, half-drawn page) does not
  end the wait. Screens that never hold still (video, animation) stop at the timeout and are
  observed anyway. `--wait N` still gives a fixed wait.
- **Freshness.** Each snapshot carries the screen signature taken *before* its dump, plus
  `age_ms`. If the screen changed during the dump, the signature no longer matches later, so a
  stale snapshot can never pass as fresh. Secure windows produce black screenshots, so they get
  no signature and always take the safe path. `tap N` and `run` take the fast path only when
  all of these hold: the signature matches, the target's stable key is unique, the policy gate
  matched nothing, there is no password field, permission dialog, or sensitive wording such as
  "Delete" on the screen, and the snapshot is less than 2 minutes old. Otherwise they re-dump
  and re-resolve, as in Phase 2.
- **Stuck detection.** The same action on the same screen structure a third time hands off
  with `repeated_action_without_progress`. Screen structure means app, activity and element
  keys, ignoring state and ticking text. An action with no visible change still hands off
  immediately, as before.
- **Fewer, cheaper adb calls.** `AdbClient` is the only place adb runs. Every call is bounded by
  a timeout, timed, and counted. Device info (screen size, density, status-bar height) is read
  once per device and cached in the session file. Swipes no longer run `wm size`, and a single
  connected device needs no `getprop`. A full observation stays one adb call.
- **Telemetry.** `run` JSON and trajectory steps carry `timing_ms`
  (observe/decision/policy/execute/settle/total), `adb_calls`, `dumps`, `probes`, `laya_calls`
  and the settle result. `run` adds a run summary (avg/p50/p95 step, calls per step). `-v`
  prints the same on stderr. The trajectory `version` stays 1, because the fields are additive.
- **Benchmark.** `la benchmark [--iterations N] [--step] [--no-device] [--json]` measures
  daemon startup, model load, first and warm decisions, and parsing. With a device it also
  measures an adb round trip, the probe and a full observation. `--step` adds full safe steps,
  where the action is a no-op key event. It never taps anything.

Measured before/after numbers and the remaining bottlenecks are in
[`BENCHMARKS.md`](BENCHMARKS.md).

Tuning (defaults suit normal devices):

| Variable | Default | |
|---|---|---|
| `LAYA_ANDROID_SETTLE_POLL_MS` | 75 | pause between probes |
| `LAYA_ANDROID_SETTLE_STABLE_MS` | 150 | how long the signature must hold, over ≥ 2 probes |
| `LAYA_ANDROID_SETTLE_TIMEOUT_MS` | 2500 | stop waiting and observe anyway |
| `LAYA_ANDROID_DAEMON_TIMEOUT` | 30 | seconds for one Laya request once the model is loaded |

Other bounds are fixed in `laya_mobile/config.py`: an adb call has 30 s, the daemon has 20 s to
start, a model load has 900 s (the first one downloads it), and an idle daemon exits after 30 min.

Code layout (`skills/laya-android/`):

```
laya_android.py        CLI (argparse, output, exit codes)
laya_mobile/
  adb.py               AdbClient (every adb call, timed), Device session: info cache, observe, probe, input
  config.py            settle defaults, per-action expectations, timeouts
  daemon.py            persistent Laya daemon + client (start on demand, restart once)
  settle.py            adaptive settle on the light probe
  freshness.py         fast path vs safe path, stuck detection
  metrics.py           per-step timings, call counts, run summaries
  benchmark.py         `la benchmark`
  models.py            MobileElement, MobileSnapshot (JSON round-trip)
  elements.py          XML -> elements: roles, labels, context, stable keys, describe()
  observe.py           snapshot, fingerprint, resolve() for stale-safe taps
  candidates.py        operation-aware pruning
  decision.py          Laya questions/answers; DecisionClient (daemon or in-process)
  policy.py            PolicyGate
  trajectory.py        TrajectoryRecorder, redaction, correction records
  runner.py            observe -> decide -> gate -> act -> settle loop
  session.py, checks.py, executor.py, errors.py
tests/unit/, tests/device/, tests/fixtures/*.xml
```

## Trajectories and privacy

```bash
la run "open Bluetooth settings" --record           # ~/.laya-android/trajectories/<UTC>-<run id>.jsonl
la run "..." --trajectory /tmp/run.jsonl            # exact file
la goal "..." --record                              # also record your manual tap/type/key/swipe
LAYA_ANDROID_RECORD=1 / LAYA_ANDROID_DATA_DIR=/path # opt in globally / move the data dir
```

Lines are `{"version": 1, "run_id", "event": "start"|"step"|"end", "timestamp", "goal", ...}`.
A step has `before`, `history`, `model_decision`, `teacher_action`, `executed`, `policy`, `after`
and `outcome`. When Laya decided and the host then acted on the same screen, the step is a
correction example: `model_decision` next to `teacher_action`. Integrations can also call
`laya_mobile.trajectory.correction_record(...)`. New optional fields (e.g. screenshots) can be
added without breaking readers. Renames bump `version`.

**Trajectories contain UI text from the device** (names, messages, balances). Treat them as
sensitive data and keep them local. The skill never stores what a password field shows. Typed text is
written as `"[REDACTED]"` in three cases: the field is a password/PIN/OTP field, the text looks
like a PIN or code, or the field is unknown and a password field may be on screen.

## Tests

No phone and no Laya download needed. The tests run on saved UIAutomator XML fixtures with a
fake model:

```bash
cd skills/laya-android
pip install -r requirements-dev.txt     # pytest
pytest                                  # = pytest tests/unit
pytest tests/device --device            # optional read-only smoke tests on a connected device
```

The unit tests cover the daemon with a fake model, both in a thread and as a real spawned
process: start on demand, duplicates, stale files, crash and restart, hang and timeout, signals.
They also cover adaptive settling on scripted signature sequences, fast/safe paths, stuck
detection, timing aggregation and the adb layer. Architectural regression tests check that a
step makes one Laya request, that swipes do not query the screen size, that settle polls never
re-dump, and that the daemon loads a model once.

## Changes in Phase 3

- Laya runs in a local daemon that starts on demand. There are new `daemon` and `benchmark`
  commands, and a global `--no-daemon` flag.
- `--wait` on `tap`/`type`/`key`/`swipe`/`run` now defaults to adaptive settling. Passing a
  number still gives a fixed wait.
- `screen --json` and trajectory snapshots gain `signature`, `secure` and `rotation`.
- `run` JSON gains `timing` (run summary). Each step gains `timing_ms`, `adb_calls`, `dumps`,
  `probes`, `laya_calls`, `settle` and, for taps, `path` (`fast`/`safe`, with `safe_reason`).
- New handoff reason: `repeated_action_without_progress`.
- Exit code 2 now also covers Laya daemon errors.

## Changes in Phase 2 (output formats)

- `screen` prints `fingerprint <id>  app <pkg>  activity <.Activity>` (was `snapshot <id>  app
  <pkg>`) and elements as `[N] role "label" [state] context="…" resource="…" (x,y)`.
- `screen --json` is the full snapshot: `fingerprint` (was `snapshot`), `visible_text` (was
  `text`), `activity`, `timestamp`, and elements with `role`/`class` (was `kind`), `center`
  (was `x`/`y`) and `stable_key` (was `key`).
- `pick`/`run` JSON: `fingerprint` replaces `snapshot`. Both add `candidates` and `policy`, and
  `run` adds `trajectory` when recording. `pick` exits 3 when the gate would stop its action.
- Trajectories are opt-in now (they were always on while a goal was set) and live under
  `~/.laya-android`. `LAYA_ANDROID_LOG` is gone.
- Usage and device errors now really exit 2, as documented. Before, they exited 1, which
  collided with `verify`/`check` "no".

## License

MIT. Laya itself is Apache-2.0.

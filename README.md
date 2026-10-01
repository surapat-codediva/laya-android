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
`laya` (pulls in torch). The first Laya call then downloads the model (~650 MB).

## Usage

Ask your agent to do something on the phone, e.g. "open Settings on my Android and turn on
dark mode". Or run the CLI directly:

```bash
bash skills/laya-android/la screen              # list tappable elements
bash skills/laya-android/la screen --json       # the full structured snapshot
bash skills/laya-android/la tap 3               # tap element 3
bash skills/laya-android/la run "open Wi-Fi settings" --until-text "Wi-Fi preferences" --record
bash skills/laya-android/la -v run "..."        # diagnostics on stderr
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

Code layout (`skills/laya-android/`):

```
laya_android.py        CLI (argparse, output, exit codes)
laya_mobile/
  adb.py               device selection, dump + activity, input
  models.py            MobileElement, MobileSnapshot (JSON round-trip)
  elements.py          XML -> elements: roles, labels, context, stable keys, describe()
  observe.py           snapshot, fingerprint, resolve() for stale-safe taps
  candidates.py        operation-aware pruning
  decision.py          Laya questions/answers (predictor is injectable)
  policy.py            PolicyGate
  trajectory.py        TrajectoryRecorder, redaction, correction records
  runner.py            observe -> decide -> gate -> act loop
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

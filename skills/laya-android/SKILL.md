---
name: laya-android
description: Control an Android phone or emulator over adb (USB or wireless debugging) — read the screen, tap, type, swipe, and navigate toward a goal — using the local Laya decision model (convaiinnovations/laya) for fast element picking and yes/no screen checks, with Claude taking over whenever Laya is unsure. Use when the user asks to operate, navigate, tap through, or check something on an Android device/app, or mentions Laya with Android.
---

# laya-android

Laya is a **classifier** (322M multilingual / 421M English), not a GUI agent: it reads text and
answers typed questions (choice / yes-no) with a confidence. It cannot see pixels or invent
actions. This skill turns the screen into text with `uiautomator dump`, lets Laya make the cheap
decisions — next operation, tap target and "goal reached?" in one forward pass — passes every
autonomous action through a deterministic **policy gate**, and performs it with `adb`. **You
(Claude) are the fallback** — exit code `3` means "Laya is unsure, stuck, or the gate stopped it;
you decide".

## Run

```bash
bash <skill-dir>/la <command>   # <skill-dir> = this skill's base directory
```

Below, `la` means `bash <skill-dir>/la`. The wrapper uses the skill's own venv; on the first
run it creates `.venv` and installs `requirements.txt` (laya + torch, a few minutes — run it
with a long timeout). Needs `python3` (override with `LAYA_PYTHON`) and `adb` on PATH.

Device is picked automatically. Wireless debugging lists one phone twice (`ip:port` and
`adb-<serial>-…._adb-tls-connect._tcp`); the script treats those as one device and uses the
mDNS name (it survives port changes). With several real devices, pass `-s <serial>`.
If no device: on the phone open *Developer options → Wireless debugging*, then
`adb pair <ip:pairport>` (once, with the code) and `adb connect <ip:port>`.

## Commands

| Command | What it does | Uses Laya |
|---|---|---|
| `la screen [--json]` | `fingerprint <id>  app <package>  activity <.Activity>`, `text:` the page's other text (title, body), then elements `[N] role "label" [state] context="…" resource="…" (x,y)`. `--json` prints the full snapshot | no |
| `la tap N` / `la tap X Y` | Tap element N from the **last** screen shown, or coordinates | no |
| `la type "text"` | Type into the focused field (ASCII only) | no |
| `la key back\|home\|enter\|del` | Press a key | no |
| `la swipe up\|down\|left\|right` | Scroll | no |
| `la screenshot [/tmp/x.png]` | Save PNG — then `Read` it. Only when `screen` is not enough (below) | no |
| `la goal "goal" [--record]` | Start a goal run; later actions are attributed to it (and recorded with `--record`) | no |
| `la finish ok\|fail [--note ".."]` | End the session with the outcome **you verified** | no |
| `la pick "goal"` | Laya's next operation + element. Prints JSON, **does not act** | yes |
| `la check "yes/no question"` | Laya P(true) about the screen. exit 0 yes / 1 no / 3 unsure | yes |
| `la verify [--text T] [--package P] [--element L]` | Exact check of the current screen. exit 0 all met / 1 not; JSON lists what is missing | no |
| `la run "goal" [--until-text T] [--until-package P] [--until-element L] [--max-steps 6] [--record]` | Loop: decide → gate → act, until done (0) or unsure/stuck/gated (3) | yes |

Global flags go before the command: `-s <serial>`, `--model ml|en`, `-v`/`--verbose` (diagnostics
on stderr: fingerprint, candidate count, decision and confidence, policy result, screen changed;
stdout stays machine-readable).

Laya's operations are `CLICK`, `SCROLL_DOWN`, `SCROLL_UP`, `BACK`, `DONE`; it does not type.
`pick`/`run` report `operation`, `op_confidence`, `target` (`id` + element), `target_confidence`,
`candidates` (how many elements Laya chose among) and `policy`; a step is confident only when the
operation, and for `CLICK` the target, reach `--min-confidence`. `target` is shown even when the
operation is not `CLICK`. `pick` exits `3` when the step is unsure **or** the gate would stop it.

**Laya sees a pruned, described candidate list**, not every node: for a tap, only clickable or
checkable elements, ranked by role, real name, resource id and words shared with the goal
(disabled and status-bar elements last), at most `--candidates 20`; `SCROLL_*` is offered only when something on screen scrolls.
Each option is one line, e.g. `button "Buy" context="iPhone Case B | ฿159"` or
`switch "switch_widget" [off] context="Bluetooth | Off"`. `context` is the nearby text that tells
repeated elements apart (the product next to a "Buy", the row a switch sits in, the message an
"OK" confirms); it never includes other tap targets' text.

**Done is decided by the screen, not by Laya, when you give `--until-*`.** `run` checks them
before each decision and after the last action: all must hold (text and labels match
case-insensitively as substrings; `--until-text`/`--until-element` can repeat). If they already
hold, `run` exits `0` without loading Laya; if Laya picks `DONE` while they don't, it hands off
and says what is missing. `p_done` is then only logged. Without `--until-*`, `run` falls back to
Laya's done check (`--done "question"`, P ≥ `--yes`), which is a guess — avoid it. `verify` and
`--until-text` see the page text `screen` shows (first 1500 chars) plus element labels.

Options: `--model ml` (multilingual, default — handles Thai UI) or `--model en`;
`--min-confidence 0.6` for pick/run; `--yes 0.8` for check/run.
First Laya call downloads ~650 MB and each invocation loads the model (several seconds);
`run` loads it once for the whole loop.

**`tap`, `type`, `key` and `swipe` print the new screen** (same format as `screen`) after
waiting `--wait 1.0` s for it to settle — that is your next observation, so don't call `screen`
or `screenshot` after an action. `--no-screen` skips it.

**Element numbers are tied to a snapshot.** `tap N` refers to the last screen printed (by
`screen`, an action, `pick` or `run`) and re-checks the screen before tapping. Elements are matched
by their **stable key** (role + resource id + name + context + container, never position or N): if
the element moved it taps its new position; if it is gone or ambiguous (identical elements and the
screen changed) it does not tap, exits `4`, and prints the current screen to pick from. `run`
applies the same check before each tap and re-decides when the target went stale.

**The fingerprint is a semantic screen id**: app, activity, visible text and each element's key and
checked/selected/focused/enabled state — not XML layout, node order, coordinates, the status bar or
clock times. Toggling a switch changes it; re-dumping an idle screen does not. `run` uses it to
detect "the screen did not change".

**Screenshots cost far more than `screen`** (an image vs. a few lines of text). Take one only when:
- elements are icons with empty or meaningless labels (`ImageView "iv_3"`) you need to tell apart,
- `screen` shows no interactive elements (WebView, game, canvas, custom-drawn UI),
- the answer is visual (colour, image content, layout) or you are doing the final check of a
  result whose text alone is ambiguous.
A new page by itself is not a reason: its `text:` line and elements say where you are.

## Workflow

1. `la goal "<the user's goal>"` (skip if you start with `run`/`pick`, which set it). Add
   `--record` when the user wants trajectories kept (e.g. to build Mobile-Laya training data).
2. `la screen` once to see where you are; after that, read the screen each action prints.
3. For a simple navigation goal, try `la run "goal" --until-text "<text only on the target screen>"`
   (add `--until-package`/`--until-element` to make it tighter).
   - exit `0`: the conditions held on screen; still glance at the printed result before reporting.
   - exit `3`: read the JSON `reason`/`steps`, then `screen` and continue with `tap N`. If the
     JSON has `policy`, the gate stopped a sensitive action: get the user's go-ahead first.
4. For assertions in a flow, prefer `verify` (exact). `check` is a Laya hint only.
5. When you have verified the result on screen: `la finish ok` (or `la finish fail`).

## Policy gate

Every action `run` would take autonomously is checked in code first (Laya is never asked).
It looks at the **target** — label, text, description, resource id, role, password flag — not at
every word on the screen, so a "Delete" button elsewhere does not stop a tap on "Inbox".
- `handoff` (exit 3, JSON `"policy": {"action", "reason", "matched", "keyword", "target"}`) for
  payment/purchase/checkout/"Buy", money transfer, delete/remove/uninstall, logout, password/
  PIN/OTP, send/post, and permission grants (any non-cancel button of the system permission
  dialog). A generic "OK"/"Confirm"/"Continue" is judged by what it confirms (its context, and the
  whole screen when it is dialog-sized): "OK" under "Delete 3 photos?" hands off. Keypad digits on
  a PIN/password screen hand off (wrong PINs count toward lockout).
- `block` for factory reset and account removal — never autonomous.
- Thai keywords are covered too (ชำระเงิน, โอนเงิน, ลบ, ออกจากระบบ, รหัสผ่าน, อนุญาต …).
When the user explicitly asked for such a step, do it yourself with `tap N`, or pass
`run --allow <category>` (`payment`, `delete`, `send`, …; not `factory_reset`/`account_removal`).
Your own `tap`/`type` are not blocked, but a sensitive one prints a `policy …` warning on stderr
and is marked in the trajectory — make sure the user asked for it.

## Trajectories

Recording is **opt-in**: `la run "goal" --record`, `la goal "goal" --record` (then your
`tap`/`type`/`key`/`swipe` are recorded too), `--trajectory <file.jsonl>` for an exact path, or
`LAYA_ANDROID_RECORD=1`. Files go to `$LAYA_ANDROID_DATA_DIR/trajectories/` (default
`~/.laya-android/trajectories/<UTC time>-<run id>.jsonl`), never into the skill folder.

Each line is versioned JSON (`"version": 1`) with `event` = `start` | `step` | `end`. A step holds
`before` (the full snapshot), `history`, `model_decision` (Laya's choice and confidence),
`teacher_action` (your action, when you acted), `executed`, `policy`, `after` (fingerprint) and
`outcome` (`screen_changed`, `stale`, `handoff`). When Laya decided on a screen and you then acted
on that same screen, the step holds both: a correction label for fine-tuning a Mobile-Laya.
`la finish ok|fail` writes the `end` line with the outcome you verified.

**Privacy**: trajectories contain on-screen text (names, messages, balances…) — treat them as
sensitive and keep them local. Password fields' text is never read, and typed text is stored as
`"[REDACTED]"` when the field is a password/PIN/OTP field, the text looks like a PIN or code, or
the field is unknown and a password field may be on screen.

## Reliability — read before trusting Laya

Measured on a real Samsung A33 home screen (11 elements), zero-shot base checkpoints:
- `pick` was near chance (confidence 0.06–0.09 ≈ 1/11) and chose the wrong element.
- `check` answered "is a login form shown?" with P=0.96 on a home screen — confidently wrong.

This matches the model card: base checkpoints are near random on unseen decision tasks, and
`noul` can follow the wording of the question instead of the state. So:
- The confidence gate (`--min-confidence`) is what keeps `run` safe; do not lower it to "make it work".
- Never rely on `check` alone for a yes. Verify on the screen yourself.
- Laya becomes useful after fine-tuning on this app's screens (see the Kaggle notebook in
  github.com/NandhaKishorM/laya). Until then, expect most steps to hand off to you.

## Safety

- On a **real phone**, ask the user before tapping/typing (`tap`, `type`, `key`, `swipe`, `run`)
  unless they already told you to operate it. `screen`, `screenshot`, `pick`, `check` are read-only.
- The policy gate stops `run` before payments, orders, transfers, deletions, logouts,
  permission grants and PIN entry, but it is a keyword heuristic, not a guarantee: don't `run`
  on such screens, and do those steps manually and deliberately, with the user's go-ahead.
- `type` sends text through `adb shell input`; don't type secrets the user hasn't provided.

## Development

`pytest` in this folder runs the unit tests on saved UIAutomator XML (`tests/fixtures/`) with a
fake Laya — no device, no model download (`pip install -r requirements-dev.txt`).
`pytest tests/device --device` adds read-only smoke tests against a connected device.

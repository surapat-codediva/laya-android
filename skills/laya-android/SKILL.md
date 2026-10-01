---
name: laya-android
description: Control an Android phone or emulator over adb (USB or wireless debugging) — read the screen, tap, type, swipe, and navigate toward a goal — using the local Laya decision model (convaiinnovations/laya) for fast element picking and yes/no screen checks, with Claude taking over whenever Laya is unsure. Use when the user asks to operate, navigate, tap through, or check something on an Android device/app, or mentions Laya with Android.
---

# laya-android

Laya is a **classifier** (322M multilingual / 421M English), not a GUI agent: it reads text and
answers typed questions (choice / yes-no) with a confidence. It cannot see pixels or invent
actions. This skill turns the screen into text with `uiautomator dump`, lets Laya make the cheap
decisions — next operation, tap target and "goal reached?" in one forward pass — and performs
actions with `adb`. **You (Claude) are the fallback** — exit code `3` means "Laya is unsure, you
decide".

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
| `la screen [--json]` | `snapshot <id>  app <package>`, `text:` the page's other text (title, body), then tappable elements `[N] kind "label" [on/off] (x,y)` | no |
| `la tap N` / `la tap X Y` | Tap element N from the **last** screen shown, or coordinates | no |
| `la type "text"` | Type into the focused field (ASCII only) | no |
| `la key back\|home\|enter\|del` | Press a key | no |
| `la swipe up\|down\|left\|right` | Scroll | no |
| `la screenshot [/tmp/x.png]` | Save PNG — then `Read` it. Only when `screen` is not enough (below) | no |
| `la goal "goal"` | Start a goal session: later actions are recorded toward it | no |
| `la finish ok\|fail [--note ".."]` | End the session with the outcome **you verified** | no |
| `la pick "goal"` | Laya's next operation + element. Prints JSON, **does not act** | yes |
| `la check "yes/no question"` | Laya P(true) about the screen. exit 0 yes / 1 no / 3 unsure | yes |
| `la verify [--text T] [--package P] [--element L]` | Exact check of the current screen. exit 0 all met / 1 not; JSON lists what is missing | no |
| `la run "goal" [--until-text T] [--until-package P] [--until-element L] [--max-steps 6]` | Loop: decide → act, until done (0) or unsure/stuck (3) | yes |

Laya's operations are `CLICK`, `SCROLL_DOWN`, `SCROLL_UP`, `BACK`, `DONE`; it does not type.
`pick`/`run` report `operation`, `op_confidence`, `target` (`id` + element) and
`target_confidence`; a step is confident only when the operation, and for `CLICK` the target,
reach `--min-confidence`. `target` is shown even when the operation is not `CLICK`.

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
`screen`, an action, `pick` or `run`) and re-checks the screen before tapping: if the element
moved it taps its new position; if it is gone or ambiguous (several elements with the same label
and the screen changed) it does not tap, exits `4`, and prints the current screen to pick from.
`run` applies the same check before each tap and re-decides when the target went stale.

**Screenshots cost far more than `screen`** (an image vs. a few lines of text). Take one only when:
- elements are icons with empty or meaningless labels (`ImageView "iv_3"`) you need to tell apart,
- `screen` shows no interactive elements (WebView, game, canvas, custom-drawn UI),
- the answer is visual (colour, image content, layout) or you are doing the final check of a
  result whose text alone is ambiguous.
A new page by itself is not a reason: its `text:` line and elements say where you are.

## Workflow

1. `la goal "<the user's goal>"` (skip if you start with `run`/`pick`, which set it).
2. `la screen` once to see where you are; after that, read the screen each action prints.
3. For a simple navigation goal, try `la run "goal" --until-text "<text only on the target screen>"`
   (add `--until-package`/`--until-element` to make it tighter).
   - exit `0`: the conditions held on screen; still glance at the printed result before reporting.
   - exit `3`: read the JSON `reason`/`steps`, then `screen` and continue with `tap N`.
4. For assertions in a flow, prefer `verify` (exact). `check` is a Laya hint only.
5. When you have verified the result on screen: `la finish ok` (or `la finish fail`).

## Trajectories

While a goal is set, every step — Laya's decisions in `run`, and your `tap`/`type`/`key`/`swipe`
— is appended to `<skill-dir>/trajectories/<session>.jsonl` with the screen it was taken on,
the action history, and Laya's prediction for that screen. A manual action after a handoff is a
correction label for fine-tuning Laya on mobile. Records hold on-screen text (the user's
messages, names, etc.) but never the text you `type`. Logs stay on this machine; set
`LAYA_ANDROID_LOG=off` to disable or `LAYA_ANDROID_LOG=<dir>` to move them.

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
- Never `run` on screens that confirm payments, orders, transfers, deletions, or PIN entry
  (a wrong PIN counts toward lockout) — do those steps manually and deliberately.
- `type` sends text through `adb shell input`; don't type secrets the user hasn't provided.

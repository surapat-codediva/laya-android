---
name: laya-android
description: Control an Android phone or emulator over adb (USB or wireless debugging) — read the screen, tap, type, swipe, and navigate toward a goal — using the local Laya decision model (convaiinnovations/laya) for fast element picking and yes/no screen checks, with Claude taking over whenever Laya is unsure. Use when the user asks to operate, navigate, tap through, or check something on an Android device/app, or mentions Laya with Android.
---

# laya-android

Laya is a ~400M-parameter **classifier**, not a GUI agent: it reads text and answers typed
questions (choice / yes-no) with a confidence. It cannot see pixels or invent actions. This
skill turns the screen into text with `uiautomator dump`, lets Laya make the cheap decisions,
and performs actions with `adb`. **You (Claude) are the fallback** — exit code `3` means
"Laya is unsure, you decide".

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
| `la screen [--json]` | List tappable elements: `[N] kind "label" (x,y)` | no |
| `la tap N` / `la tap X Y` | Tap element N from `screen`, or coordinates | no |
| `la type "text"` | Type into the focused field (ASCII only) | no |
| `la key back\|home\|enter\|del` | Press a key | no |
| `la swipe up\|down\|left\|right` | Scroll | no |
| `la screenshot [/tmp/x.png]` | Save PNG — then `Read` it to see the screen | no |
| `la pick "goal"` | Laya picks the element to tap next. Prints JSON, **does not tap** | yes |
| `la check "yes/no question"` | Laya P(true) about the screen. exit 0 yes / 1 no / 3 unsure | yes |
| `la run "goal" [--done "question"] [--max-steps 6]` | Loop: check done → pick → tap, until done (0) or unsure/stuck (3) | yes |

Options: `--model ml` (multilingual, default — handles Thai UI) or `--model en`;
`--min-confidence 0.6` for pick/run; `--yes 0.8` for check/run.
First Laya call downloads ~650 MB and each invocation loads the model (several seconds);
`run` loads it once for the whole loop.

## Workflow

1. `la screen` to see where you are (and `screenshot` + Read when labels are empty/icons).
2. For a simple navigation goal, try `la run "goal" --done "<question true only at the target>"`.
   - exit `0`: verify with `screen`/`screenshot` before reporting success.
   - exit `3`: read the JSON `reason`/`history`, then continue manually with `screen` + `tap N`.
3. For assertions in a flow, `check` is fine as a hint but confirm anything important yourself.

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

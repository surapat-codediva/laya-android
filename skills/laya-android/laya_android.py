#!/usr/bin/env python3
"""Drive an Android device with adb, using Laya as a fast local decision model.

Laya (convaiinnovations/laya) is a classifier: it reads text and answers typed
questions with a confidence. It cannot see pixels or generate actions, so this
script turns the screen into text (uiautomator dump), asks Laya -- in one forward
pass -- which operation comes next, which element to tap and whether the goal is
met, passes the action through a deterministic policy gate, and performs it with adb.

Exit codes: 0 ok / goal reached, 1 check/verify answered no, 2 usage or device error,
3 handoff -- Laya is unsure, stuck, or the policy gate stopped it; the caller decides,
4 stale -- the element is no longer on screen; run `screen` again.
"""
import argparse
import json
import logging
import os
import sys
import time

from laya_mobile import adb as adb_mod
from laya_mobile.checks import conditions, unmet
from laya_mobile.decision import LazyLaya, ask_check, confident, decide, summary
from laya_mobile.elements import describe, norm
from laya_mobile.errors import LayaAndroidError, UsageError
from laya_mobile.observe import resolve
from laya_mobile.policy import BLOCKED, SENSITIVE, PolicyGate
from laya_mobile.runner import HANDOFF, action_text, run_goal
from laya_mobile.session import Session
from laya_mobile.trajectory import action, typed

STALE = 4
log = logging.getLogger("laya_mobile")


# ---------- output ----------

def show(s, snap, as_json=False):
    """Print a snapshot and make it the one `tap N` refers to."""
    s.show(snap)
    s.save()
    if as_json:
        print(json.dumps(snap.to_dict(), ensure_ascii=False))
        return snap
    act = snap.activity or ""
    if act.startswith((snap.package or "") + "."):
        act = act[len(snap.package):]
    print("fingerprint %s  app %s%s" % (snap.fingerprint, snap.package, "  activity " + act if act else ""))
    # Titles and body text are not tappable but tell which page this is.
    labels = {norm(p) for e in snap.elements for p in e.label.split(" | ")}
    page = [p for p in snap.visible_text.split(" | ") if norm(p) not in labels]
    if page:
        print("text: " + " | ".join(page)[:500])
    for e in snap.elements:
        x, y = e.center
        print("[%d] %s (%d,%d)" % (e.id, describe(e), x, y))
    if not snap.elements:
        print("(no interactive elements -- try a screenshot)")
    return snap


def show_after(a, s):
    """After an action: let the screen settle, then print it, saving a separate `screen` call."""
    if a.no_screen:
        return None
    time.sleep(a.wait)
    print()
    return show(s, a.device.observe())


def warn_policy(op, el, snap):
    """Manual actions are the host's call; a sensitive one is flagged on stderr and in the trajectory."""
    p = PolicyGate().evaluate(op, el, snap)
    if not p.allowed:
        print("laya-android: policy %s for %s: %s (%s %r) -- make sure the user asked for this"
              % (p.action, action_text(op, el), p.reason, p.matched, p.keyword), file=sys.stderr)
    return p


def record_manual(s, op, el=None, before=None, after=None, policy=None, history_text=None, **extra):
    """Record a host-chosen action. When Laya decided on the same screen, the pair
    (model_decision, teacher_action) is a correction label for fine-tuning."""
    rec = s.recorder()
    if rec:
        laya = s.laya if before is not None and (s.laya or {}).get("fingerprint") == before.fingerprint else None
        teacher = action(op, el, **extra)
        outcome = {"screen_changed": after.fingerprint != before.fingerprint} if after and before else {}
        rec.step(s.next_step(), "agent", before, s.history, laya, teacher, dict(teacher, success=True), policy,
                 after, outcome)
    s.history.append(history_text or action_text(op, el))
    if after is None:
        s.acted = True  # element numbers from the last `screen` no longer apply
    s.save()


# ---------- commands ----------

def cmd_screen(a):
    show(Session.load(a.serial), a.device.observe(), a.json)


def cmd_tap(a):
    s = Session.load(a.serial)
    old = s.current()
    if a.y is not None:
        x, y = a.target, a.y
        hit = [e for e in (old.elements if old else [])
               if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
        el = min(hit, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]), default=None)
        policy = warn_policy("CLICK", el, old) if el else None
        a.device.tap(x, y)
        s.acted = True
        print("tapped (%d,%d)" % (x, y))
        after = show_after(a, s)
        return record_manual(s, "CLICK", el, old, after, policy, x=x, y=y)
    if not old:
        raise UsageError("run `screen` before `tap N`: N refers to the last `screen`, "
                         "and an action since then may have changed the screen")
    el = old.element(a.target)
    if el is None:
        raise UsageError("no element [%d] in the last `screen`" % a.target)
    fresh = a.device.observe()
    cur = resolve(el, old, fresh)
    if cur is None:
        print("stale: [%d] %s from screen %s is no longer on screen; not tapped. Current screen:\n"
              % (el.id, describe(el), old.fingerprint))
        show(s, fresh)
        return STALE
    policy = warn_policy("CLICK", cur, fresh)
    a.device.tap(*cur.center)
    s.acted = True
    print("tapped [%d] %s at (%d,%d)" % (el.id, describe(el, full=False), *cur.center))
    after = show_after(a, s)
    record_manual(s, "CLICK", el, old, after, policy)


def cmd_type(a):
    s = Session.load(a.serial)
    old = s.current()
    # After an action without a printed screen, the last screen seen still tells whether a
    # password field is around; with no screen at all, the text is redacted.
    seen = old or s.obs
    target = next((e for e in seen.elements if e.editable and e.focused), None) if old else None
    policy = warn_policy("TYPE", target, old) if target else None
    a.device.type_text(a.text)
    s.acted = True
    print("typed %d chars" % len(a.text))
    after = show_after(a, s)
    t = typed(a.text, target, seen)
    record_manual(s, "TYPE", target, old, after, policy, history_text="TYPE (%d chars)" % len(a.text), **t)


def cmd_key(a):
    s = Session.load(a.serial)
    old = s.current()
    key = a.key.upper()
    a.device.key(key)
    s.acted = True
    print("key %s" % key)
    after = show_after(a, s)
    record_manual(s, key if key in ("BACK", "HOME") else "KEY_" + key, None, old, after)


def cmd_swipe(a):
    s = Session.load(a.serial)
    old = s.current()
    a.device.swipe(a.direction)
    s.acted = True
    op = {"up": "SCROLL_DOWN", "down": "SCROLL_UP", "left": "SWIPE_LEFT", "right": "SWIPE_RIGHT"}[a.direction]
    print("swiped %s" % a.direction)
    after = show_after(a, s)
    record_manual(s, op, None, old, after)


def cmd_screenshot(a):
    print(a.device.screenshot(a.out))


def recording(a):
    return getattr(a, "record", False) or os.environ.get("LAYA_ANDROID_RECORD", "").lower() in ("1", "true", "yes")


def cmd_goal(a):
    s = Session.load(a.serial).start(a.goal, recording(a), a.trajectory, force=True)
    s.save()
    print("goal set (run %s)%s" % (s.id, ", recording to " + s.trajectory if s.trajectory else ""))


def cmd_finish(a):
    s = Session.load(a.serial)
    if not s.goal:
        raise UsageError("no goal set (`goal`, `pick` or `run` sets one)")
    rec = s.recorder()
    if rec:
        rec.end(a.outcome, a.note or "verified by the agent")
    print("run %s finished: %s" % (s.id, a.outcome))
    s.finish()
    s.save()


def cmd_pick(a):
    s = Session.load(a.serial).start(a.goal)
    snap = a.device.observe()
    d = decide(LazyLaya(a.model), a.goal, snap, s.history, limit=a.candidates)
    s.show(snap)
    s.laya = d
    s.save()
    el = snap.element(d["target"]) if d["operation"] == "CLICK" else None
    policy = PolicyGate().evaluate(d["operation"], el, snap)
    print(json.dumps(dict(summary(d, snap), policy=policy.to_dict()), ensure_ascii=False))
    return 0 if confident(d, a.min_confidence) and policy.allowed else HANDOFF


def cmd_check(a):
    p = ask_check(LazyLaya(a.model), a.question, a.device.observe())
    print(json.dumps({"p_true": p}))
    if p >= a.yes:
        return 0
    return 1 if p <= 1 - a.yes else HANDOFF


def cmd_verify(a):
    cond = conditions(a.until_text, a.until_package, a.until_element)
    if not any(cond.values()):
        raise UsageError("verify needs at least one of --text, --package, --element")
    miss = unmet(a.device.observe(), cond)
    print(json.dumps({"met": not miss, "unmet": miss}, ensure_ascii=False))
    return 1 if miss else 0


def cmd_run(a):
    s = Session.load(a.serial).start(a.goal, recording(a), a.trajectory)
    cond = conditions(a.until_text, a.until_package, a.until_element)
    r = run_goal(a.device, LazyLaya(a.model), s, a.goal, cond if any(cond.values()) else None, a.done,
                 a.max_steps, a.min_confidence, a.yes, a.wait, a.candidates, PolicyGate(a.allow or ()))
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    return r.code


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial", default=os.environ.get("ANDROID_SERIAL"), help="adb device serial")
    ap.add_argument("--model", choices=["ml", "en"], default="ml",
                    help="Laya checkpoint: ml = multilingual (Thai OK, default), en = English")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="diagnostics on stderr: fingerprint, candidates, decisions, policy, screen changes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    acting = argparse.ArgumentParser(add_help=False)
    acting.add_argument("--wait", type=float, default=1.0, help="seconds to let the screen settle before printing it")
    acting.add_argument("--no-screen", action="store_true", help="do not print the screen after the action")
    rec = argparse.ArgumentParser(add_help=False)
    rec.add_argument("--record", action="store_true",
                     help="record a JSONL trajectory under $LAYA_ANDROID_DATA_DIR/trajectories (~/.laya-android)")
    rec.add_argument("--trajectory", metavar="PATH", help="record the trajectory to this JSONL file")
    laya = argparse.ArgumentParser(add_help=False)
    laya.add_argument("--min-confidence", type=float, default=0.6)
    laya.add_argument("--candidates", type=int, default=20, help="most elements offered to Laya as tap targets")

    s = sub.add_parser("screen", help="list interactive elements")
    s.add_argument("--json", action="store_true", help="the full structured snapshot")
    s.set_defaults(fn=cmd_screen)

    s = sub.add_parser("tap", parents=[acting], help="tap element N (from the last `screen`) or coordinates X Y")
    s.add_argument("target", type=int)
    s.add_argument("y", type=int, nargs="?")
    s.set_defaults(fn=cmd_tap)

    s = sub.add_parser("type", parents=[acting], help="type ASCII text into the focused field")
    s.add_argument("text")
    s.set_defaults(fn=cmd_type)

    s = sub.add_parser("key", parents=[acting], help="press a key: back, home, enter, del, tab ...")
    s.add_argument("key")
    s.set_defaults(fn=cmd_key)

    s = sub.add_parser("swipe", parents=[acting], help="scroll the screen")
    s.add_argument("direction", choices=["up", "down", "left", "right"])
    s.set_defaults(fn=cmd_swipe)

    s = sub.add_parser("screenshot", help="save a PNG of the screen")
    s.add_argument("out", nargs="?", default="/tmp/laya-android.png")
    s.set_defaults(fn=cmd_screenshot)

    s = sub.add_parser("goal", parents=[rec], help="start a goal run; later actions are attributed to it")
    s.add_argument("goal")
    s.set_defaults(fn=cmd_goal)

    s = sub.add_parser("finish", help="end the goal run with its verified outcome")
    s.add_argument("outcome", choices=["ok", "fail"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_finish)

    s = sub.add_parser("pick", parents=[laya], help="ask Laya for the next operation and element (does not act)")
    s.add_argument("goal")
    s.set_defaults(fn=cmd_pick)

    s = sub.add_parser("check", help="ask Laya a yes/no question about the screen")
    s.add_argument("question")
    s.add_argument("--yes", type=float, default=0.8, help="P(true) needed for yes (exit 0)")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("verify", help="check the screen against exact conditions (no Laya); exit 0 met / 1 not")
    s.add_argument("--text", dest="until_text", action="append", metavar="TEXT", help="text on screen (repeatable)")
    s.add_argument("--package", dest="until_package", metavar="PKG", help="app in front")
    s.add_argument("--element", dest="until_element", action="append", metavar="LABEL",
                   help="tappable element whose label contains this (repeatable)")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("run", parents=[rec, laya], help="let Laya act toward a goal until done or unsure")
    s.add_argument("goal")
    s.add_argument("--done", help="yes/no question that is true when the goal is reached")
    s.add_argument("--until-text", action="append", metavar="TEXT",
                   help="goal is reached only when this text is on screen (repeatable; replaces the done check)")
    s.add_argument("--until-package", metavar="PKG", help="... and this app is in front")
    s.add_argument("--until-element", action="append", metavar="LABEL",
                   help="... and a tappable element's label contains this (repeatable)")
    s.add_argument("--max-steps", type=int, default=6)
    s.add_argument("--yes", type=float, default=0.8)
    s.add_argument("--wait", type=float, default=1.5, help="seconds to wait after each action")
    s.add_argument("--allow", action="append", choices=sorted(set(SENSITIVE) - BLOCKED), metavar="CATEGORY",
                   help="let the policy gate pass this sensitive category: %(choices)s (repeatable). "
                        "factory_reset and account_removal are never allowed")
    s.set_defaults(fn=cmd_run)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("laya-android: %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO if a.verbose else logging.WARNING)
    try:
        a.serial = a.serial or adb_mod.pick_serial()
        a.device = adb_mod.Device(a.serial)
        return a.fn(a) or 0
    except LayaAndroidError as e:
        print("laya-android: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Drive an Android device with adb, using Laya as a fast local decision model.

Laya (convaiinnovations/laya) is a classifier: it reads text and answers typed
questions with a confidence. It cannot see pixels or generate actions, so this
script turns the screen into text (uiautomator dump), asks Laya -- in one forward
pass -- which operation comes next, which element to tap and whether the goal is
met, and performs the action with adb.

Exit codes: 0 ok / goal reached, 2 usage or device error,
3 handoff -- Laya is unsure or stuck; the caller (Claude) should decide,
4 stale -- the element is no longer on screen; run `screen` again.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HANDOFF = 3
STALE = 4
HISTORY = 5  # recent actions shown to Laya
SYSTEMUI = "com.android.systemui"
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------- adb ----------

def adb(serial, *args, capture=True):
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode != 0:
        sys.exit("adb failed: %s\n%s" % (" ".join(cmd), (r.stderr or "").strip()))
    return r.stdout


def pick_serial():
    """The one device to use when -s is not given.

    Wireless debugging lists the same phone twice: as ip:port and as an mDNS
    `adb-<serialno>-xxx._adb-tls-connect._tcp` name. Those count as one device; the
    mDNS name is preferred because it survives the port changing on reconnect.
    """
    lines = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout.splitlines()[1:]
    serials = [l.split("\t")[0] for l in lines if l.endswith("\tdevice")]
    if not serials:
        sys.exit("no adb device. Wireless: enable Wireless debugging, then `adb pair ip:port` "
                 "(once) and `adb connect ip:port`")
    phones = {}
    for s in serials:
        hw = subprocess.run(["adb", "-s", s, "shell", "getprop", "ro.serialno"],
                            capture_output=True, text=True).stdout.strip() or s
        phones.setdefault(hw, []).append(s)
    if len(phones) > 1:
        sys.exit("several devices connected, pass -s: %s" % ", ".join(serials))
    (names,) = phones.values()
    return next((s for s in names if "._adb-tls-connect." in s), names[0])


def dump_xml(serial):
    for attempt in range(2):
        out = adb(serial, "exec-out", "uiautomator", "dump", "/dev/tty")
        # uiautomator appends "UI hierchary dumped to: /dev/tty" after the XML.
        end = out.rfind("</hierarchy>")
        if end >= 0:
            break
        time.sleep(1)  # "null root node" while the screen is animating or waking
    else:
        sys.exit("uiautomator dump returned no hierarchy (screen off/locked or secure window?): %s" % out.strip())
    return out[out.find("<"):end + len("</hierarchy>")]


def tap(serial, x, y):
    adb(serial, "shell", "input", "tap", str(x), str(y))


def swipe(serial, direction):
    size = adb(serial, "shell", "wm", "size").strip().rsplit(" ", 1)[-1]
    w, h = map(int, size.split("x"))
    cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
    # "up" scrolls content up, i.e. the finger moves from bottom to top.
    x1, y1, x2, y2 = {
        "up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
        "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy),
    }[direction]
    adb(serial, "shell", "input", "swipe", *map(str, (x1, y1, x2, y2, 300)))


# ---------- screen -> observation ----------

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _label(node):
    own = (node.get("text") or node.get("content-desc") or "").strip()
    if own:
        return own
    # React Native puts the text in child TextViews of a clickable ViewGroup.
    parts = [(n.get("text") or n.get("content-desc") or "").strip() for n in node.iter()]
    return " | ".join(p for p in parts if p)


def elements(root):
    """Interactive elements on screen, in document order."""
    out = []
    for node in root.iter("node"):
        cls = node.get("class", "").rsplit(".", 1)[-1]
        editable = "EditText" in cls
        if node.get("clickable") != "true" and not editable:
            continue
        m = BOUNDS.match(node.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        rid = node.get("resource-id", "")
        label = (_label(node) or rid.rsplit("/", 1)[-1])[:80]
        if not label:
            continue
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        # Nested clickable wrappers share a centre; one tap target is enough.
        if any(abs(e["x"] - x) <= 4 and abs(e["y"] - y) <= 4 for e in out):
            continue
        el = {
            "id": len(out),
            "kind": "input" if editable else cls or "View",
            "label": label,
            "x": x,
            "y": y,
            "bounds": [x1, y1, x2, y2],
            # Identifies the same element across dumps; position is not part of it.
            "key": "%s|%s|%s" % (cls, rid, label),
        }
        if node.get("checkable") == "true":
            el["checked"] = node.get("checked") == "true"
        if node.get("selected") == "true":
            el["selected"] = True
        out.append(el)
    return out


def visible_text(nodes, limit=1500):
    seen, parts = set(), []
    for node in nodes:
        t = (node.get("text") or node.get("content-desc") or "").strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    return " | ".join(parts)[:limit]


def observe(serial):
    """One screen observation: app package, visible text, interactive elements and a snapshot id."""
    root = ET.fromstring(dump_xml(serial))
    nodes = list(root.iter("node"))
    pkgs = [n.get("package") for n in nodes if n.get("package")]
    pkg = next((p for p in pkgs if p != SYSTEMUI), pkgs[0] if pkgs else "")
    els = elements(root)
    # Text and snapshot id leave out the status bar: its clock would change the id every minute.
    # The notification shade is all SystemUI, so then it stays.
    app_nodes = [n for n in nodes if n.get("package") != SYSTEMUI] or nodes
    app_text = [n.get("text") or n.get("content-desc") or "" for n in app_nodes]
    blob = json.dumps([pkg, app_text, [[e["key"], e["bounds"], e.get("checked"), e.get("selected")] for e in els]],
                      ensure_ascii=False)
    return {"snapshot": hashlib.sha1(blob.encode()).hexdigest()[:10], "package": pkg,
            "text": visible_text(app_nodes), "elements": els}


def resolve(el, old, new):
    """Element `el` of observation `old` as it is in the fresh observation `new`; None if gone or ambiguous."""
    if new["snapshot"] == old["snapshot"]:
        return new["elements"][el["id"]]
    # A key repeated on either screen (three "Buy" buttons) is ambiguous once the screen changed:
    # after a list shift, the button in the same place can belong to another item.
    same = [e for e in new["elements"] if e["key"] == el["key"]]
    if len(same) == 1 and sum(e["key"] == el["key"] for e in old["elements"]) == 1:
        return same[0]
    return None


def describe(el):
    s = "%s \"%s\"" % (el["kind"], el["label"])
    if "checked" in el:
        s += " [on]" if el["checked"] else " [off]"
    if el.get("selected"):
        s += " [selected]"
    return s


# ---------- session + trajectory log ----------
# The session remembers, per device, the last observation the caller saw (so `tap N`
# refers to it), the current goal and the actions taken toward it. Every step toward a
# goal is appended to trajectories/<session>.jsonl: Laya's prediction next to the action
# actually taken is the training data for a mobile fine-tune.

def session_path(serial):
    return os.path.join(tempfile.gettempdir(), "laya-android-%s.json" % re.sub(r"[^\w.-]", "_", serial))


def new_session_id():
    return time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()


def load_session(serial):
    try:
        with open(session_path(serial), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if not s.get("id"):
        s.update(id=new_session_id(), goal=None, history=[])
    return s


def save_session(serial, s):
    path = session_path(serial)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def start_session(serial, goal):
    s = load_session(serial)
    if s.get("goal") != goal:
        s.update(id=new_session_id(), goal=goal, history=[])
    return s


def log(s, record):
    """Append a record to the session's trajectory file. Nothing is logged without a goal."""
    where = os.environ.get("LAYA_ANDROID_LOG", os.path.join(SKILL_DIR, "trajectories"))
    if not s.get("goal") or where.lower() in ("", "0", "off"):
        return
    record = dict({"t": round(time.time(), 3), "session": s["id"], "goal": s["goal"]}, **record)
    try:
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, s["id"] + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print("laya-android: trajectory log failed: %s" % e, file=sys.stderr)


def action_record(op, el=None, **extra):
    r = {"op": op}
    if el:
        r.update(target=el["id"], key=el["key"], label=describe(el))
    r.update(extra)
    return r


def action_text(op, el=None):
    return "%s %s" % (op, describe(el)) if el else op


def record_agent(serial, s, op, el=None, history_text=None, **extra):
    """Log an action the caller chose -- a teacher label where Laya was unsure -- and add it to the history."""
    obs = None if s.get("acted") else s.get("obs")
    laya = s.get("laya") if obs and (s.get("laya") or {}).get("snapshot") == obs["snapshot"] else None
    log(s, {"event": "step", "actor": "agent", "obs": obs, "history": list(s["history"]), "laya": laya,
            "action": action_record(op, el, **extra), "executed": True})
    s["history"].append(history_text or action_text(op, el))
    s["acted"] = True  # element numbers from the last `screen` no longer apply
    save_session(serial, s)


# ---------- Laya ----------

_agent = None

OPERATIONS = {
    "CLICK": "tap one of the on-screen elements",
    "SCROLL_DOWN": "scroll to reveal content further down",
    "SCROLL_UP": "scroll to reveal content further up",
    "BACK": "go back to the previous screen",
    "DONE": "the goal is already reached on the current screen",
}


def agent(model):
    global _agent
    if _agent is None:
        os.environ.setdefault("USE_TF", "0")  # avoids an abseil deadlock if TF is installed
        import laya
        sub = "multilingual" if model == "ml" else None
        _agent = laya.load("convaiinnovations/laya", subfolder=sub)
    return _agent


def laya_state(obs, history):
    lines = ["app: %s" % obs["package"]]
    if history:
        lines.append("recent actions: " + "; ".join(history[-HISTORY:]))
    lines.append("screen: " + obs["text"])
    return "\n".join(lines)


def decide(model, goal, obs, history, done_q=None):
    """Next operation, tap target and (with done_q) P(goal reached) -- one Laya forward pass."""
    els = obs["elements"]
    q = {"operation": {
        "type": "choice",
        "instructions": "Goal: %s. What should be done next on this screen to make progress toward the goal?" % goal,
        "criteria": {k: v for k, v in OPERATIONS.items() if k != "CLICK" or els},
    }}
    if els:
        q["target"] = {
            "type": "choice",
            "instructions": "Goal: %s. If an element should be tapped next, which one?" % goal,
            "criteria": {str(e["id"]): describe(e) for e in els},
        }
    if done_q:
        q["done"] = {"type": "noul", "instructions": done_q}
    t0 = time.monotonic()
    ans = agent(model).predict(laya_state(obs, history), q)["answers"]
    d = {"snapshot": obs["snapshot"], "operation": ans["operation"]["choice"],
         "op_confidence": round(float(ans["operation"]["confidence"]), 3)}
    if els:
        d["target"] = int(ans["target"]["choice"])
        d["target_confidence"] = round(float(ans["target"]["confidence"]), 3)
    if done_q:
        d["p_done"] = round(float(ans["done"]["noul"]), 3)
    d["ms"] = round((time.monotonic() - t0) * 1000)
    return d


def confident(d, min_confidence):
    return d["op_confidence"] >= min_confidence and (
        d["operation"] != "CLICK" or d["target_confidence"] >= min_confidence)


def summary(d, obs):
    out = {k: v for k, v in d.items() if k != "target"}
    if "target" in d:
        el = obs["elements"][d["target"]]
        out["target"] = {"id": el["id"], "element": describe(el)}
    return out


def ask_check(model, question, obs):
    q = {"q": {"type": "noul", "instructions": question}}
    a = agent(model).predict(laya_state(obs, []), q)["answers"]["q"]
    return round(float(a["noul"]), 3)


# ---------- deterministic goal check ----------
# Laya's "goal reached?" is a guess; these conditions are read straight off the screen.

def _norm(t):
    return " ".join(t.split()).casefold()


def conditions(a):
    return {"text": a.until_text or [], "package": a.until_package, "element": a.until_element or []}


def unmet(obs, cond):
    """The conditions `obs` does not meet; [] means the goal is reached. All conditions must hold."""
    labels = [_norm(e["label"]) for e in obs["elements"]]
    screen = _norm(obs["text"])
    miss = []
    if cond["package"] and obs["package"] != cond["package"]:
        miss.append("app is %s, not %s" % (obs["package"], cond["package"]))
    for t in cond["text"]:
        if _norm(t) not in screen and not any(_norm(t) in l for l in labels):
            miss.append('no text "%s"' % t)
    for t in cond["element"]:
        if not any(_norm(t) in l for l in labels):
            miss.append('no element "%s"' % t)
    return miss


def act(serial, op, el=None):
    if op == "CLICK":
        tap(serial, el["x"], el["y"])
    elif op == "SCROLL_DOWN":
        swipe(serial, "up")
    elif op == "SCROLL_UP":
        swipe(serial, "down")
    elif op == "BACK":
        adb(serial, "shell", "input", "keyevent", "KEYCODE_BACK")


# ---------- commands ----------

def show(serial, obs, as_json=False):
    """Print an observation and make it the one `tap N` refers to."""
    s = load_session(serial)
    s.update(obs=obs, acted=False)
    save_session(serial, s)
    if as_json:
        print(json.dumps({k: obs[k] for k in ("snapshot", "package", "text", "elements")}, ensure_ascii=False))
        return
    print("snapshot %s  app %s" % (obs["snapshot"], obs["package"]))
    # Titles and body text are not tappable but tell which page this is.
    labels = {p for e in obs["elements"] for p in e["label"].split(" | ")}
    page = [p for p in obs["text"].split(" | ") if p not in labels]
    if page:
        print("text: " + " | ".join(page)[:500])
    for e in obs["elements"]:
        print("[%d] %s (%d,%d)" % (e["id"], describe(e), e["x"], e["y"]))
    if not obs["elements"]:
        print("(no interactive elements -- try a screenshot)")


def show_after(a):
    """After an action: let the screen settle, then print it, saving a separate `screen` call."""
    if a.no_screen:
        return
    time.sleep(a.wait)
    print()
    show(a.serial, observe(a.serial))


def cmd_screen(a):
    show(a.serial, observe(a.serial), a.json)


def cmd_tap(a):
    s = load_session(a.serial)
    old = None if s.get("acted") else s.get("obs")
    if a.y is not None:
        x, y = a.target, a.y
        hit = [e for e in (old or {}).get("elements", [])
               if e["bounds"][0] <= x < e["bounds"][2] and e["bounds"][1] <= y < e["bounds"][3]]
        el = min(hit, key=lambda e: (e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1]),
                 default=None)
        tap(a.serial, x, y)
        record_agent(a.serial, s, "CLICK", el, x=x, y=y)
        print("tapped (%d,%d)" % (x, y))
        return show_after(a)
    if not old:
        sys.exit("run `screen` before `tap N`: N refers to the last `screen`, "
                 "and an action since then may have changed the screen")
    if not 0 <= a.target < len(old["elements"]):
        sys.exit("no element [%d] in the last `screen`" % a.target)
    el = old["elements"][a.target]
    fresh = observe(a.serial)
    cur = resolve(el, old, fresh)
    if cur is None:
        print("stale: [%d] %s from snapshot %s is no longer on screen; not tapped. Current screen:\n"
              % (el["id"], describe(el), old["snapshot"]))
        show(a.serial, fresh)
        return STALE
    tap(a.serial, cur["x"], cur["y"])
    record_agent(a.serial, s, "CLICK", el)
    print("tapped [%d] %s at (%d,%d)" % (el["id"], describe(el), cur["x"], cur["y"]))
    show_after(a)


def cmd_type(a):
    # `input text` needs spaces as %s and cannot type non-ASCII (e.g. Thai).
    if any(ord(c) > 127 for c in a.text):
        sys.exit("adb `input text` is ASCII-only; use an IME such as ADBKeyboard for non-ASCII text")
    adb(a.serial, "shell", "input", "text", a.text.replace(" ", "%s"))
    # The text itself is not logged: it may be a secret.
    record_agent(a.serial, load_session(a.serial), "TYPE", chars=len(a.text),
                 history_text="TYPE (%d chars)" % len(a.text))
    print("typed %d chars" % len(a.text))
    show_after(a)


def cmd_key(a):
    key = a.key.upper()
    adb(a.serial, "shell", "input", "keyevent", "KEYCODE_" + key)
    record_agent(a.serial, load_session(a.serial), key if key in ("BACK", "HOME") else "KEY_" + key)
    print("key %s" % key)
    show_after(a)


def cmd_swipe(a):
    swipe(a.serial, a.direction)
    op = {"up": "SCROLL_DOWN", "down": "SCROLL_UP", "left": "SWIPE_LEFT", "right": "SWIPE_RIGHT"}[a.direction]
    record_agent(a.serial, load_session(a.serial), op)
    print("swiped %s" % a.direction)
    show_after(a)


def cmd_screenshot(a):
    with open(a.out, "wb") as f:
        r = subprocess.run(["adb"] + (["-s", a.serial] if a.serial else []) + ["exec-out", "screencap", "-p"],
                           stdout=f)
    if r.returncode != 0:
        sys.exit("screencap failed")
    print(a.out)


def cmd_goal(a):
    s = load_session(a.serial)
    s.update(id=new_session_id(), goal=a.goal, history=[])
    save_session(a.serial, s)
    print("goal set (session %s)" % s["id"])


def cmd_finish(a):
    s = load_session(a.serial)
    if not s.get("goal"):
        sys.exit("no goal set (`goal`, `pick` or `run` sets one)")
    log(s, {"event": "end", "outcome": a.outcome, "note": a.note})
    print("session %s finished: %s" % (s["id"], a.outcome))
    s.update(id=new_session_id(), goal=None, history=[])
    save_session(a.serial, s)


def cmd_pick(a):
    s = start_session(a.serial, a.goal)
    obs = observe(a.serial)
    d = decide(a.model, a.goal, obs, s["history"])
    s.update(obs=obs, acted=False, laya=d)
    save_session(a.serial, s)
    print(json.dumps(summary(d, obs), ensure_ascii=False))
    return 0 if confident(d, a.min_confidence) else HANDOFF


def cmd_check(a):
    p = ask_check(a.model, a.question, observe(a.serial))
    print(json.dumps({"p_true": p}))
    if p >= a.yes:
        return 0
    return 1 if p <= 1 - a.yes else HANDOFF


def cmd_verify(a):
    cond = conditions(a)
    if not any(cond.values()):
        sys.exit("verify needs at least one of --text, --package, --element")
    miss = unmet(observe(a.serial), cond)
    print(json.dumps({"met": not miss, "unmet": miss}, ensure_ascii=False))
    return 1 if miss else 0


def cmd_run(a):
    s = start_session(a.serial, a.goal)
    done_q = a.done or "Has this goal been reached on the current screen: %s?" % a.goal
    # With --until-* the goal is reached only when the screen meets them; Laya's p_done is then just logged.
    cond = conditions(a) if any(conditions(a).values()) else None
    steps = []
    obs = observe(a.serial)
    for step in range(1, a.max_steps + 2):
        if cond:
            miss = unmet(obs, cond)
            if not miss:
                s.update(obs=obs, acted=False)
                log(s, {"event": "verified", "obs": obs, "until": cond})
                return _stop(a, s, "goal verified on screen", steps, 0)
        if step > a.max_steps:
            break
        d = decide(a.model, a.goal, obs, s["history"], done_q)
        s.update(obs=obs, acted=False, laya=d)
        el = obs["elements"][d["target"]] if d["operation"] == "CLICK" else None
        rec = {"event": "step", "actor": "laya", "obs": obs, "history": list(s["history"]), "laya": d,
               "action": action_record(d["operation"], el), "executed": False}
        steps.append(dict(summary(d, obs), step=step))
        if not cond and d["p_done"] >= a.yes:
            log(s, dict(rec, note="done check"))
            return _stop(a, s, "goal reached (p=%.3f)" % d["p_done"], steps, 0)
        if d["operation"] == "DONE" and cond:
            reason = "Laya chose DONE but the screen does not meet the goal: " + "; ".join(miss)
        elif d["operation"] == "DONE":
            reason = "Laya chose DONE but the done check is unsure (p=%.3f)" % d["p_done"]
        elif not confident(d, a.min_confidence):
            reason = "low confidence on step %d" % step
        else:
            reason = None
        if reason:
            log(s, dict(rec, note=reason))
            return _stop(a, s, reason, steps, HANDOFF)
        before = obs
        if el:
            # Laya decided on `obs`; tap only if that element is still there.
            before = observe(a.serial)
            cur = resolve(el, obs, before)
            if cur is None:
                log(s, dict(rec, note="stale"))
                steps[-1]["stale"] = True
                obs = before
                s["obs"] = obs
                continue
            el = cur
        act(a.serial, d["operation"], el)
        log(s, dict(rec, executed=True))
        s["history"].append(action_text(d["operation"], el))
        print("step %d: %s (%dms)" % (step, action_text(d["operation"], el), d["ms"]), file=sys.stderr)
        time.sleep(a.wait)
        obs = observe(a.serial)
        s.update(obs=obs, acted=False)
        if obs["snapshot"] == before["snapshot"]:
            return _stop(a, s, "screen did not change after %s" % action_text(d["operation"], el), steps, HANDOFF)
    return _stop(a, s, "max steps reached", steps, HANDOFF)


def _stop(a, s, reason, steps, code):
    save_session(a.serial, s)
    result = "done" if code == 0 else "handoff"
    log(s, {"event": "run_end", "result": result, "reason": reason})
    print(json.dumps({"result": result, "reason": reason, "snapshot": s["obs"]["snapshot"], "steps": steps},
                     ensure_ascii=False, indent=2))
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial", default=os.environ.get("ANDROID_SERIAL"), help="adb device serial")
    ap.add_argument("--model", choices=["ml", "en"], default="ml",
                    help="Laya checkpoint: ml = multilingual (Thai OK, default), en = English")
    sub = ap.add_subparsers(dest="cmd", required=True)
    acting = argparse.ArgumentParser(add_help=False)
    acting.add_argument("--wait", type=float, default=1.0, help="seconds to let the screen settle before printing it")
    acting.add_argument("--no-screen", action="store_true", help="do not print the screen after the action")

    s = sub.add_parser("screen", help="list interactive elements")
    s.add_argument("--json", action="store_true")
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

    s = sub.add_parser("goal", help="start a goal session; later actions are recorded toward it")
    s.add_argument("goal")
    s.set_defaults(fn=cmd_goal)

    s = sub.add_parser("finish", help="end the goal session with its verified outcome")
    s.add_argument("outcome", choices=["ok", "fail"])
    s.add_argument("--note")
    s.set_defaults(fn=cmd_finish)

    s = sub.add_parser("pick", help="ask Laya for the next operation and element (does not act)")
    s.add_argument("goal")
    s.add_argument("--min-confidence", type=float, default=0.6)
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

    s = sub.add_parser("run", help="let Laya act toward a goal until done or unsure")
    s.add_argument("goal")
    s.add_argument("--done", help="yes/no question that is true when the goal is reached")
    s.add_argument("--until-text", action="append", metavar="TEXT",
                   help="goal is reached only when this text is on screen (repeatable; replaces the done check)")
    s.add_argument("--until-package", metavar="PKG", help="... and this app is in front")
    s.add_argument("--until-element", action="append", metavar="LABEL",
                   help="... and a tappable element's label contains this (repeatable)")
    s.add_argument("--max-steps", type=int, default=6)
    s.add_argument("--min-confidence", type=float, default=0.6)
    s.add_argument("--yes", type=float, default=0.8)
    s.add_argument("--wait", type=float, default=1.5, help="seconds to wait after each action")
    s.set_defaults(fn=cmd_run)

    a = ap.parse_args()
    a.serial = a.serial or pick_serial()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()

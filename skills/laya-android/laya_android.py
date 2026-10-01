#!/usr/bin/env python3
"""Drive an Android device with adb, using Laya as a fast local decision model.

Laya (convaiinnovations/laya) is a classifier: it reads text and answers typed
questions with a confidence. It cannot see pixels or generate actions, so this
script turns the screen into text (uiautomator dump), asks Laya which element to
tap or whether a goal is met, and performs the tap with adb.

Exit codes: 0 ok / goal reached, 2 usage or device error,
3 handoff -- Laya is unsure or stuck; the caller (Claude) should decide.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HANDOFF = 3


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


# ---------- screen -> elements ----------

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _label(node):
    own = (node.get("text") or node.get("content-desc") or "").strip()
    if own:
        return own
    # React Native puts the text in child TextViews of a clickable ViewGroup.
    parts = [(n.get("text") or n.get("content-desc") or "").strip() for n in node.iter()]
    return " | ".join(p for p in parts if p)


def elements(xml):
    """Interactive elements on screen, in document order."""
    out = []
    for node in ET.fromstring(xml).iter("node"):
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
        label = _label(node) or node.get("resource-id", "").rsplit("/", 1)[-1]
        if not label:
            continue
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        # Nested clickable wrappers share a centre; one tap target is enough.
        if any(abs(e["x"] - x) <= 4 and abs(e["y"] - y) <= 4 for e in out):
            continue
        out.append({
            "id": len(out),
            "kind": "input" if editable else cls or "View",
            "label": label[:80],
            "x": x,
            "y": y,
        })
    return out


def visible_text(xml, limit=1500):
    seen, parts = set(), []
    for node in ET.fromstring(xml).iter("node"):
        t = (node.get("text") or node.get("content-desc") or "").strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    return " | ".join(parts)[:limit]


def describe(el):
    return "%s \"%s\"" % (el["kind"], el["label"])


# ---------- Laya ----------

_agent = None


def agent(model):
    global _agent
    if _agent is None:
        os.environ.setdefault("USE_TF", "0")  # avoids an abseil deadlock if TF is installed
        import laya
        sub = "multilingual" if model == "ml" else None
        _agent = laya.load("convaiinnovations/laya", subfolder=sub)
    return _agent


def ask_pick(model, goal, xml, els):
    criteria = {str(e["id"]): describe(e) for e in els}
    q = {"next": {
        "type": "choice",
        "instructions": "Goal: %s. Which on-screen element should be tapped next to make progress toward the goal?" % goal,
        "criteria": criteria,
    }}
    a = agent(model).predict(visible_text(xml), q)["answers"]["next"]
    el = els[int(a["choice"])]
    return {"element": el, "confidence": round(float(a["confidence"]), 3)}


def ask_check(model, question, xml):
    q = {"q": {"type": "noul", "instructions": question}}
    a = agent(model).predict(visible_text(xml), q)["answers"]["q"]
    return round(float(a["noul"]), 3)


# ---------- commands ----------

def cmd_screen(a):
    xml = dump_xml(a.serial)
    els = elements(xml)
    if a.json:
        print(json.dumps(els, ensure_ascii=False))
        return
    for e in els:
        print("[%d] %s (%d,%d)" % (e["id"], describe(e), e["x"], e["y"]))
    if not els:
        print("(no interactive elements -- try a screenshot)")


def cmd_tap(a):
    if a.y is not None:
        x, y = a.target, a.y
    else:
        els = elements(dump_xml(a.serial))
        if not 0 <= a.target < len(els):
            sys.exit("no element [%d]; run `screen` first" % a.target)
        x, y = els[a.target]["x"], els[a.target]["y"]
    adb(a.serial, "shell", "input", "tap", str(x), str(y))
    print("tapped (%d,%d)" % (x, y))


def cmd_type(a):
    # `input text` needs spaces as %s and cannot type non-ASCII (e.g. Thai).
    if any(ord(c) > 127 for c in a.text):
        sys.exit("adb `input text` is ASCII-only; use an IME such as ADBKeyboard for non-ASCII text")
    adb(a.serial, "shell", "input", "text", a.text.replace(" ", "%s"))
    print("typed %d chars" % len(a.text))


def cmd_key(a):
    adb(a.serial, "shell", "input", "keyevent", "KEYCODE_" + a.key.upper())
    print("key %s" % a.key.upper())


def cmd_swipe(a):
    size = adb(a.serial, "shell", "wm", "size").strip().rsplit(" ", 1)[-1]
    w, h = map(int, size.split("x"))
    cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
    # "up" scrolls content up, i.e. the finger moves from bottom to top.
    x1, y1, x2, y2 = {
        "up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
        "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy),
    }[a.direction]
    adb(a.serial, "shell", "input", "swipe", *map(str, (x1, y1, x2, y2, 300)))
    print("swiped %s" % a.direction)


def cmd_screenshot(a):
    with open(a.out, "wb") as f:
        r = subprocess.run(["adb"] + (["-s", a.serial] if a.serial else []) + ["exec-out", "screencap", "-p"],
                           stdout=f)
    if r.returncode != 0:
        sys.exit("screencap failed")
    print(a.out)


def cmd_pick(a):
    xml = dump_xml(a.serial)
    els = elements(xml)
    if not els:
        print(json.dumps({"handoff": "no interactive elements"}))
        return HANDOFF
    p = ask_pick(a.model, a.goal, xml, els)
    print(json.dumps(p, ensure_ascii=False))
    return 0 if p["confidence"] >= a.min_confidence else HANDOFF


def cmd_check(a):
    p = ask_check(a.model, a.question, dump_xml(a.serial))
    print(json.dumps({"p_true": p}))
    if p >= a.yes:
        return 0
    return 1 if p <= 1 - a.yes else HANDOFF


def cmd_run(a):
    done_q = a.done or "Has this goal been reached on the current screen: %s?" % a.goal
    last_xml, history = None, []
    for step in range(1, a.max_steps + 1):
        xml = dump_xml(a.serial)
        if xml == last_xml:
            return _stop("screen did not change after the last tap", history, HANDOFF)
        p_done = ask_check(a.model, done_q, xml)
        if p_done >= a.yes:
            return _stop("goal reached (p=%.3f)" % p_done, history, 0)
        els = elements(xml)
        if not els:
            return _stop("no interactive elements", history, HANDOFF)
        p = ask_pick(a.model, a.goal, xml, els)
        el = p["element"]
        history.append({"step": step, "tap": describe(el), "confidence": p["confidence"], "p_done": p_done})
        if p["confidence"] < a.min_confidence:
            history[-1]["tapped"] = False
            return _stop("low confidence on step %d" % step, history, HANDOFF)
        adb(a.serial, "shell", "input", "tap", str(el["x"]), str(el["y"]))
        print("step %d: tap %s (conf %.3f)" % (step, describe(el), p["confidence"]), file=sys.stderr)
        last_xml = xml
        time.sleep(a.wait)
    return _stop("max steps reached", history, HANDOFF)


def _stop(reason, history, code):
    print(json.dumps({"result": "done" if code == 0 else "handoff", "reason": reason, "history": history},
                     ensure_ascii=False, indent=2))
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-s", "--serial", default=os.environ.get("ANDROID_SERIAL"), help="adb device serial")
    ap.add_argument("--model", choices=["ml", "en"], default="ml",
                    help="Laya checkpoint: ml = multilingual (Thai OK, default), en = English")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="list interactive elements")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_screen)

    s = sub.add_parser("tap", help="tap element N (from `screen`) or coordinates X Y")
    s.add_argument("target", type=int)
    s.add_argument("y", type=int, nargs="?")
    s.set_defaults(fn=cmd_tap)

    s = sub.add_parser("type", help="type ASCII text into the focused field")
    s.add_argument("text")
    s.set_defaults(fn=cmd_type)

    s = sub.add_parser("key", help="press a key: back, home, enter, del, tab ...")
    s.add_argument("key")
    s.set_defaults(fn=cmd_key)

    s = sub.add_parser("swipe", help="scroll the screen")
    s.add_argument("direction", choices=["up", "down", "left", "right"])
    s.set_defaults(fn=cmd_swipe)

    s = sub.add_parser("screenshot", help="save a PNG of the screen")
    s.add_argument("out", nargs="?", default="/tmp/laya-android.png")
    s.set_defaults(fn=cmd_screenshot)

    s = sub.add_parser("pick", help="ask Laya which element to tap next (does not tap)")
    s.add_argument("goal")
    s.add_argument("--min-confidence", type=float, default=0.6)
    s.set_defaults(fn=cmd_pick)

    s = sub.add_parser("check", help="ask Laya a yes/no question about the screen")
    s.add_argument("question")
    s.add_argument("--yes", type=float, default=0.8, help="P(true) needed for yes (exit 0)")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("run", help="let Laya tap toward a goal until done or unsure")
    s.add_argument("goal")
    s.add_argument("--done", help="yes/no question that is true when the goal is reached")
    s.add_argument("--max-steps", type=int, default=6)
    s.add_argument("--min-confidence", type=float, default=0.6)
    s.add_argument("--yes", type=float, default=0.8)
    s.add_argument("--wait", type=float, default=1.5, help="seconds to wait after each tap")
    s.set_defaults(fn=cmd_run)

    a = ap.parse_args()
    a.serial = a.serial or pick_serial()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()

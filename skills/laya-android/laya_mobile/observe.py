"""uiautomator dump -> MobileSnapshot, its semantic fingerprint, and re-finding an element."""
from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

from .elements import CLOCK, SYSTEMUI, extract, node_text, norm
from .errors import ObservationError
from .models import MobileSnapshot

ACTIVITY_MARK = "__LAYA_ACTIVITY__"
TEXT_LIMIT = 1500
COMPONENT = re.compile(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)")
RECENTER = 8  # px an element may move between two dumps of the same screen


def split_dump(out):
    """`uiautomator dump /dev/tty; echo MARK; dumpsys window | grep ...` output -> (xml, focus lines).
    uiautomator appends "UI hierchary dumped to: /dev/tty" after the XML."""
    end = out.rfind("</hierarchy>")
    if end < 0:
        return None, ""
    tail = out[end:]
    focus = tail.split(ACTIVITY_MARK, 1)[1] if ACTIVITY_MARK in tail else ""
    return out[out.find("<"):end + len("</hierarchy>")], focus


def parse_activity(focus):
    """(package, activity) from `dumpsys window` focus lines; the focused app's activity is
    preferred over the focused window, which may be a popup or the keyboard."""
    lines = focus.splitlines()
    for key in ("mFocusedApp", "mCurrentFocus"):
        for line in lines:
            if key in line:
                m = COMPONENT.search(line)
                if m:
                    pkg, act = m.groups()
                    return pkg, pkg + act if act.startswith(".") else act
    return None, None


def visible_text(nodes, limit=TEXT_LIMIT):
    seen, parts = set(), []
    for node in nodes:
        t = node_text(node)
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    return " | ".join(parts)[:limit]


def fingerprint(package, activity, texts, elements):
    """Semantic screen id: what the screen shows, not how the XML happens to be laid out.
    Order-free (sorted), position-free (no bounds), clock-free (12:34 -> #:##). It changes when
    text, the set of elements or an element's checked/selected/focused/enabled state changes."""
    blob = {
        "package": package or "",
        "activity": activity or "",
        "text": sorted({CLOCK.sub("#:##", norm(t)) for t in texts if t.strip()}),
        "elements": sorted([e.stable_key, e.checked, e.selected, e.focused, e.enabled] for e in elements),
    }
    return hashlib.sha1(json.dumps(blob, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def snapshot_from_xml(xml, activity=None, timestamp=None):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ObservationError("unparseable uiautomator dump: %s" % e) from None
    nodes = list(root.iter("node"))
    pkgs = [n.get("package") for n in nodes if n.get("package")]
    pkg = next((p for p in pkgs if p != SYSTEMUI), pkgs[0] if pkgs else None)
    els = extract(root)
    # Text and fingerprint leave out the status bar: its clock and icons change on their own.
    # The notification shade is all SystemUI, so then it stays.
    app_nodes = [n for n in nodes if n.get("package") != SYSTEMUI] or nodes
    app_els = [e for e in els if e.package != SYSTEMUI] or els
    fp = fingerprint(pkg, activity, [node_text(n) for n in app_nodes], app_els)
    return MobileSnapshot(package=pkg, activity=activity, visible_text=visible_text(app_nodes), elements=els,
                          fingerprint=fp, timestamp=round(time.time() if timestamp is None else timestamp, 3))


def resolve(el, old, new):
    """Element `el` of snapshot `old` as it is in the fresh snapshot `new`; None if gone or ambiguous."""
    same = [e for e in new.elements if e.stable_key == el.stable_key]
    if len(same) == 1 and sum(e.stable_key == el.stable_key for e in old.elements) == 1:
        return same[0]
    if new.fingerprint == old.fingerprint and same:
        # Same screen, repeated key: the one at the same place is the same element.
        x, y = el.center
        near = [e for e in same if abs(e.center[0] - x) <= RECENTER and abs(e.center[1] - y) <= RECENTER]
        if len(near) == 1:
            return near[0]
    # A key repeated on a changed screen is ambiguous: after a list shift, the button in the
    # same place can belong to another item.
    return None

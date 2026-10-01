"""Deterministic goal checks, read straight off the screen (Laya's "goal reached?" is a guess)."""
from __future__ import annotations

from .elements import norm


def conditions(text=None, package=None, element=None):
    return {"text": list(text or []), "package": package, "element": list(element or [])}


def unmet(snapshot, cond):
    """The conditions `snapshot` does not meet; [] means the goal is reached. All must hold."""
    labels = [norm(e.label) for e in snapshot.elements]
    screen = norm(snapshot.visible_text)
    miss = []
    if cond["package"] and snapshot.package != cond["package"]:
        miss.append("app is %s, not %s" % (snapshot.package, cond["package"]))
    for t in cond["text"]:
        if norm(t) not in screen and not any(norm(t) in l for l in labels):
            miss.append('no text "%s"' % t)
    for t in cond["element"]:
        if not any(norm(t) in l for l in labels):
            miss.append('no element "%s"' % t)
    return miss

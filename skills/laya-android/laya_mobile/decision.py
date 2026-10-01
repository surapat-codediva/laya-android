"""Laya decisions. The predictor is anything with Laya's `predict(state, questions)`; tests pass a fake."""
from __future__ import annotations

import os
import time

from .candidates import candidates_for_operation
from .elements import describe

HISTORY = 5  # recent actions shown to Laya
OPERATIONS = {
    "CLICK": "tap one of the on-screen elements",
    "SCROLL_DOWN": "scroll to reveal content further down",
    "SCROLL_UP": "scroll to reveal content further up",
    "BACK": "go back to the previous screen",
    "DONE": "the goal is already reached on the current screen",
}

_models = {}


def load_laya(model):
    """The local Laya checkpoint: ml = multilingual (Thai OK), en = English. Loaded once per process."""
    if model not in _models:
        os.environ.setdefault("USE_TF", "0")  # avoids an abseil deadlock if TF is installed
        import laya
        _models[model] = laya.load("convaiinnovations/laya", subfolder="multilingual" if model == "ml" else None)
    return _models[model]


class LazyLaya:
    """Loads the checkpoint on the first predict(), so a run that is already done never loads it."""

    def __init__(self, model):
        self.model = model

    def predict(self, state, questions):
        return load_laya(self.model).predict(state, questions)


def laya_state(snapshot, history):
    app = snapshot.package or "?"
    if snapshot.activity:
        app += " / " + snapshot.activity.rsplit(".", 1)[-1]
    lines = ["app: %s" % app]
    if history:
        lines.append("recent actions: " + "; ".join(history[-HISTORY:]))
    lines.append("screen: " + snapshot.visible_text)
    return "\n".join(lines)


def operations_for(snapshot, clickable):
    """Operations that can apply here: no CLICK without click targets, no SCROLL without a
    scrollable container."""
    scrollable = any(e.scrollable for e in snapshot.elements)
    return {k: v for k, v in OPERATIONS.items()
            if (k != "CLICK" or clickable) and (not k.startswith("SCROLL") or scrollable)}


def decide(predictor, goal, snapshot, history, done_q=None, limit=20):
    """Next operation, tap target (among the pruned CLICK candidates) and, with done_q,
    P(goal reached) -- one forward pass."""
    cands = candidates_for_operation(snapshot, "CLICK", limit=limit, goal=goal)
    q = {"operation": {
        "type": "choice",
        "instructions": "Goal: %s. What should be done next on this screen to make progress toward the goal?" % goal,
        "criteria": operations_for(snapshot, cands),
    }}
    if cands:
        q["target"] = {
            "type": "choice",
            "instructions": "Goal: %s. If an element should be tapped next, which one?" % goal,
            "criteria": {str(e.id): describe(e) for e in cands},
        }
    if done_q:
        q["done"] = {"type": "noul", "instructions": done_q}
    t0 = time.monotonic()
    ans = predictor.predict(laya_state(snapshot, history), q)["answers"]
    d = {"fingerprint": snapshot.fingerprint, "operation": ans["operation"]["choice"],
         "op_confidence": round(float(ans["operation"]["confidence"]), 3),
         "candidates": [e.id for e in cands]}
    if cands:
        d["target"] = int(ans["target"]["choice"])
        d["target_stable_key"] = snapshot.element(d["target"]).stable_key
        d["target_confidence"] = round(float(ans["target"]["confidence"]), 3)
    if done_q:
        d["p_done"] = round(float(ans["done"]["noul"]), 3)
    d["ms"] = round((time.monotonic() - t0) * 1000)
    return d


def confident(d, min_confidence):
    return d["op_confidence"] >= min_confidence and (
        d["operation"] != "CLICK" or d.get("target_confidence", 0) >= min_confidence)


def summary(d, snapshot):
    """A decision as the CLI prints it: the target as text instead of a bare id."""
    out = {k: v for k, v in d.items() if k not in ("target", "candidates")}
    out["candidates"] = len(d.get("candidates", []))
    if "target" in d:
        el = snapshot.element(d["target"])
        out["target"] = {"id": el.id, "element": describe(el)}
    return out


def ask_check(predictor, question, snapshot):
    q = {"q": {"type": "noul", "instructions": question}}
    a = predictor.predict(laya_state(snapshot, []), q)["answers"]["q"]
    return round(float(a["noul"]), 3)

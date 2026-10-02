"""Shared test helpers: fixture loading, a fake Laya and a fake device."""
import os
import time

from laya_mobile.observe import snapshot_from_xml

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def xml(name):
    with open(os.path.join(FIXTURES, name + ".xml"), encoding="utf-8") as f:
        return f.read()


def snap(name, activity=None):
    return snapshot_from_xml(xml(name), activity=activity, timestamp=0)


def by_label(snapshot, label, role=None, context=None):
    found = [e for e in snapshot.elements if e.label == label and (role is None or e.role == role)
             and (context is None or context in e.context)]
    assert len(found) == 1, "%r: %s" % (label, [(e.label, e.context) for e in found])
    return found[0]


class FakePredictor:
    """Laya stand-in. `answers` is a list (one per predict call) of
    {"operation": (choice, conf), "target": (choice, conf), "done": p}; the target can be a
    callable(criteria) -> choice, to pick by description."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        a = self.answers.pop(0)
        out = {}
        if "operation" in questions:
            op, conf = a.get("operation", ("BACK", 0.1))
            out["operation"] = {"choice": op, "confidence": conf}
        if "target" in questions:
            t, conf = a.get("target", (next(iter(questions["target"]["criteria"])), 0.1))
            if callable(t):
                t = t(questions["target"]["criteria"])
            out["target"] = {"choice": str(t), "confidence": conf}
        if "done" in questions:
            out["done"] = {"noul": a.get("done", 0.0)}
        if "q" in questions:
            out["q"] = {"noul": a.get("q", 0.5)}
        return {"answers": out}


def pick(text):
    """Target chooser: the option whose description contains `text`."""
    def choose(criteria):
        return next(k for k, v in criteria.items() if text in v)
    return choose


class FakeClock:
    """A monotonic clock that only moves when sleep() is called: settle loops run instantly."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def sig_of(snapshot):
    """The fake screen signature: same screen content -> same signature, like a pixel hash."""
    return "sig:" + snapshot.fingerprint


class FakeDevice:
    """Serves saved screens. `screens` is the sequence observe() returns (the last repeats);
    `transitions` maps a stable label (or "swipe up", "key BACK") to the screen list index to
    switch to. Counts full observations and cheap probes."""

    def __init__(self, screens, transitions=None):
        self.screens = [snap(s) if isinstance(s, str) else s for s in screens]
        self.i = 0
        self.transitions = transitions or {}
        self.actions = []
        self.observations = 0
        self.probes = 0

    def current(self):
        return self.screens[min(self.i, len(self.screens) - 1)]

    def observe(self, signature=None):
        self.observations += 1
        s = self.current()
        s.signature = sig_of(s)
        s.timestamp = time.time()  # a fresh dump
        return s

    def probe(self):
        self.probes += 1
        return sig_of(self.current())

    def _advance(self, key):
        if key in self.transitions:
            self.i = self.transitions[key]

    def tap(self, x, y):
        self.actions.append(("tap", x, y))
        cur = self.current()
        hit = [e for e in cur.elements if e.bounds[0] <= x < e.bounds[2] and e.bounds[1] <= y < e.bounds[3]]
        for e in sorted(hit, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1])):
            if e.label in self.transitions:
                return self._advance(e.label)

    def swipe(self, direction):
        self.actions.append(("swipe", direction))
        self._advance("swipe " + direction)

    def key(self, name):
        self.actions.append(("key", name))
        self._advance("key " + name)

    def type_text(self, text):
        self.actions.append(("type", text))

    def screenshot(self, out):
        return out

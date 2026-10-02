"""Latency telemetry without dependencies: per-step phase timings, call counts, run summaries.

A step's phases are observe / decision / policy / execute / settle; `total` is the step's wall
time, so it also holds what falls between phases. Calls are counted with their duration, both
per step and for the whole run: adb (every adb process), laya (every Laya request), and among
the adb calls, dump (full uiautomator observation, ~2 s) and probe (screen signature, ~0.15 s).
"""
from __future__ import annotations

import time
from contextlib import contextmanager

PHASES = ("observe", "decision", "policy", "execute", "settle")


def percentile(values, p):
    """Linear-interpolated percentile (p in 0..100) of a non-empty sequence."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values):
    """{"n", "p50", "p95", "avg", "min", "max"} in the values' unit, rounded to 0.1."""
    if not values:
        return {"n": 0}
    r = lambda x: round(x, 1)
    return {"n": len(values), "p50": r(percentile(values, 50)), "p95": r(percentile(values, 95)),
            "avg": r(sum(values) / len(values)), "min": r(min(values)), "max": r(max(values))}


def run_summary(step_totals):
    """Run-level numbers from each step's total_ms."""
    if not step_totals:
        return {"steps": 0, "total_ms": 0}
    s = summarize(step_totals)
    return {"steps": s["n"], "total_ms": round(sum(step_totals), 1), "avg_step_ms": s["avg"],
            "p50_step_ms": s["p50"], "p95_step_ms": s["p95"]}


class Metrics:
    """Collects timings for the current step and counts for the whole run.

    with m.phase("observe"): ...   time a phase of the current step
    m.call("adb", ms)              one external call and its duration
    m.start_step() / m.end_step()  end_step() returns the step's record and starts nothing
    """

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.calls = {}  # kind -> [ms, ...] for the run
        self.steps = []  # finished step records
        self._step = None

    # ---- steps ----
    def start_step(self):
        self._step = {"t0": self.clock(), "phases": {}, "calls": {}}

    def peek(self):
        """The current step's record so far (None outside a step)."""
        st = self._step
        if st is None:
            return None
        timing = {k: round(v, 1) for k, v in st["phases"].items()}
        timing["total"] = round((self.clock() - st["t0"]) * 1000, 1)
        c = st["calls"]
        return {"timing_ms": timing, "adb_calls": c.get("adb", 0), "laya_calls": c.get("laya", 0),
                "dumps": c.get("dump", 0), "probes": c.get("probe", 0)}

    def end_step(self):
        rec, self._step = self.peek(), None
        if rec is not None:
            self.steps.append(rec)
        return rec

    def discard_step(self):
        self._step = None

    @contextmanager
    def phase(self, name):
        t0 = self.clock()
        try:
            yield
        finally:
            if self._step is not None:
                ph = self._step["phases"]
                ph[name] = ph.get(name, 0.0) + (self.clock() - t0) * 1000

    def add(self, name, ms):
        """Add time measured elsewhere to a phase of the current step."""
        if self._step is not None:
            ph = self._step["phases"]
            ph[name] = ph.get(name, 0.0) + ms

    # ---- calls ----
    def call(self, kind, ms):
        self.calls.setdefault(kind, []).append(ms)
        if self._step is not None:
            c = self._step["calls"]
            c[kind] = c.get(kind, 0) + 1

    def count(self, kind):
        return len(self.calls.get(kind, ()))

    # ---- reports ----
    def summary(self):
        totals = [s["timing_ms"]["total"] for s in self.steps]
        out = run_summary(totals)
        for kind in ("adb", "laya"):
            out[kind + "_calls"] = self.count(kind)
        if self.steps:
            n = len(self.steps)
            out["adb_calls_per_step"] = round(sum(s["adb_calls"] for s in self.steps) / n, 2)
            out["laya_calls_per_step"] = round(sum(s["laya_calls"] for s in self.steps) / n, 2)
            out["dumps_per_step"] = round(sum(s["dumps"] for s in self.steps) / n, 2)
            out["probes_per_step"] = round(sum(s["probes"] for s in self.steps) / n, 2)
            for p in PHASES:
                vals = [s["timing_ms"].get(p, 0.0) for s in self.steps]
                out["avg_%s_ms" % p] = round(sum(vals) / len(vals), 1)
        return out


def format_step(rec):
    """The --verbose stderr block for one step or command."""
    t = rec["timing_ms"]
    lines = ["%s: %.0fms" % (p, t[p]) for p in PHASES if p in t]
    lines.append("total: %.0fms" % t["total"])
    lines.append("adb calls: %d (dumps %d, probes %d)  laya calls: %d"
                 % (rec["adb_calls"], rec.get("dumps", 0), rec.get("probes", 0), rec["laya_calls"]))
    return "\n".join(lines)


class Metered:
    """Wraps a predictor so each predict() counts as one Laya call with its latency."""

    def __init__(self, predictor, metrics):
        self.predictor, self.metrics = predictor, metrics

    def predict(self, state, questions):
        t0 = time.perf_counter()
        try:
            return self.predictor.predict(state, questions)
        finally:
            self.metrics.call("laya", (time.perf_counter() - t0) * 1000)

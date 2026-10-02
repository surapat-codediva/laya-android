"""Before/after harness: drive run_goal on the device's Settings app with a scripted oracle.

usage: compare_run.py <skill dir> <out.json> [--runs N] [--fake-laya | --daemon]
  <skill dir>  the skills/laya-android folder of the version to measure (e.g. a git worktree of
               the Phase 2 commit for "before"); run with that version's or this venv's python
  --daemon     Laya through the persistent daemon (Phase 3); default: in-process (Phase 2)
  --fake-laya  no model at all: device-side timing only
The oracle calls the real Laya model (so decision latency is real) and then replaces its
answers with a fixed, non-destructive navigation script: open a Settings page, go back, ...
Every adb process is counted by wrapping subprocess.Popen (subprocess.run goes through it).
"""
import json
import os
import statistics
import subprocess
import sys
import time

skill, out = sys.argv[1], sys.argv[2]
fake = "--fake-laya" in sys.argv
runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 3
sys.path.insert(0, skill)
os.environ.setdefault("USE_TF", "0")

from laya_mobile import adb as adb_mod  # noqa: E402
from laya_mobile import runner  # noqa: E402
from laya_mobile.session import Session  # noqa: E402

SERIAL = os.environ.get("ANDROID_SERIAL", "emulator-5554")
SCRIPT = [("CLICK", "Network & internet"), ("BACK", None), ("CLICK", "Connected devices"), ("BACK", None),
          ("CLICK", "Apps"), ("BACK", None)]

adb_calls = []
_real_run, _real_popen = subprocess.run, subprocess.Popen


class CountingPopen(_real_popen):
    def __init__(self, cmd, *a, **kw):
        if cmd and cmd[0] == "adb":
            adb_calls.append(" ".join(cmd[3:5]))
        super().__init__(cmd, *a, **kw)


subprocess.Popen = CountingPopen


class Oracle:
    def __init__(self, model):
        self.model, self.i, self.laya_ms = model, 0, []

    def predict(self, state, questions):
        t0 = time.perf_counter()
        if self.model is not None:
            self.model.predict(state, questions)
        self.laya_ms.append((time.perf_counter() - t0) * 1000)
        op, label = SCRIPT[min(self.i, len(SCRIPT) - 1)]
        self.i += 1
        ans = {"operation": {"choice": op, "confidence": 0.99}}
        if "target" in questions:
            crit = questions["target"]["criteria"]
            tgt = next((k for k, v in crit.items() if label and '"%s' % label in v), next(iter(crit)))
            ans["target"] = {"choice": tgt, "confidence": 0.99}
        if "done" in questions:
            ans["done"] = {"noul": 0.0}
        return {"answers": ans}


def home():
    _real_run(["adb", "-s", SERIAL, "shell", "am", "force-stop", "com.android.settings"])
    _real_run(["adb", "-s", SERIAL, "shell", "am", "start", "-W", "-a", "android.settings.SETTINGS"],
              capture_output=True)
    time.sleep(2.5)


phases = {}


def timed(name, fn):
    def wrap(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            phases[name] = phases.get(name, 0) + (time.perf_counter() - t0) * 1000
    return wrap


adb_mod.Device.observe = timed("observe", adb_mod.Device.observe)
runner.execute = timed("execute", runner.execute)
import inspect  # noqa: E402
EXTRA = {"sleep": timed("sleep", time.sleep)} if "sleep" in inspect.signature(runner.run_goal).parameters else {}

model = None
if "--daemon" in sys.argv:
    from laya_mobile.decision import DaemonDecisionClient
    model = DaemonDecisionClient("ml")
    model.predict("app: x\nscreen: y", {"q": {"type": "noul", "instructions": "ok?"}})  # start + load + warm
elif not fake:
    from laya_mobile.decision import load_laya
    model = load_laya("ml")

results = []
for r in range(runs):
    home()
    del adb_calls[:]
    oracle = Oracle(model)
    phases.clear()
    kw = {}
    if "metrics" in inspect.signature(runner.run_goal).parameters:
        from laya_mobile.metrics import Metrics
        kw["metrics"] = Metrics()
        dev = adb_mod.Device(SERIAL, metrics=kw["metrics"])
    else:
        dev = adb_mod.Device(SERIAL)
    s = Session(SERIAL).start("bench %d" % r)
    t0 = time.perf_counter()
    res = runner.run_goal(dev, oracle, s, "navigate settings", None, None, max_steps=len(SCRIPT), min_confidence=0.6,
                          yes=0.8, **EXTRA, **kw)
    total = (time.perf_counter() - t0) * 1000
    d = res.to_dict()
    results.append({"total_ms": round(total), "steps": len(d["steps"]), "reason": d["reason"],
                    "adb_calls": len(adb_calls), "adb_detail": list(adb_calls),
                    "laya_ms": [round(x, 1) for x in oracle.laya_ms],
                    "phases_ms": {k: round(v) for k, v in phases.items()}, "run": d.get("timing"),
                    "step_detail": [{k: st.get(k) for k in ("operation", "path", "safe_reason", "settle", "timing_ms",
                                                           "adb_calls")} for st in d["steps"]]})
    print("run %d: %d ms, %d steps, %d adb calls, reason=%s" % (r, total, len(d["steps"]), len(adb_calls),
                                                                d["reason"]), file=sys.stderr)

per_step = [x["total_ms"] / max(x["steps"], 1) for x in results]
summary = {"runs": results, "avg_step_ms": round(statistics.mean(per_step), 1),
           "adb_calls_per_step": round(statistics.mean(x["adb_calls"] / max(x["steps"], 1) for x in results), 2)}
with open(out, "w") as f:
    json.dump(summary, f, indent=1)
print(json.dumps({k: v for k, v in summary.items() if k != "runs"}))

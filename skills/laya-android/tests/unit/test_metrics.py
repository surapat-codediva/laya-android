from laya_mobile.metrics import Metered, Metrics, format_step, percentile, run_summary, summarize


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10], 95) == 10
    assert percentile([], 50) is None
    assert percentile(range(1, 101), 95) == 95.05


def test_run_summary():
    s = run_summary([100, 200, 300, 400])
    assert s == {"steps": 4, "total_ms": 1000, "avg_step_ms": 250.0, "p50_step_ms": 250.0, "p95_step_ms": 385.0}
    assert run_summary([]) == {"steps": 0, "total_ms": 0}
    assert summarize([])["n"] == 0


def test_step_phases_calls_and_total():
    c = Clock()
    m = Metrics(clock=c)
    m.start_step()
    with m.phase("observe"):
        c.t += 0.2
    with m.phase("decision"):
        c.t += 0.03
        m.call("laya", 30)
    m.call("adb", 12)
    m.call("adb", 8)
    with m.phase("observe"):  # a second observation adds up
        c.t += 0.1
    c.t += 0.01  # time outside any phase still counts in total
    rec = m.end_step()
    assert rec["timing_ms"] == {"observe": 300.0, "decision": 30.0, "total": 340.0}
    assert rec["adb_calls"] == 2 and rec["laya_calls"] == 1
    m.call("adb", 5)  # outside a step: counted for the run only
    s = m.summary()
    assert s["steps"] == 1 and s["adb_calls"] == 3 and s["laya_calls"] == 1
    assert s["adb_calls_per_step"] == 2 and s["avg_observe_ms"] == 300.0
    out = format_step(rec)
    assert "observe: 300ms" in out and "total: 340ms" in out
    assert "adb calls: 2 (dumps 0, probes 0)  laya calls: 1" in out


def test_peek_and_discard():
    m = Metrics(clock=Clock())
    assert m.peek() is None and m.end_step() is None
    m.start_step()
    assert m.peek()["timing_ms"] == {"total": 0.0}
    m.discard_step()
    assert m.end_step() is None and m.steps == []


def test_metered_counts_each_predict():
    class P:
        def predict(self, state, questions):
            return {"answers": {}}
    m = Metrics()
    m.start_step()
    p = Metered(P(), m)
    p.predict("s", {})
    p.predict("s", {})
    assert m.end_step()["laya_calls"] == 2

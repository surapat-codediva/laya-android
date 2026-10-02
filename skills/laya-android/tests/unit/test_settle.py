"""Adaptive settle on scripted signature sequences: no device, no real time."""
import pytest

from helpers import FakeClock
from laya_mobile.config import SettleConfig, action_settle
from laya_mobile.errors import UsageError
from laya_mobile.settle import DYNAMIC, STABLE, UNCHANGED, settle

CFG = SettleConfig(poll_ms=100, stable_ms=150, timeout_ms=2500, min_samples=2)


def run(seq, op="CLICK", before="A", cfg=CFG, probe_ms=120):
    """Probe returns seq[i] on the i-th call (the last value repeats); each probe takes probe_ms
    (~120 ms is what one costs over adb on an idle emulator)."""
    clock = FakeClock()
    calls = []

    def probe():
        clock.sleep(probe_ms / 1000.0)
        calls.append(seq[min(len(calls), len(seq) - 1)])
        return calls[-1]
    res = settle(probe, before, op, cfg, clock, clock.sleep)
    return res, calls, clock.t


def test_settles_on_the_new_screen():
    res, calls, _ = run(list("AABBB"))
    assert res.status == STABLE and res.signature == "B" and res.changed
    assert calls == list("AABB")  # stops as soon as B held for two probes
    assert res.settled


def test_waits_out_intermediate_screens():
    # tap -> spinner -> intermediate -> final: not the first change, the stable one.
    res, calls, _ = run(list("AABCCC"))
    assert res.status == STABLE and res.signature == "C"
    assert calls == list("AABCC")


def test_never_stable_times_out_as_dynamic():
    res, calls, t = run(list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"))
    assert res.status == DYNAMIC and res.changed and not res.settled
    assert t * 1000 >= CFG.timeout_ms and len(calls) < 30  # bounded


def test_no_change_for_an_action_that_should_change_is_unchanged_after_grace():
    res, calls, t = run(list("AAAA"))
    assert res.status == UNCHANGED and not res.changed and res.settled
    assert t * 1000 >= action_settle("CLICK").change_grace_ms  # it did wait for a slow screen
    assert t * 1000 < CFG.timeout_ms


def test_late_change_inside_the_grace_period_is_caught():
    res, _, _ = run(["A"] * 4 + ["B"] * 3)  # B appears ~0.9 s after the tap, inside the 1.2 s grace
    assert res.status == STABLE and res.signature == "B"


def test_key_without_expected_change_settles_immediately():
    res, calls, _ = run(list("AAAA"), op="KEY_ENTER")
    assert res.status == UNCHANGED and calls == ["A", "A"]


def test_unknown_before_only_needs_stability():
    res, calls, _ = run(list("XXX"), before=None)
    assert res.status == STABLE and calls == ["X", "X"]


def test_unreadable_probe_counts_as_change_and_waits_for_stability():
    res, calls, _ = run([None, None, None])
    assert res.status == STABLE and res.signature is None and calls == [None, None]


def test_slow_probes_are_reported():
    res, _, _ = run(list("ABB"), probe_ms=400)
    assert res.status == STABLE and res.probe_ms == pytest.approx(400)
    assert res.to_dict()["probe_ms"] == 400.0


def test_action_types():
    assert action_settle("SCROLL_DOWN") == action_settle("SWIPE_LEFT") == action_settle("SCROLL")
    assert action_settle("TYPE_TEXT") == action_settle("TYPE")
    assert action_settle("KEY_ENTER").expect_change is False
    assert action_settle("whatever").expect_change is False
    assert action_settle("CLICK").expect_change and action_settle("BACK").expect_change


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("LAYA_ANDROID_SETTLE_POLL_MS", "120")
    monkeypatch.setenv("LAYA_ANDROID_SETTLE_STABLE_MS", "300")
    monkeypatch.setenv("LAYA_ANDROID_SETTLE_TIMEOUT_MS", "3000")
    c = SettleConfig.from_env()
    assert (c.poll_ms, c.stable_ms, c.timeout_ms) == (120, 300, 3000)
    monkeypatch.setenv("LAYA_ANDROID_SETTLE_TIMEOUT_MS", "soon")
    with pytest.raises(UsageError):
        SettleConfig.from_env()

from helpers import FakeDevice, FakePredictor, pick
from laya_mobile import trajectory as tj
from laya_mobile.checks import conditions
from laya_mobile.runner import DONE, HANDOFF, run_goal
from laya_mobile.session import Session


def session(goal, record=True):
    return Session("emulator-5554").start(goal, record=record)


def go(device, answers, goal="open Bluetooth", **kw):
    s = kw.pop("session", None) or session(goal)
    r = run_goal(device, FakePredictor(answers), s, goal, sleep=lambda _: None, **kw)
    return r, s


def test_reaches_goal_verified_on_screen_and_records_each_step():
    dev = FakeDevice(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    r, s = go(dev, [{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)}],
              cond=conditions(text=["On"], element=["Bluetooth | On"]))
    assert (r.code, r.result, r.reason) == (DONE, "done", "goal verified on screen")
    assert dev.actions == [("tap", 540, 720)]
    lines = tj.read(r.trajectory)
    assert [l["event"] for l in lines] == ["start", "step", "end"]
    step = lines[1]
    assert step["actor"] == "laya" and step["executed"]["operation"] == "CLICK"
    assert step["outcome"] == {"screen_changed": True}
    assert step["policy"]["action"] == "allow"
    assert step["after"]["fingerprint"] == dev.screens[1].fingerprint
    assert s.history == ['CLICK item "Bluetooth | Off"']


def test_already_done_never_asks_laya():
    p = FakePredictor([])
    r = run_goal(FakeDevice(["settings"]), p, session("x"), "x", cond=conditions(text=["Wi-Fi"]),
                 sleep=lambda _: None)
    assert r.code == DONE and p.calls == []


def test_low_confidence_hands_off():
    dev = FakeDevice(["settings"])
    r, _ = go(dev, [{"operation": ("CLICK", 0.3), "target": (pick("Bluetooth"), 0.9)}],
              cond=conditions(text=["nope"]))
    assert r.code == HANDOFF and r.reason == "low confidence on step 1" and dev.actions == []


def test_policy_gate_stops_autonomous_payment():
    dev = FakeDevice(["payment"])
    r, _ = go(dev, [{"operation": ("CLICK", 0.95), "target": (pick("Confirm payment"), 0.95)}], goal="pay",
              cond=conditions(text=["Thank you"]))
    assert r.code == HANDOFF and r.reason == "sensitive_action"
    assert r.policy == {"action": "handoff", "reason": "sensitive_action", "matched": "payment",
                        "keyword": "payment", "target": "Confirm payment"}
    assert dev.actions == []  # nothing was tapped
    out = r.to_dict()
    assert out["result"] == "handoff" and out["policy"]["matched"] == "payment"
    step = [l for l in tj.read(r.trajectory) if l["event"] == "step"][0]
    assert step["executed"] is None and step["policy"]["action"] == "handoff"


def test_screen_not_changing_hands_off():
    dev = FakeDevice(["settings"])
    r, _ = go(dev, [{"operation": ("SCROLL_DOWN", 0.9)}], cond=conditions(text=["nope"]))
    assert r.code == HANDOFF and r.reason.startswith("screen did not change after SCROLL_DOWN")
    assert dev.actions == [("swipe", "up")]


def test_laya_done_without_conditions_uses_done_check():
    r, _ = go(FakeDevice(["settings"]), [{"operation": ("DONE", 0.9), "done": 0.95}])
    assert r.code == DONE and r.reason == "goal reached (p=0.950)"
    r, _ = go(FakeDevice(["settings"]), [{"operation": ("DONE", 0.9), "done": 0.5}])
    assert r.code == HANDOFF and "done check is unsure" in r.reason


def test_stale_target_is_not_tapped_and_laya_redecides():
    class Shifting(FakeDevice):
        def observe(self):
            # The second observation (the pre-tap re-check) is a different screen.
            self.n = getattr(self, "n", 0) + 1
            return self.screens[0] if self.n == 1 else self.screens[1]

    dev = Shifting(["settings", "basic"])
    r, _ = go(dev, [{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)},
                    {"operation": ("BACK", 0.2)}], cond=conditions(text=["nope"]))
    assert dev.actions == [] and r.steps[0]["stale"] is True and r.reason == "low confidence on step 2"


def test_recording_is_opt_in(tmp_path):
    s = Session("emulator-5554").start("x")
    r, _ = go(FakeDevice(["settings"]), [{"operation": ("BACK", 0.1)}], session=s, cond=conditions(text=["no"]))
    assert r.trajectory is None and not (tmp_path / "data").exists()


def test_teacher_correction_after_handoff_is_recorded():
    """Laya hands off; the host taps something else on the same screen -> model vs teacher label."""
    from laya_android import record_manual  # the CLI's recording of host actions
    dev = FakeDevice(["duplicate_buy"])
    r, s = go(dev, [{"operation": ("CLICK", 0.4), "target": (pick("Case A"), 0.3)}], goal="buy case B",
              cond=conditions(text=["Thank you"]))
    assert r.code == HANDOFF
    before = s.current()
    b = before.elements[2]

    record_manual(s, "CLICK", b, before, None, None)
    step = tj.read(s.trajectory)[-1]
    assert step["actor"] == "agent"
    assert step["model_decision"]["target_id"] == 1 and step["model_decision"]["confidence"] == 0.4
    assert step["teacher_action"]["target_stable_key"] == b.stable_key

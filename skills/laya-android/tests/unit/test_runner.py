from helpers import FakeClock, FakeDevice, FakePredictor, pick, sig_of, snap
from laya_mobile import trajectory as tj
from laya_mobile.checks import conditions
from laya_mobile.runner import DONE, HANDOFF, run_goal
from laya_mobile.session import Session


def session(goal, record=True):
    return Session("emulator-5554").start(goal, record=record)


def go(device, answers, goal="open Bluetooth", **kw):
    s = kw.pop("session", None) or session(goal)
    clock = FakeClock()
    r = run_goal(device, FakePredictor(answers), s, goal, sleep=clock.sleep, clock=clock, **kw)
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
    r = run_goal(FakeDevice(["settings"]), p, session("x"), "x", cond=conditions(text=["Wi-Fi"]))
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
        def observe(self, signature=None):
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


# ---------- Phase 3: speed without dropping safety ----------

def test_fast_path_skips_the_pre_tap_dump_and_reuses_the_settled_snapshot():
    dev = FakeDevice(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    p = FakePredictor([{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)},
                       {"operation": ("BACK", 0.2)}])
    clock = FakeClock()
    r = run_goal(dev, p, session("bt"), "bt", cond=conditions(text=["nope"]), sleep=clock.sleep, clock=clock)
    first = r.steps[0]
    assert first["path"] == "fast" and "safe_reason" not in first
    assert first["settle"]["status"] == "stable" and first["settle"]["changed"]
    # initial observation + the one after settling: no re-dump before the tap, none during settle
    assert dev.observations == 2 and dev.probes >= 3
    # the second decision ran on the settled observation (no second dump of the same screen)
    assert r.steps[1]["fingerprint"] == dev.screens[1].fingerprint


def test_one_laya_request_per_step():
    dev = FakeDevice(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    p = FakePredictor([{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)},
                       {"operation": ("SCROLL_DOWN", 0.2)}])
    clock = FakeClock()
    r = run_goal(dev, p, session("bt"), "bt", cond=conditions(text=["nope"]), sleep=clock.sleep, clock=clock)
    assert len(p.calls) == len(r.steps) == 2
    assert set(p.calls[0][1]) == {"operation", "target", "done"}  # all questions in one forward pass
    assert all(s["laya_calls"] == 1 for s in r.steps)


def test_settle_polls_are_light_observations_only():
    """The settle loop probes the screen signature; it never re-parses or re-extracts the screen."""
    dev = FakeDevice(["basic", "settings"], {"key BACK": 1})
    r, _ = go(dev, [{"operation": ("BACK", 0.9)}, {"operation": ("BACK", 0.2)}], cond=conditions(text=["nope"]))
    assert r.steps[0]["settle"]["polls"] >= 2
    assert dev.observations == 2  # one full observation per step, whatever the number of polls


def test_sensitive_context_takes_the_safe_path():
    # "Cancel" is allowed, but on a "Delete 3 photos?" dialog it gets a fresh look first.
    dev = FakeDevice(["dialog_delete", "basic"], {"Cancel": 1})
    r, _ = go(dev, [{"operation": ("CLICK", 0.9), "target": (pick('"Cancel"'), 0.9)},
                    {"operation": ("BACK", 0.2)}], goal="close", cond=conditions(text=["nope"]))
    assert r.steps[0]["path"] == "safe" and r.steps[0]["safe_reason"] == "sensitive_context"
    assert dev.observations == 3 and dev.actions[0][0] == "tap"


def test_screen_changed_since_the_decision_takes_the_safe_path():
    class Drifting(FakeDevice):
        def probe(self):
            self.probes += 1
            return "moved" if self.probes == 1 else super().probe()

    dev = Drifting(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    r, _ = go(dev, [{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)},
                    {"operation": ("BACK", 0.2)}], cond=conditions(text=["nope"]))
    assert r.steps[0]["path"] == "safe" and r.steps[0]["safe_reason"] == "screen_changed"
    assert dev.observations == 3 and dev.actions == [("tap", 540, 720)]


def test_ambiguous_identity_takes_the_safe_path():
    from laya_mobile.freshness import fast_path_blocker
    from laya_mobile.policy import PolicyGate
    s = snap("settings")
    s.signature, s.timestamp = "sig", __import__("time").time()
    el = s.elements[4]
    assert fast_path_blocker(s, el, PolicyGate().evaluate("CLICK", el, s)) is None
    s.elements[3].stable_key = el.stable_key
    assert fast_path_blocker(s, el, PolicyGate().evaluate("CLICK", el, s)) == "ambiguous_identity"


def test_fast_path_blockers():
    from laya_mobile.freshness import fast_path_blocker
    from laya_mobile.policy import PolicyGate
    gate = PolicyGate()
    s = snap("settings")
    el = s.elements[4]
    assert fast_path_blocker(s, el, gate.evaluate("CLICK", el, s)) == "no_signature"
    s.signature = "sig"
    s.timestamp = 1.0  # decades old
    assert fast_path_blocker(s, el, gate.evaluate("CLICK", el, s)) == "old_snapshot"
    s.timestamp = __import__("time").time()
    s.secure = True
    assert fast_path_blocker(s, el, gate.evaluate("CLICK", el, s)) == "secure_window"
    pw = snap("password")
    pw.signature, pw.timestamp = "sig", s.timestamp
    btn = next(e for e in pw.elements if e.label == "Log in")
    assert fast_path_blocker(pw, btn, gate.evaluate("CLICK", btn, pw)) == "credential_screen"
    pay = snap("payment")
    pay.signature, pay.timestamp = "sig", s.timestamp
    assert fast_path_blocker(pay, pay.elements[1], gate.evaluate("CLICK", pay.elements[1], pay)) == "sensitive_target"


def test_repeated_action_without_progress_hands_off():
    # settings -CLICK Bluetooth-> basic -BACK-> settings -CLICK Bluetooth-> basic -BACK-> settings: a loop.
    dev = FakeDevice(["settings", "basic"], {"Bluetooth | Off": 1, "key BACK": 0})
    click = {"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)}
    back = {"operation": ("BACK", 0.9)}
    r, _ = go(dev, [click, back, click, back, click], cond=conditions(text=["nope"]), max_steps=8)
    assert r.code == HANDOFF and r.reason == "repeated_action_without_progress"
    assert [a[0] for a in dev.actions] == ["tap", "key", "tap", "key"]  # the third tap never happened
    assert r.steps[-1]["stuck"] is True
    end = tj.read(r.trajectory)[-1]
    assert end["event"] == "end" and end["reason"] == "repeated_action_without_progress"


def test_progress_tracker_ignores_state_and_counts_per_target():
    from laya_mobile.freshness import ProgressTracker, structure_key
    assert structure_key(snap("settings")) == structure_key(snap("settings_bt_on"))  # a toggle is the same screen
    assert structure_key(snap("settings")) != structure_key(snap("basic"))
    t = ProgressTracker(max_repeats=2)
    s = snap("settings")
    a, b = s.elements[4], s.elements[5]
    t.record(s, "CLICK", a)
    t.record(s, "CLICK", b)
    assert not t.stuck(s, "CLICK", a)
    t.record(s, "CLICK", a)
    assert t.stuck(s, "CLICK", a) and not t.stuck(s, "CLICK", b) and not t.stuck(s, "BACK")


def test_step_and_run_timing_are_reported_and_recorded():
    dev = FakeDevice(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    r, _ = go(dev, [{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)},
                    {"operation": ("BACK", 0.2)}], cond=conditions(text=["nope"]))
    step = r.steps[0]
    assert set(step["timing_ms"]) >= {"observe", "decision", "policy", "execute", "settle", "total"}
    assert step["adb_calls"] == 0 and step["laya_calls"] == 1  # fake device: no adb
    t = r.to_dict()["timing"]
    assert t["steps"] == 2 and set(t) >= {"total_ms", "avg_step_ms", "p50_step_ms", "p95_step_ms",
                                          "laya_calls_per_step", "avg_settle_ms"}
    lines = tj.read(r.trajectory)
    assert lines[0]["version"] == 1  # additive fields: no schema bump
    rec = [l for l in lines if l["event"] == "step"][0]
    assert set(rec["timing_ms"]) >= {"decision", "execute", "settle", "total"}
    assert rec["settle"]["status"] == "stable" and rec["laya_calls"] == 1
    assert lines[-1]["metrics"]["steps"] == 2


def test_fixed_wait_is_still_available():
    slept = []
    dev = FakeDevice(["settings", "settings_bt_on"], {"Bluetooth | Off": 1})
    r = run_goal(dev, FakePredictor([{"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)}]),
                 session("bt"), "bt", cond=conditions(text=["On"], element=["Bluetooth | On"]), wait=0.5,
                 sleep=slept.append)
    assert r.code == DONE and slept == [0.5] and r.steps[0]["settle"]["status"] == "fixed_wait"
    assert dev.probes == 1  # only the fast-path check; no settle polling


def test_signature_older_than_the_dump_is_dropped():
    """The app changed only after the settle gave up waiting; the dump saw the change, so the
    pre-change signature must not be attached to it (it would make the next fast path wrong)."""
    from laya_mobile.metrics import Metrics
    from laya_mobile.runner import after_action

    class Late(FakeDevice):
        def probe(self):
            self.probes += 1
            return sig_of(self.screens[0])  # pixels still show the old screen

        def observe(self, signature=None):
            self.observations += 1
            s = self.screens[1]
            s.signature = signature
            return s

    dev = Late(["settings", "settings_bt_on"])
    before = dev.screens[0]
    before.signature = sig_of(before)
    clock = FakeClock()
    m = Metrics()
    m.start_step()
    snap_, info = after_action(dev, "CLICK", before, m, sleep=clock.sleep, clock=clock)
    assert info["status"] == "unchanged" and snap_.fingerprint != before.fingerprint
    assert snap_.signature is None

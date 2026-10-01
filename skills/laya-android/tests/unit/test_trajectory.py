import json
import os
import re

from helpers import by_label, snap
from laya_mobile import trajectory as tj
from laya_mobile.policy import PolicyGate


def test_default_location_is_outside_the_repo(tmp_path):
    rec = tj.TrajectoryRecorder.create("open Bluetooth settings")
    assert rec.path.startswith(str(tmp_path / "data" / "trajectories"))  # LAYA_ANDROID_DATA_DIR
    assert re.search(r"/\d{4}-\d\d-\d\dT\d{6}Z-[0-9a-f]{12}\.jsonl$", rec.path)
    assert rec.run_id in rec.path


def test_data_dir_defaults_to_home(monkeypatch):
    monkeypatch.delenv("LAYA_ANDROID_DATA_DIR")
    assert tj.data_dir() == os.path.join(os.path.expanduser("~"), ".laya-android")


def test_records_are_versioned_jsonl(tmp_path):
    path = str(tmp_path / "run.jsonl")
    rec = tj.TrajectoryRecorder.create("goal", path=path)
    before, after = snap("settings"), snap("settings_bt_on")
    sw = next(e for e in before.elements if e.role == "switch" and e.context.startswith("Bluetooth"))
    decision = {"fingerprint": before.fingerprint, "operation": "CLICK", "op_confidence": 0.83, "target": sw.id,
                "target_stable_key": sw.stable_key, "target_confidence": 0.7, "candidates": [1, 2], "ms": 12}
    rec.start(serial="emulator-5554")
    rec.step(1, "laya", before, ["BACK"], decision, None,
             dict(tj.action("CLICK", sw), success=True), PolicyGate().evaluate("CLICK", sw, before), after,
             {"screen_changed": True})
    rec.end("done", "goal verified on screen")
    lines = tj.read(path)
    assert [l["event"] for l in lines] == ["start", "step", "end"]
    assert all(l["version"] == tj.SCHEMA_VERSION == 1 and l["run_id"] == rec.run_id and l["goal"] == "goal"
               for l in lines)
    step = lines[1]
    assert step["before"]["fingerprint"] == before.fingerprint and len(step["before"]["elements"]) == 9
    assert step["model_decision"] == {"operation": "CLICK", "confidence": 0.83, "fingerprint": before.fingerprint,
                                      "target_id": sw.id, "target_stable_key": sw.stable_key,
                                      "target_confidence": 0.7, "ms": 12, "candidates": [1, 2]}
    assert step["executed"]["target_stable_key"] == sw.stable_key and step["executed"]["success"] is True
    assert step["policy"] == {"action": "allow", "reason": ""}
    assert step["after"] == {"fingerprint": after.fingerprint, "package": "com.android.settings", "activity": None}
    assert step["outcome"] == {"screen_changed": True}
    assert step["teacher_action"] is None


def test_password_never_reaches_the_file(tmp_path):
    path = str(tmp_path / "run.jsonl")
    rec = tj.TrajectoryRecorder.create("log in", path=path)
    s = snap("password")
    pw = next(e for e in s.elements if e.password)
    t = tj.typed("hunter2secret", pw, s)
    assert t == {"text": "[REDACTED]", "chars": 13}
    rec.step(1, "agent", s, teacher_action=tj.action("TYPE", pw, **t), executed=dict(tj.action("TYPE", pw, **t)))
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    assert "hunter2secret" not in raw and "[REDACTED]" in raw


def test_redact_snapshot_masks_password_text_defensively():
    d = snap("password").to_dict()
    pw = next(e for e in d["elements"] if e["password"])
    pw["text"] = "leaked"
    assert next(e for e in tj.redact_snapshot(d)["elements"] if e["password"])["text"] == "[REDACTED]"
    assert pw["text"] == "leaked"  # the input is not modified


def test_typed_text_redaction_rules():
    s = snap("password")
    user = by_label(s, "somchai")
    assert tj.typed("hello world", None, snap("basic"))["text"] == "hello world"  # no password field on screen
    assert tj.typed("hello world")["text"] == "[REDACTED]"  # unknown field: assume secret
    assert tj.typed("482913", None, snap("basic"))["text"] == "[REDACTED]"  # looks like a PIN / OTP
    assert tj.typed("somchai", user, None)["text"] == "somchai"
    assert tj.typed("anything", None, s)["text"] == "[REDACTED]"  # a password field has focus
    otp = by_label(snap("basic"), "you@example.com")
    otp.hint = "Verification code"
    assert tj.typed("ab12", otp)["text"] == "[REDACTED]"


def test_correction_record_holds_model_and_teacher(tmp_path):
    s = snap("duplicate_buy")
    a, b = s.elements[1], s.elements[2]
    laya = {"fingerprint": s.fingerprint, "operation": "CLICK", "op_confidence": 0.31, "target": a.id,
            "target_stable_key": a.stable_key, "target_confidence": 0.28}
    rec = tj.correction_record(4, s, laya, tj.action("CLICK", b), after=snap("payment"))
    assert rec["actor"] == "agent"
    assert rec["model_decision"]["target_stable_key"] == a.stable_key
    assert rec["teacher_action"]["target_stable_key"] == b.stable_key
    assert rec["outcome"] == {"screen_changed": True}
    json.dumps(rec, ensure_ascii=False)


def test_write_failure_does_not_raise(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rec = tj.TrajectoryRecorder(str(blocker / "sub" / "run.jsonl"), "r", "g")
    rec.end("done", "x")
    assert "trajectory write failed" in caplog.text

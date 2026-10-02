"""The CLI end to end against saved screens: no adb, no Laya."""
import json

import pytest

import laya_android as cli
from helpers import FakeDevice, FakePredictor, pick, snap
from laya_mobile import trajectory as tj
from laya_mobile.errors import AdbError, DeviceError


@pytest.fixture
def device(monkeypatch):
    holder = {}

    def use(*screens, transitions=None):
        holder["dev"] = FakeDevice(list(screens), transitions)
        return holder["dev"]
    monkeypatch.setattr(cli.adb_mod, "pick_serial", lambda *a: "emulator-5554")
    monkeypatch.setattr(cli.adb_mod, "Device", lambda serial, **kw: holder["dev"])
    return use


@pytest.fixture
def laya(monkeypatch):
    def use(*answers):
        p = FakePredictor(answers)
        monkeypatch.setattr(cli, "decision_client", lambda model, daemon=True: p)
        return p
    return use


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_screen_text(device, capsys):
    device("duplicate_buy")
    code, out, _ = run(capsys, "screen")
    lines = out.splitlines()
    assert code == 0
    assert lines[0].startswith("fingerprint ") and "app com.example.shop" in lines[0]
    assert lines[1] == "text: Shop | iPhone Case A | ฿199 | iPhone Case B | ฿159 | iPhone Case C | ฿99"
    assert '[2] button "Buy" context="iPhone Case B | ฿159" (900,860)' in lines


def test_screen_json(device, capsys):
    dev = device("settings")
    code, out, _ = run(capsys, "screen", "--json")
    d = json.loads(out)
    assert code == 0 and d["fingerprint"] == dev.screens[0].fingerprint
    assert d["elements"][3]["role"] == "switch" and d["elements"][3]["center"] == {"x": 950, "y": 520}


def test_tap_n_uses_last_screen_and_prints_the_new_one(device, capsys):
    dev = device("settings", "settings_bt_on", transitions={"Bluetooth | Off": 1})
    run(capsys, "screen")
    code, out, _ = run(capsys, "tap", "4", "--wait", "0")
    assert code == 0 and dev.actions == [("tap", 540, 720)]
    assert out.startswith('tapped [4] item "Bluetooth | Off" at (540,720)')
    assert 'switch "switch_widget" [on] context="Bluetooth | On"' in out


def test_tap_before_screen_is_a_usage_error(device, capsys):
    device("settings")
    code, _, err = run(capsys, "tap", "1")
    assert code == 2 and "run `screen` before `tap N`" in err


def test_tap_stale_exits_4(device, capsys):
    dev = device("settings")
    run(capsys, "screen")
    dev.screens = [snap("basic")]  # the screen changed since `screen`
    code, out, _ = run(capsys, "tap", "4")
    assert code == 4 and out.startswith("stale:") and dev.actions == []


def test_tap_coordinates(device, capsys):
    dev = device("settings")
    run(capsys, "screen")
    code, out, _ = run(capsys, "tap", "100", "200", "--no-screen")
    assert code == 0 and dev.actions == [("tap", 100, 200)] and out == "tapped (100,200)\n"


def test_sensitive_manual_tap_warns_but_runs(device, capsys):
    dev = device("payment")
    run(capsys, "screen")
    code, _, err = run(capsys, "tap", "1", "--no-screen")
    assert code == 0 and dev.actions == [("tap", 800, 2170)]
    assert "policy handoff" in err and "payment" in err


def test_type_key_swipe(device, capsys):
    dev = device("basic")
    run(capsys, "screen")
    assert run(capsys, "type", "hello world", "--no-screen")[1] == "typed 11 chars\n"
    assert run(capsys, "key", "back", "--no-screen")[1] == "key BACK\n"
    assert run(capsys, "swipe", "up", "--no-screen")[1] == "swiped up\n"
    assert dev.actions == [("type", "hello world"), ("key", "BACK"), ("swipe", "up")]


def test_type_non_ascii_is_a_usage_error(capsys, monkeypatch):
    real = cli.adb_mod.Device("x")
    monkeypatch.setattr(real, "adb", lambda *a: pytest.fail("adb must not run"))
    monkeypatch.setattr(cli.adb_mod, "pick_serial", lambda *a: "x")
    monkeypatch.setattr(cli.adb_mod, "Device", lambda serial, **kw: real)
    code, _, err = run(capsys, "type", "สวัสดี", "--no-screen")
    assert code == 2 and "ASCII-only" in err


def test_goal_record_manual_steps_and_finish(device, capsys, tmp_path):
    device("password")
    code, out, _ = run(capsys, "goal", "log in", "--record")
    assert code == 0 and "recording to" in out
    path = out.strip().rsplit(" ", 1)[-1]
    run(capsys, "screen")
    run(capsys, "type", "hunter2secret", "--wait", "0")  # the password field has focus
    run(capsys, "tap", "2", "--wait", "0")
    run(capsys, "finish", "ok", "--note", "logged in")
    lines = tj.read(path)
    assert [l["event"] for l in lines] == ["start", "step", "step", "end"]
    typed = lines[1]
    assert typed["actor"] == "agent" and typed["teacher_action"]["text"] == "[REDACTED]"
    assert typed["teacher_action"]["chars"] == 13 and typed["policy"]["reason"] == "sensitive_input"
    assert lines[2]["teacher_action"]["target_label"] == "Log in"
    assert lines[3] == dict(lines[3], result="ok", reason="logged in")
    with open(path, encoding="utf-8") as f:
        assert "hunter2secret" not in f.read()


def test_typing_after_a_blind_tap_is_redacted(device, capsys):
    device("password")
    path = run(capsys, "goal", "log in", "--record")[1].strip().rsplit(" ", 1)[-1]
    run(capsys, "screen")
    run(capsys, "tap", "1", "--no-screen")  # focus the password field, screen not printed
    run(capsys, "type", "hunter2secret", "--no-screen")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    assert "hunter2secret" not in raw and '"chars": 13' in raw


def test_actions_are_not_recorded_without_opt_in(device, capsys, tmp_path):
    device("basic")
    run(capsys, "goal", "x")
    run(capsys, "screen")
    run(capsys, "tap", "2", "--no-screen")
    assert not (tmp_path / "data").exists()


def test_record_env_opt_in(device, capsys, monkeypatch):
    monkeypatch.setenv("LAYA_ANDROID_RECORD", "1")
    device("basic")
    assert "recording to" in run(capsys, "goal", "x")[1]


def test_verify_exit_codes(device, capsys):
    device("settings")
    code, out, _ = run(capsys, "verify", "--text", "Bluetooth", "--package", "com.android.settings")
    assert code == 0 and json.loads(out) == {"met": True, "unmet": []}
    code, out, _ = run(capsys, "verify", "--element", "Display")
    assert code == 1 and json.loads(out)["unmet"] == ['no element "Display"']
    assert run(capsys, "verify")[0] == 2


def test_pick(device, laya, capsys):
    device("duplicate_buy")
    laya({"operation": ("CLICK", 0.9), "target": (pick("Case B"), 0.8)})
    code, out, _ = run(capsys, "pick", "buy case B")
    d = json.loads(out)
    assert d["operation"] == "CLICK" and d["target"]["id"] == 2 and d["candidates"] == 3
    assert d["policy"]["matched"] == "payment"
    assert code == 3  # confident, but the gate would stop it


def test_check(device, laya, capsys):
    device("settings")
    laya({"q": 0.9})
    assert run(capsys, "check", "wifi on?")[0] == 0
    laya({"q": 0.1})
    assert run(capsys, "check", "wifi on?")[0] == 1
    laya({"q": 0.5})
    assert run(capsys, "check", "wifi on?")[0] == 3


def test_run_records_and_prints_json(device, laya, capsys, tmp_path):
    device("settings", "settings_bt_on", transitions={"Bluetooth | Off": 1})
    laya({"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)})
    traj = str(tmp_path / "run.jsonl")
    code, out, err = run(capsys, "-v", "run", "turn on bluetooth", "--until-text", "On", "--until-element",
                         "Bluetooth | On", "--wait", "0", "--trajectory", traj)
    d = json.loads(out)
    assert code == 0 and d["result"] == "done" and d["trajectory"] == traj
    assert d["steps"][0]["target"]["element"].startswith('item "Bluetooth | Off"')
    assert "step 1: CLICK" in err and "candidates=" in err and "policy: allow" in err and "screen changed: True" in err
    assert [l["event"] for l in tj.read(traj)] == ["start", "step", "end"]


def test_run_policy_handoff_output(device, laya, capsys):
    device("payment")
    laya({"operation": ("CLICK", 0.95), "target": (pick("Confirm payment"), 0.95)})
    code, out, err = run(capsys, "run", "pay", "--until-text", "Thank you")
    d = json.loads(out)
    assert code == 3 and d["result"] == "handoff" and d["reason"] == "sensitive_action"
    assert d["policy"]["matched"] == "payment" and d["policy"]["target"] == "Confirm payment"
    assert "candidates=" not in err  # no diagnostics without --verbose


def test_run_allow_category(device, laya, capsys):
    device("payment")
    laya({"operation": ("CLICK", 0.95), "target": (pick("Confirm payment"), 0.95)})
    code, out, _ = run(capsys, "run", "pay", "--until-text", "Thank you", "--allow", "payment", "--wait", "0")
    assert json.loads(out)["reason"].startswith("screen did not change")  # it did tap


def test_device_errors_exit_2(monkeypatch, capsys):
    def boom(*a):
        raise DeviceError("no adb device")
    monkeypatch.setattr(cli.adb_mod, "pick_serial", boom)
    code, _, err = run(capsys, "screen")
    assert code == 2 and "no adb device" in err

    class Broken:
        def observe(self, signature=None):
            raise AdbError("adb not found on PATH")
    monkeypatch.setattr(cli.adb_mod, "pick_serial", lambda *a: "x")
    monkeypatch.setattr(cli.adb_mod, "Device", lambda serial, **kw: Broken())
    assert run(capsys, "screen")[0] == 2


# ---------- Phase 3 ----------

def test_tap_after_screen_takes_the_fast_path(device, capsys):
    dev = device("settings", "settings_bt_on", transitions={"Bluetooth | Off": 1})
    run(capsys, "screen")
    code, out, err = run(capsys, "-v", "tap", "4")
    assert code == 0 and dev.actions == [("tap", 540, 720)]
    assert "path: fast" in err
    assert dev.observations == 2  # `screen` and the screen printed after the tap: no re-dump before it
    assert 'switch "switch_widget" [on]' in out  # adaptive settle, then the new screen


def test_tap_after_the_screen_changed_takes_the_safe_path(device, capsys):
    dev = device("settings", "settings_bt_on")
    run(capsys, "screen")
    dev.i = 1  # Bluetooth turned on by itself: same element, different screen
    code, _, err = run(capsys, "-v", "tap", "4", "--no-screen")
    assert code == 0 and "path: safe (screen_changed)" in err and dev.observations == 2


def test_verbose_prints_timing_and_keeps_stdout_clean(device, capsys):
    device("settings")
    code, out, err = run(capsys, "-v", "screen", "--json")
    json.loads(out)  # still valid JSON
    assert "observe: " in err and "total: " in err and "adb calls: 0 (dumps 0, probes 0)  laya calls: 0" in err
    code, out, err = run(capsys, "screen")
    assert "total:" not in err


def test_run_json_has_step_and_run_timing(device, laya, capsys):
    device("settings", "settings_bt_on", transitions={"Bluetooth | Off": 1})
    laya({"operation": ("CLICK", 0.9), "target": (pick('"Bluetooth | Off"'), 0.9)})
    code, out, err = run(capsys, "-v", "run", "turn on bluetooth", "--until-text", "On", "--until-element",
                         "Bluetooth | On")
    d = json.loads(out)
    assert code == 0 and d["steps"][0]["path"] == "fast" and d["steps"][0]["settle"]["status"] == "stable"
    assert set(d["steps"][0]["timing_ms"]) >= {"observe", "decision", "policy", "execute", "settle", "total"}
    assert d["timing"]["steps"] == 1 and d["timing"]["laya_calls_per_step"] == 1
    assert "settle: stable" in err and "laya calls: 1" in err


def test_no_daemon_flag_uses_in_process_laya(device, monkeypatch, capsys):
    device("settings")
    seen = []

    def client(model, daemon=True):
        seen.append((model, daemon))
        return FakePredictor([{"q": 0.9}])
    monkeypatch.setattr(cli, "decision_client", client)
    run(capsys, "--no-daemon", "--model", "en", "check", "x?")
    run(capsys, "check", "x?")
    assert seen == [("en", False), ("ml", True)]


def test_daemon_status_and_stop_need_no_device(monkeypatch, capsys):
    monkeypatch.setattr(cli.adb_mod, "pick_serial", lambda *a: pytest.fail("no device needed"))
    code, out, _ = run(capsys, "daemon", "status")
    assert code == 1 and json.loads(out)["daemon"] == "stopped"
    code, out, _ = run(capsys, "daemon", "stop")
    assert code == 0 and json.loads(out) == {"stopped": False}


def test_benchmark_without_device(monkeypatch, capsys):
    from laya_mobile import benchmark
    monkeypatch.setattr(benchmark, "load_laya", lambda model: None)
    monkeypatch.setattr(benchmark, "InProcessDecisionClient",
                        lambda model: FakePredictor([{"operation": ("BACK", 0.5)}] * 10))
    code, out, err = run(capsys, "--no-daemon", "benchmark", "--no-device", "--iterations", "3", "--json")
    rep = json.loads(out)
    assert code == 0 and rep["laya"]["mode"] == "in-process" and rep["laya"]["warm_ms"]["n"] == 3
    assert rep["parse_ms"]["n"] == 3 and "device" not in rep
    assert "Device: not measured" in err


def test_device_info_is_cached_in_the_session(tmp_path):
    from laya_mobile.adb import DeviceInfo
    from laya_mobile.session import Session

    class Dev:
        _info = DeviceInfo(1080, 2400, 420, 132)
    s = Session("emu")
    s.remember_device(Dev())
    assert Session.load("emu").device_info() == DeviceInfo(1080, 2400, 420, 132)

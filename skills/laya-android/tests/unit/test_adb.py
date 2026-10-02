"""AdbClient and the Device session against a scripted subprocess runner: no adb needed."""
import subprocess

import pytest

from helpers import xml
from laya_mobile import adb
from laya_mobile.adb import AdbClient, Device, DeviceInfo, parse_info, pick_serial
from laya_mobile.errors import AdbError, DeviceError
from laya_mobile.metrics import Metrics
from laya_mobile.observe import ACTIVITY_MARK

INFO_OUT = ("Physical size: 1080x2400\nPhysical density: 420\n%s\n"
            "        InsetsSource id=1 type=statusBars frame=[0,0][1080,132] visible=true\n%s\n36\nPixel 8\n"
            % (adb.INFO_MARK, adb.INFO_MARK))
MD5 = "c2cee059957131725ecee7f0f784921a"
FOCUS = ("  mCurrentFocus=Window{a1 u0 com.android.settings/com.android.settings.SubSettings}\n"
         "  mFocusedApp=ActivityRecord{2 u0 com.android.settings/.SubSettings t26}\n")


class Runner:
    """Answers adb commands by the first rule whose key is in the command line."""

    def __init__(self, rules):
        self.rules = rules
        self.cmds = []

    def __call__(self, cmd, text=True, timeout=None, **kw):
        line = " ".join(cmd)
        self.cmds.append(line)
        for key, out in self.rules:
            if key in line:
                if isinstance(out, BaseException):
                    raise out
                rc, out = out if isinstance(out, tuple) else (0, out)
                return subprocess.CompletedProcess(cmd, rc, out, "boom" if rc else "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def count(self, key):
        return sum(key in c for c in self.cmds)


def observe_out(name="settings", pre=MD5, secure=0):
    head = "%s  -\n%s\n" % (pre, adb.SIG_MARK) if pre else ""
    return "%s%s\nUI hierchary dumped to: /dev/tty\n%s\n%s%s\n%d\n" % (
        head, xml(name), ACTIVITY_MARK, FOCUS, adb.SECURE_MARK, secure)


def device(rules, info=None, metrics=None):
    r = Runner(rules)
    return Device("emu", metrics=metrics, info=info, adb=AdbClient("emu", runner=r)), r


def test_every_call_is_timed_and_counted():
    m = Metrics()
    m.start_step()
    c = AdbClient("emu", metrics=m, runner=Runner([("input", "")]))
    c.tap(1, 2)
    c.key("back")
    rec = m.end_step()
    assert rec["adb_calls"] == 2 and len(c.calls) == 2
    assert c.calls[0][0] == ("shell", "input", "tap", "1", "2")
    assert all(ms >= 0 for _, ms in c.calls)


def test_errors_are_typed_and_bounded():
    c = AdbClient("emu", runner=Runner([("tap", subprocess.TimeoutExpired("adb", 30))]))
    with pytest.raises(AdbError, match="timed out after 30s"):
        c.tap(1, 2)
    c = AdbClient("emu", runner=Runner([("tap", FileNotFoundError())]))
    with pytest.raises(AdbError, match="adb not found"):
        c.tap(1, 2)
    c = AdbClient("emu", runner=Runner([("tap", (1, ""))]))
    with pytest.raises(AdbError, match="adb failed"):
        c.tap(1, 2)


def test_pick_serial_single_device_needs_one_call():
    r = Runner([("devices", "List of devices attached\nemulator-5554\tdevice\n")])
    assert pick_serial(AdbClient(runner=r)) == "emulator-5554"
    assert r.cmds == ["adb devices"]


def test_pick_serial_wireless_duplicate_prefers_mdns():
    mdns = "adb-R5CT-abc._adb-tls-connect._tcp"
    r = Runner([("devices", "List of devices attached\n192.168.1.5:40000\tdevice\n%s\tdevice\n" % mdns),
                ("ro.serialno", "R5CT\n")])
    assert pick_serial(AdbClient(runner=r)) == mdns
    r = Runner([("devices", "List\nA\tdevice\nB\tdevice\n"), ("-s A", "one\n"), ("-s B", "two\n")])
    with pytest.raises(DeviceError, match="several devices"):
        pick_serial(AdbClient(runner=r))
    with pytest.raises(DeviceError, match="no adb device"):
        pick_serial(AdbClient(runner=Runner([("devices", "List of devices attached\n")])))


def test_parse_info():
    i = parse_info(INFO_OUT)
    assert (i.width, i.height, i.density, i.status_bar, i.sdk, i.model) == (1080, 2400, 420, 132, 36, "Pixel 8")
    i = parse_info("Physical size: 1080x2400\nOverride size: 720x1600\nPhysical density: 320\n")
    assert (i.width, i.height, i.status_bar) == (720, 1600, 96)  # no inset line: 48 dp
    with pytest.raises(AdbError):
        parse_info("error: closed")
    assert DeviceInfo.from_dict(i.to_dict()) == i and DeviceInfo.from_dict({"bad": 1}) is None


def test_screen_size_is_queried_once_not_per_swipe():
    d, r = device([("wm size", INFO_OUT), ("input", "")])
    d.swipe("up")
    d.swipe("down")
    d.swipe("left")
    assert r.count("wm size") == 1 and r.count("input swipe") == 3
    assert r.cmds[1].endswith("input swipe 540 2000 540 400 300")


def test_cached_device_info_means_no_query_at_all():
    d, r = device([("input", "")], info=DeviceInfo(1080, 2400, 420, 132))
    d.swipe("up")
    assert r.cmds == ["adb -s emu shell input swipe 540 2000 540 400 300"]


def test_swipe_follows_rotation_without_a_query():
    d, r = device([("input", "")], info=DeviceInfo(1080, 2400, 420, 132))
    d.rotation = 1  # landscape, as read from the last dump
    d.swipe("left")
    assert r.cmds[-1].endswith("input swipe 2000 540 400 540 300")


def test_observe_is_one_call_with_signature_activity_and_rotation():
    d, r = device([("uiautomator", observe_out())], info=DeviceInfo(1080, 2400, 420, 132))
    s = d.observe()
    assert len(r.cmds) == 1
    assert "screencap | tail -c +%d | md5sum" % (16 + 1080 * 4 * 132 + 1) in r.cmds[0]
    assert r.cmds[0].index("md5sum") < r.cmds[0].index("uiautomator dump")  # signature first
    assert s.signature == MD5 and not s.secure and s.rotation == 0
    assert s.activity == "com.android.settings.SubSettings" and s.package == "com.android.settings"
    assert d.last_snapshot is s and d.last_fingerprint == s.fingerprint


def test_dumps_and_probes_are_counted_apart():
    m = Metrics()
    m.start_step()
    d, _ = device([("uiautomator", observe_out()), ("md5sum", "%s  -\n" % MD5)], info=DeviceInfo(1080, 2400),
                  metrics=m)
    d.observe()
    d.probe()
    d.probe()
    rec = m.end_step()
    assert (rec["adb_calls"], rec["dumps"], rec["probes"]) == (3, 1, 2)


def test_observe_reuses_a_settled_signature():
    d, r = device([("uiautomator", observe_out(pre=None))], info=DeviceInfo(1080, 2400, 420, 132))
    s = d.observe(signature="settled-sig")
    assert "md5sum" not in r.cmds[0] and s.signature == "settled-sig"


def test_secure_window_has_no_signature():
    d, _ = device([("uiautomator", observe_out(secure=1))], info=DeviceInfo(1080, 2400, 420, 132))
    s = d.observe()
    assert s.secure and s.signature is None


def test_observe_retries_once_then_fails(monkeypatch):
    monkeypatch.setattr(adb.time, "sleep", lambda s: None)
    d, r = device([("uiautomator", "ERROR: null root node returned by UiTestAutomationBridge.\n")],
                  info=DeviceInfo(1080, 2400))
    from laya_mobile.errors import ObservationError
    with pytest.raises(ObservationError):
        d.observe()
    assert r.count("uiautomator") == 2


def test_probe():
    d, r = device([("md5sum", "%s  -\n" % MD5)], info=DeviceInfo(1080, 2400, 420, 132))
    assert d.probe() == MD5 and len(r.cmds) == 1 and "uiautomator" not in r.cmds[0]
    d, _ = device([("md5sum", "screencap: permission denied\n")], info=DeviceInfo(1080, 2400))
    assert d.probe() is None


def test_rotation_is_read_from_the_dump():
    landscape = observe_out().replace('<hierarchy rotation="0">', '<hierarchy rotation="1">')
    d, _ = device([("uiautomator", landscape)], info=DeviceInfo(1080, 2400))
    assert d.observe().rotation == 1 and d.screen_size() == (2400, 1080)

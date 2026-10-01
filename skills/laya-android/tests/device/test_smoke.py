"""Read-only checks against a real device. Run with: pytest tests/device --device
They never tap or type; leave the phone unlocked on any screen."""
import json
import subprocess
import sys
import os

import pytest

pytestmark = pytest.mark.device
SKILL = os.path.join(os.path.dirname(__file__), "..", "..")


@pytest.fixture(scope="module")
def device():
    from laya_mobile.adb import Device, pick_serial
    return Device(pick_serial())


def test_observe_gives_a_structured_snapshot(device):
    s = device.observe()
    assert s.package and len(s.fingerprint) == 16
    assert all(e.stable_key and len(e.bounds) == 4 for e in s.elements)
    json.dumps(s.to_dict(), ensure_ascii=False)


def test_idle_screen_keeps_its_fingerprint(device):
    a, b = device.observe(), device.observe()
    assert a.fingerprint == b.fingerprint, "screen changed between two dumps (animation, ticker?)"
    assert sorted(e.stable_key for e in a.elements) == sorted(e.stable_key for e in b.elements)


def test_cli_screen_json(device):
    out = subprocess.run([sys.executable, os.path.join(SKILL, "laya_android.py"), "-s", device.serial, "screen",
                          "--json"], capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["fingerprint"]

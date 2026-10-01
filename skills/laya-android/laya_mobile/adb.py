"""adb transport: device selection, screen dump and input. Raises AdbError/DeviceError/ObservationError."""
from __future__ import annotations

import subprocess
import time

from .errors import AdbError, DeviceError, ObservationError, UsageError
from .observe import ACTIVITY_MARK, parse_activity, snapshot_from_xml, split_dump


def _run(cmd, **kw):
    kw.setdefault("text", True)
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError:
        raise AdbError("adb not found on PATH (install Android platform-tools)") from None


def pick_serial():
    """The one device to use when -s is not given.

    Wireless debugging lists the same phone twice: as ip:port and as an mDNS
    `adb-<serialno>-xxx._adb-tls-connect._tcp` name. Those count as one device; the
    mDNS name is preferred because it survives the port changing on reconnect.
    """
    lines = _run(["adb", "devices"], capture_output=True).stdout.splitlines()[1:]
    serials = [l.split("\t")[0] for l in lines if l.endswith("\tdevice")]
    if not serials:
        raise DeviceError("no adb device. Wireless: enable Wireless debugging, then `adb pair ip:port` "
                          "(once) and `adb connect ip:port`")
    phones = {}
    for s in serials:
        hw = _run(["adb", "-s", s, "shell", "getprop", "ro.serialno"], capture_output=True).stdout.strip() or s
        phones.setdefault(hw, []).append(s)
    if len(phones) > 1:
        raise DeviceError("several devices connected, pass -s: %s" % ", ".join(serials))
    (names,) = phones.values()
    return next((s for s in names if "._adb-tls-connect." in s), names[0])


class Device:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args):
        cmd = ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)
        r = _run(cmd, capture_output=True)
        if r.returncode != 0:
            raise AdbError("adb failed: %s\n%s" % (" ".join(cmd), (r.stderr or "").strip()))
        return r.stdout

    def observe(self):
        """Dump the UI and read the focused activity in one adb round trip."""
        # `; true`: grep finding nothing must not turn into an adb failure.
        script = ("uiautomator dump /dev/tty; echo %s; dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; true"
                  % ACTIVITY_MARK)
        for _ in range(2):
            xml, focus = split_dump(self.adb("exec-out", script))
            if xml:
                break
            time.sleep(1)  # "null root node" while the screen is animating or waking
        else:
            raise ObservationError("uiautomator dump returned no hierarchy (screen off/locked or secure window?)")
        return snapshot_from_xml(xml, activity=parse_activity(focus)[1])

    def tap(self, x, y):
        self.adb("shell", "input", "tap", str(x), str(y))

    def swipe(self, direction):
        size = self.adb("shell", "wm", "size").strip().rsplit(" ", 1)[-1]
        w, h = map(int, size.split("x"))
        cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
        # "up" scrolls content up, i.e. the finger moves from bottom to top.
        x1, y1, x2, y2 = {
            "up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy),
        }[direction]
        self.adb("shell", "input", "swipe", *map(str, (x1, y1, x2, y2, 300)))

    def key(self, name):
        self.adb("shell", "input", "keyevent", "KEYCODE_" + name.upper())

    def type_text(self, text):
        # `input text` needs spaces as %s and cannot type non-ASCII (e.g. Thai).
        if any(ord(c) > 127 for c in text):
            raise UsageError("adb `input text` is ASCII-only; use an IME such as ADBKeyboard for non-ASCII text")
        self.adb("shell", "input", "text", text.replace(" ", "%s"))

    def screenshot(self, out):
        with open(out, "wb") as f:
            r = _run(["adb"] + (["-s", self.serial] if self.serial else []) + ["exec-out", "screencap", "-p"],
                     stdout=f, text=False)
        if r.returncode != 0:
            raise AdbError("screencap failed")
        return out

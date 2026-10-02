"""adb transport: one AdbClient runs every adb process (timed, counted, bounded by a timeout);
Device is the per-device session: cached device info, full observation, the cheap screen
probe and input. Raises AdbError/DeviceError/ObservationError."""
from __future__ import annotations

import math
import re
import subprocess
import time
from dataclasses import asdict, dataclass

from . import config
from .errors import AdbError, DeviceError, ObservationError, UsageError
from .observe import ACTIVITY_MARK, parse_activity, snapshot_from_xml

SIG_MARK = "__LAYA_SIG__"
SECURE_MARK = "__LAYA_SECURE__"
INFO_MARK = "__LAYA_INFO__"
STATUS_BAR_DP = 48  # fallback status bar height when dumpsys does not report it
SCREENCAP_HEADER = 16  # width, height, format, dataspace (12 bytes before Android 9: the hash is still stable)


@dataclass
class AdbResult:
    stdout: object
    stderr: str
    returncode: int
    duration_ms: float


class AdbClient:
    """The only place an adb process is started. Every call is bounded by a timeout, timed,
    and reported to `metrics` (kind "adb") when one is attached."""

    def __init__(self, serial=None, metrics=None, timeout=config.ADB_TIMEOUT_S, runner=None):
        self.serial, self.metrics, self.timeout = serial, metrics, timeout
        self.runner = runner or subprocess.run
        self.calls = []  # (args, duration_ms), for diagnostics and tests

    def command(self, args):
        return ["adb"] + (["-s", self.serial] if self.serial else []) + list(args)

    def run(self, *args, timeout=None, text=True, stdout=None, check=True):
        cmd = self.command(args)
        kw = {"stdout": stdout} if stdout is not None else {"capture_output": True}
        if stdout is not None:
            kw["stderr"] = subprocess.PIPE
        t0 = time.perf_counter()
        try:
            r = self.runner(cmd, text=text, timeout=timeout or self.timeout, **kw)
        except FileNotFoundError:
            raise AdbError("adb not found on PATH (install Android platform-tools)") from None
        except subprocess.TimeoutExpired:
            raise AdbError("adb timed out after %ss: %s" % (timeout or self.timeout, " ".join(cmd))) from None
        finally:
            ms = (time.perf_counter() - t0) * 1000
            self.calls.append((tuple(args), ms))
            if self.metrics is not None:
                self.metrics.call("adb", ms)
        err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
        res = AdbResult(r.stdout, err or "", r.returncode, ms)
        if check and r.returncode != 0:
            raise AdbError("adb failed: %s\n%s" % (" ".join(cmd), res.stderr.strip()))
        return res

    def shell(self, *args, **kw):
        return self.run("shell", *args, **kw)

    def exec_out(self, script, **kw):
        return self.run("exec-out", script, **kw)

    def tap(self, x, y):
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1, y1, x2, y2, ms=300):
        self.shell("input", "swipe", *map(str, (x1, y1, x2, y2, ms)))

    def key(self, name):
        self.shell("input", "keyevent", "KEYCODE_" + name.upper())

    def text(self, s):
        self.shell("input", "text", s)

    def screencap(self, out):
        with open(out, "wb") as f:
            self.run("exec-out", "screencap", "-p", text=False, stdout=f)
        return out


def pick_serial(adb=None):
    """The one device to use when -s is not given.

    Wireless debugging lists the same phone twice: as ip:port and as an mDNS
    `adb-<serialno>-xxx._adb-tls-connect._tcp` name. Those count as one device; the
    mDNS name is preferred because it survives the port changing on reconnect. With a single
    entry there is nothing to merge, so no per-device query is made.
    """
    adb = adb or AdbClient()
    lines = adb.run("devices", check=False).stdout.splitlines()[1:]
    serials = [l.split("\t")[0] for l in lines if l.endswith("\tdevice")]
    if not serials:
        raise DeviceError("no adb device. Wireless: enable Wireless debugging, then `adb pair ip:port` "
                          "(once) and `adb connect ip:port`")
    if len(serials) == 1:
        return serials[0]
    phones = {}
    for s in serials:
        r = AdbClient(s, adb.metrics, runner=adb.runner).shell("getprop", "ro.serialno", check=False)
        phones.setdefault(r.stdout.strip() or s, []).append(s)
    if len(phones) > 1:
        raise DeviceError("several devices connected, pass -s: %s" % ", ".join(serials))
    (names,) = phones.values()
    return next((s for s in names if "._adb-tls-connect." in s), names[0])


@dataclass
class DeviceInfo:
    """What rarely changes: queried once per session (one adb call) and cached in the session file."""
    width: int
    height: int
    density: int = 0
    status_bar: int = 0  # px at the top that the screen signature leaves out (clock, icons)
    sdk: int = 0
    model: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        try:
            return cls(**{k: d[k] for k in ("width", "height", "density", "status_bar", "sdk", "model") if k in d})
        except (TypeError, KeyError):
            return None


INFO_SCRIPT = ("wm size; wm density; echo %s; dumpsys window | grep -m1 'type=statusBars frame'; echo %s; "
               "getprop ro.build.version.sdk; getprop ro.product.model; true" % (INFO_MARK, INFO_MARK))


def parse_info(out):
    size = re.findall(r"(?:Physical|Override) size: (\d+)x(\d+)", out)
    dens = re.findall(r"(?:Physical|Override) density: (\d+)", out)
    if not size:
        raise AdbError("could not read the screen size (wm size): %r" % out[:200])
    w, h = map(int, size[-1])  # an override wins over the physical size
    density = int(dens[-1]) if dens else 0
    parts = out.split(INFO_MARK)
    m = re.search(r"frame=\[\d+,\d+\]\[\d+,(\d+)\]", parts[1] if len(parts) > 1 else "")
    status = int(m.group(1)) if m else math.ceil(STATUS_BAR_DP * (density or 160) / 160)
    tail = (parts[2] if len(parts) > 2 else "").split()
    sdk = int(tail[0]) if tail and tail[0].isdigit() else 0
    return DeviceInfo(w, h, density, status, sdk, " ".join(tail[1:]))


def parse_observe(out):
    """Output of the observe script -> (pre-dump signature or None, xml or None, focus lines, secure)."""
    sig = None
    if SIG_MARK in out:
        head, out = out.split(SIG_MARK, 1)
        sig = signature_from(head)
    end = out.rfind("</hierarchy>")
    if end < 0:
        return sig, None, "", False
    xml = out[out.find("<"):end + len("</hierarchy>")]
    tail = out[end:]
    focus = tail.split(ACTIVITY_MARK, 1)[1] if ACTIVITY_MARK in tail else ""
    secure = False
    if SECURE_MARK in focus:
        focus, rest = focus.split(SECURE_MARK, 1)
        secure = (rest.split() or ["0"])[0] != "0"
    return sig, xml, focus, secure


def signature_from(out):
    """Probe output -> the screen hash, or None when screencap gave nothing usable."""
    m = re.search(r"\b([0-9a-f]{32})\b", out or "")
    return m.group(1) if m else None


class Device:
    """Device session: one per CLI invocation or `run`, reused for every step.

    observe()  full observation: uiautomator dump + focused activity + screen signature, one adb call
    probe()    light observation: the screen signature only (~120-150 ms vs ~2 s for a dump)
    """

    def __init__(self, serial, metrics=None, info=None, adb=None):
        self.serial = serial
        self.adb = adb or AdbClient(serial, metrics)
        if metrics is not None:
            self.adb.metrics = metrics
        self._info = info
        self.last_snapshot = None
        self.rotation = 0

    @property
    def metrics(self):
        return self.adb.metrics

    @property
    def info(self):
        if self._info is None:
            self._info = parse_info(self.adb.shell(INFO_SCRIPT).stdout)
        return self._info

    @property
    def last_fingerprint(self):
        return self.last_snapshot.fingerprint if self.last_snapshot else None

    def probe_script(self):
        """Hash of the screen below the status bar, computed on the device (36 bytes come back).
        Changes when anything visible changes; the status bar clock does not. (Adding the focused
        window from dumpsys measured +100 ms per probe; secure windows, whose screenshots are
        black, are caught by observe() instead.)"""
        i = self.info
        skip = SCREENCAP_HEADER + i.width * 4 * i.status_bar
        return "screencap | tail -c +%d | md5sum" % (skip + 1)

    def _count(self, kind, t0):
        if self.metrics is not None:
            self.metrics.call(kind, (time.perf_counter() - t0) * 1000)

    def probe(self):
        t0 = time.perf_counter()
        try:
            return signature_from(self.adb.shell(self.probe_script()).stdout)
        finally:
            self._count("probe", t0)

    def observe(self, signature=None):
        """Dump the UI, read the focused activity and whether a secure window is shown, in one adb
        round trip. The screen signature is taken *before* the dump (or passed in by a settle
        loop that just took it): if the screen changes while dumping, a later probe will not
        match it, so a stale snapshot can never pass as fresh."""
        pre = "" if signature else "%s; echo %s; " % (self.probe_script(), SIG_MARK)
        # `; true`: grep finding nothing must not turn into an adb failure.
        script = ("%suiautomator dump /dev/tty; echo %s; dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; "
                  "echo %s; dumpsys window windows | grep -c ' fl=.*SECURE'; true"
                  % (pre, ACTIVITY_MARK, SECURE_MARK))
        for _ in range(2):
            t0 = time.perf_counter()
            try:
                sig, xml, focus, secure = parse_observe(self.adb.exec_out(script).stdout)
            finally:
                self._count("dump", t0)
            if xml:
                break
            time.sleep(1)  # "null root node" while the screen is animating or waking
        else:
            raise ObservationError("uiautomator dump returned no hierarchy (screen off/locked or secure window?)")
        snap = snapshot_from_xml(xml, activity=parse_activity(focus)[1])
        snap.signature = None if secure else (signature or sig)
        snap.secure = secure
        self.rotation = snap.rotation
        self.last_snapshot = snap
        return snap

    def tap(self, x, y):
        self.adb.tap(x, y)

    def screen_size(self):
        """(width, height) as the screen is currently rotated; no adb call once info is cached."""
        w, h = self.info.width, self.info.height
        return (h, w) if self.rotation in (1, 3) else (w, h)

    def swipe(self, direction):
        w, h = self.screen_size()
        cx, cy, dx, dy = w // 2, h // 2, w // 3, h // 3
        # "up" scrolls content up, i.e. the finger moves from bottom to top.
        x1, y1, x2, y2 = {
            "up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy),
        }[direction]
        self.adb.swipe(x1, y1, x2, y2, 300)

    def key(self, name):
        self.adb.key(name)

    def type_text(self, text):
        # `input text` needs spaces as %s and cannot type non-ASCII (e.g. Thai).
        if any(ord(c) > 127 for c in text):
            raise UsageError("adb `input text` is ASCII-only; use an IME such as ADBKeyboard for non-ASCII text")
        self.adb.text(text.replace(" ", "%s"))

    def screenshot(self, out):
        try:
            return self.adb.screencap(out)
        except AdbError:
            raise AdbError("screencap failed") from None

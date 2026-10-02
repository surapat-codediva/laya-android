"""`la benchmark`: component latencies, measured on this machine and device right now.

Device-free parts (always): Laya through the daemon (or in-process with --no-daemon) and
snapshot parsing + fingerprinting. With a device: one adb round trip, the cheap screen probe,
and a full observation; --step adds full safe steps (observe -> decide -> policy gate -> a
no-op key event -> settle -> observe). Nothing is ever tapped or typed.
"""
from __future__ import annotations

import time

from .decision import DaemonDecisionClient, InProcessDecisionClient, decide, load_laya
from .metrics import PHASES, Metrics, Metered, summarize
from .observe import snapshot_from_xml
from .policy import PolicyGate
from .runner import after_action

GOAL = "open Bluetooth settings"
DONE_Q = "Has this goal been reached on the current screen: %s?" % GOAL
ROWS = ["Network & internet|Mobile, Wi-Fi, hotspot", "Connected devices|Bluetooth, pairing",
        "Apps|Assistant, recent apps", "Notifications|Notification history", "Battery|100%",
        "Storage|34% used", "Sound & vibration|Volume, haptics", "Display|Dark theme, font size",
        "Accessibility|Display, interaction", "Security & privacy|App security, device lock",
        "Location|On - 3 apps have access", "System|Languages, gestures, time"]


def synthetic_xml():
    """A Settings-like screen (12 rows) for device-free benchmarks."""
    node = ('<node index="0" text="{t}" resource-id="{rid}" class="{cls}" package="com.android.settings" '
            'content-desc="" checkable="false" checked="false" clickable="{c}" enabled="true" focusable="{c}" '
            'focused="false" scrollable="{s}" long-clickable="false" password="false" selected="false" '
            'bounds="[{x1},{y1}][{x2},{y2}]">')
    parts = ["<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">",
             node.format(t="", rid="", cls="android.widget.FrameLayout", c="false", s="false", x1=0, y1=0, x2=1080,
                         y2=2400),
             node.format(t="", rid="com.android.settings:id/recycler_view", cls="androidx.recyclerview.widget."
                         "RecyclerView", c="false", s="true", x1=0, y1=200, x2=1080, y2=2400)]
    for i, row in enumerate(ROWS):
        title, sub = row.split("|")
        y = 200 + i * 180
        parts.append(node.format(t="", rid="", cls="android.widget.LinearLayout", c="true", s="false", x1=0, y1=y,
                                 x2=1080, y2=y + 180))
        parts.append(node.format(t=title.replace("&", "&amp;"), rid="android:id/title",
                                 cls="android.widget.TextView", c="false", s="false", x1=150, y1=y + 30, x2=900,
                                 y2=y + 90) + "</node>")
        parts.append(node.format(t=sub, rid="android:id/summary", cls="android.widget.TextView", c="false", s="false",
                                 x1=150, y1=y + 95, x2=900, y2=y + 150) + "</node>")
        parts.append("</node>")
    parts.append("</node></node></hierarchy>")
    return "".join(parts)


def _times(fn, n):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def laya_section(model, use_daemon, n, snap):
    sec = {"mode": "daemon" if use_daemon else "in-process"}
    if use_daemon:
        from .daemon import DaemonClient
        client = DaemonClient()
        t0 = time.perf_counter()
        if client.start():
            sec["daemon_startup_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()
        client._call({"op": "load", "model": model}, client.load_timeout)
        sec["load_ms"] = round((time.perf_counter() - t0) * 1000, 1)  # ~0 when already loaded
        client.loaded.add(model)
        st = client.status(model)
        sec["daemon"] = {k: st.get(k) for k in ("pid", "uptime_s", "requests", "models")}
        predictor = DaemonDecisionClient(model, client)
    else:
        t0 = time.perf_counter()
        load_laya(model)
        sec["load_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        predictor = InProcessDecisionClient(model)
    run = lambda: decide(predictor, GOAL, snap, [], DONE_Q)
    sec["first_ms"] = round(_times(run, 1)[0], 1)
    sec["warm_ms"] = summarize(_times(run, n))
    return sec, predictor


def device_section(device, n):
    sec = {}
    sec["adb_shell_ms"] = summarize(_times(lambda: device.adb.shell("true"), n))
    sec["probe_ms"] = summarize(_times(device.probe, n))
    k = max(3, n // 4)
    sec["observe_ms"] = summarize(_times(device.observe, k))
    snap = device.last_snapshot
    sec["screen"] = {"package": snap.package, "elements": len(snap.elements), "secure": snap.secure}
    return sec


def step_section(device, predictor, n):
    """Full safe steps: nothing on screen changes (the key event is KEYCODE_UNKNOWN)."""
    m = Metrics()
    pred = Metered(predictor, m)
    gate = PolicyGate()
    snap = None
    saved, device.adb.metrics = device.adb.metrics, m  # count this section's adb calls
    for _ in range(n):
        m.start_step()
        with m.phase("observe"):
            snap = device.observe() if snap is None else snap
        with m.phase("decision"):
            d = decide(pred, GOAL, snap, [], DONE_Q)
        with m.phase("policy"):
            gate.evaluate(d["operation"], snap.element(d["target"]) if "target" in d else None, snap)
        with m.phase("execute"):
            device.key("UNKNOWN")
        snap, _ = after_action(device, "KEY_UNKNOWN", snap, m)
        m.end_step()
    device.adb.metrics = saved
    totals = [s["timing_ms"]["total"] for s in m.steps]
    sec = {"step_ms": summarize(totals)}
    for p in PHASES:
        sec[p + "_ms"] = summarize([s["timing_ms"].get(p, 0.0) for s in m.steps])
    sec["adb_calls_per_step"] = round(sum(s["adb_calls"] for s in m.steps) / len(m.steps), 2)
    sec["laya_calls_per_step"] = round(sum(s["laya_calls"] for s in m.steps) / len(m.steps), 2)
    sec["dumps_per_step"] = round(sum(s["dumps"] for s in m.steps) / len(m.steps), 2)
    sec["probes_per_step"] = round(sum(s["probes"] for s in m.steps) / len(m.steps), 2)
    return sec


def run_benchmark(iterations=20, model="ml", use_daemon=True, device=None, step=False, emit=print):
    n = max(1, iterations)
    report = {"iterations": n, "model": model}
    xml = synthetic_xml()
    snap = snapshot_from_xml(xml)
    report["parse_ms"] = summarize(_times(lambda: snapshot_from_xml(xml), n))
    emit("Snapshot parse + fingerprint (synthetic 12-row screen): %s" % fmt(report["parse_ms"]))
    report["laya"], predictor = laya_section(model, use_daemon, n, snap)
    lay = report["laya"]
    emit("Laya (%s, model %s):" % (lay["mode"], model))
    if "daemon_startup_ms" in lay:
        emit("  daemon startup  %.0f ms" % lay["daemon_startup_ms"])
    emit("  load            %.0f ms%s" % (lay["load_ms"], "  (already loaded)" if lay["load_ms"] < 50 else ""))
    emit("  first decision  %.0f ms" % lay["first_ms"])
    emit("  warm decision   %s" % fmt(lay["warm_ms"]))
    if device is not None:
        report["device"] = device_section(device, n)
        dv = report["device"]
        emit("Device %s (%s):" % (device.serial, device.info.model or "?"))
        emit("  adb shell round trip   %s" % fmt(dv["adb_shell_ms"]))
        emit("  screen probe (light)   %s" % fmt(dv["probe_ms"]))
        emit("  full observation       %s" % fmt(dv["observe_ms"]))
        if step:
            report["step"] = step_section(device, predictor, max(3, n // 4))
            st = report["step"]
            emit("Full safe step (observe, decide, gate, no-op key, settle, observe):")
            emit("  total     %s" % fmt(st["step_ms"]))
            for p in PHASES:
                emit("  %-9s %s" % (p, fmt(st[p + "_ms"])))
            emit("  adb calls/step %.2f (dumps %.2f, probes %.2f)  laya calls/step %.2f"
                 % (st["adb_calls_per_step"], st["dumps_per_step"], st["probes_per_step"], st["laya_calls_per_step"]))
    else:
        emit("Device: not measured (no device)")
    return report


def fmt(s):
    if not s.get("n"):
        return "-"
    return "p50 %.0f ms  p95 %.0f ms  (n=%d)" % (s["p50"], s["p95"], s["n"])

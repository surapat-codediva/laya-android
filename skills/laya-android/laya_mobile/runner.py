"""The autonomous loop: observe -> decide (Laya) -> policy gate -> act -> settle, until done or handoff.

`device` needs observe(signature=None), probe() and the executor's tap/swipe/key; `predictor`
needs Laya's predict(). Both are injected, so the loop runs in tests against saved XML and a
fake model.

Per step: one Laya request; the observation the next decision uses is the one taken when the
previous action settled (never dumped twice); a tap re-dumps the screen first only on the safe
path (freshness.fast_path_blocker). Each step records its phase timings and adb/Laya call counts.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .checks import unmet
from .config import SettleConfig
from .decision import confident, decide, summary
from .elements import describe
from .executor import execute
from .freshness import ProgressTracker, fast_path_blocker
from .metrics import Metered, Metrics, format_step
from .observe import resolve
from .policy import PolicyGate
from .settle import settle
from .trajectory import action

DONE, HANDOFF = 0, 3
STUCK = "repeated_action_without_progress"
log = logging.getLogger("laya_mobile")


@dataclass
class RunResult:
    code: int
    reason: str
    fingerprint: str
    steps: List[dict] = field(default_factory=list)
    policy: Optional[dict] = None
    trajectory: Optional[str] = None
    timing: Optional[dict] = None

    @property
    def result(self):
        return "done" if self.code == DONE else "handoff"

    def to_dict(self):
        d = {"result": self.result, "reason": self.reason, "fingerprint": self.fingerprint, "steps": self.steps}
        if self.policy:
            d["policy"] = self.policy
        if self.trajectory:
            d["trajectory"] = self.trajectory
        if self.timing:
            d["timing"] = self.timing
        return d


def action_text(op, el=None):
    return "%s %s" % (op, describe(el, full=False)) if el else op


def after_action(device, operation, before, metrics, wait=None, settle_cfg=None, sleep=time.sleep,
                 clock=time.monotonic):
    """Wait for the screen to settle after `operation`, then take the next full observation.
    wait=None: adaptive (poll the cheap signature); a number: a fixed wait (explicit WAIT).
    Returns (snapshot, settle info dict)."""
    if wait is not None:
        with metrics.phase("settle"):
            sleep(wait)
        with metrics.phase("observe"):
            return device.observe(), {"status": "fixed_wait", "ms": round(wait * 1000, 1)}
    with metrics.phase("settle"):
        res = settle(device.probe, before.signature if before else None, operation, settle_cfg, clock, sleep)
    with metrics.phase("observe"):
        # A settled signature is reused as the snapshot's own: no second screencap.
        snap = device.observe(signature=res.signature if res.settled else None)
    if before is not None and snap.signature and snap.signature == before.signature \
            and snap.fingerprint != before.fingerprint:
        # The pixels were hashed before a change the dump then saw (a slow app reacting after
        # the settle gave up waiting): that signature does not describe this snapshot.
        snap.signature = None
    return snap, res.to_dict()


def run_goal(device, predictor, session, goal, cond=None, done_q=None, max_steps=6, min_confidence=0.6, yes=0.8,
             wait=None, limit=20, gate=None, sleep=time.sleep, settle_cfg=None, metrics=None, clock=time.monotonic):
    """With `cond` (exact on-screen conditions) the goal is reached only when the screen meets
    them and Laya's p_done is just logged; without, Laya's done check decides."""
    gate = gate or PolicyGate()
    m = metrics or Metrics()
    predictor = Metered(predictor, m)
    settle_cfg = settle_cfg or SettleConfig.from_env()
    progress = ProgressTracker()
    done_q = done_q or "Has this goal been reached on the current screen: %s?" % goal
    rec = session.recorder()
    steps = []

    def record(base, **kw):
        if rec:
            rec.step(**dict(base, **kw))

    def close(entry):
        """End the current step's timing and attach it to its output entry."""
        t = m.end_step()
        entry.update(t)
        for line in format_step(t).splitlines():
            log.info("  %s", line)
        return dict(t)

    def stop(code, reason, snap, policy=None):
        session.show(snap)
        session.save()
        timing = m.summary()
        if rec:
            rec.end("done" if code == DONE else "handoff", reason, metrics=timing)
        log.info("stop: %s (%s)", "done" if code == DONE else "handoff", reason)
        return RunResult(code, reason, snap.fingerprint, steps, policy, session.trajectory, timing)

    m.start_step()
    with m.phase("observe"):
        snap = device.observe()
    for step in range(1, max_steps + 2):
        if step > 1:
            m.start_step()
        miss = unmet(snap, cond) if cond else None
        if cond and not miss:
            m.discard_step()  # nothing was decided: not a step
            return stop(DONE, "goal verified on screen", snap)
        if step > max_steps:
            m.discard_step()
            break
        with m.phase("decision"):
            d = decide(predictor, goal, snap, session.history, done_q, limit)
        session.show(snap)
        session.laya = d
        el = snap.element(d["target"]) if d["operation"] == "CLICK" else None
        entry = dict(summary(d, snap), step=step)
        steps.append(entry)
        log.info("step %d: fingerprint=%s candidates=%d op=%s (%.3f) target=%s (%s) p_done=%s", step,
                 snap.fingerprint, len(d["candidates"]), d["operation"], d["op_confidence"],
                 describe(el) if el else "-", d.get("target_confidence", "-"), d.get("p_done"))
        n = session.next_step()
        base = dict(step=n, actor="laya", before=snap, history=list(session.history), model_decision=d)

        if not cond and d["p_done"] >= yes:
            record(base, outcome={"done": True}, note="done check", **close(entry))
            return stop(DONE, "goal reached (p=%.3f)" % d["p_done"], snap)
        if d["operation"] == "DONE" and cond:
            reason = "Laya chose DONE but the screen does not meet the goal: " + "; ".join(miss)
        elif d["operation"] == "DONE":
            reason = "Laya chose DONE but the done check is unsure (p=%.3f)" % d["p_done"]
        elif not confident(d, min_confidence):
            reason = "low confidence on step %d" % step
        else:
            reason = None
        if reason:
            record(base, outcome={"handoff": reason}, **close(entry))
            return stop(HANDOFF, reason, snap)

        with m.phase("policy"):
            policy = gate.evaluate(d["operation"], el, snap)
        log.info("policy: %s %s", policy.action, policy.reason or "")
        entry["policy"] = policy.to_dict()
        if not policy.allowed:
            record(base, policy=policy, outcome={"handoff": policy.reason}, **close(entry))
            return stop(HANDOFF, policy.reason, snap, policy.to_dict())
        if progress.stuck(snap, d["operation"], el):
            # Faster loops repeat faster: the same action on the same screen again is a loop.
            log.info("stuck: %s repeated on the same screen", action_text(d["operation"], el))
            entry["stuck"] = True
            record(base, policy=policy, outcome={"handoff": STUCK}, **close(entry))
            return stop(HANDOFF, STUCK, snap)

        before = snap
        if el:
            # Laya decided on `snap`; tap only if that element is still there.
            why = fast_path_blocker(snap, el, policy)
            if why is None:
                with m.phase("observe"):
                    why = None if device.probe() == snap.signature else "screen_changed"
            entry["path"] = "fast" if why is None else "safe"
            if why:
                entry["safe_reason"] = why
                with m.phase("observe"):
                    before = device.observe()
                cur = resolve(el, snap, before)
                if cur is None:
                    log.info("stale target %s; re-deciding", describe(el))
                    entry["stale"] = True
                    record(base, policy=policy, after=before, outcome={"stale": True}, **close(entry))
                    snap = before
                    continue
                el = cur
            log.info("path: %s%s", entry["path"], " (%s)" % why if why else "")
        progress.record(snap, d["operation"], el)
        with m.phase("execute"):
            execute(device, d["operation"], el)
        print("step %d: %s (%dms)" % (step, action_text(d["operation"], el), d["ms"]), file=sys.stderr)
        session.history.append(action_text(d["operation"], el))
        snap, settled = after_action(device, d["operation"], before, m, wait, settle_cfg, sleep, clock)
        entry["settle"] = settled
        changed = snap.fingerprint != before.fingerprint
        log.info("settle: %s after %d polls; screen changed: %s (%s -> %s)", settled["status"],
                 settled.get("polls", 0), changed, before.fingerprint, snap.fingerprint)
        record(base, before=before, policy=policy, executed=dict(action(d["operation"], el), success=True),
               after=snap, outcome={"screen_changed": changed}, settle=settled, **close(entry))
        if not changed:
            return stop(HANDOFF, "screen did not change after %s" % action_text(d["operation"], el), snap)
    return stop(HANDOFF, "max steps reached", snap)

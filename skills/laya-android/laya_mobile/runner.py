"""The autonomous loop: observe -> decide (Laya) -> policy gate -> act, until done or handoff.

`device` needs observe() and the executor's tap/swipe/key; `predictor` needs Laya's predict().
Both are injected, so the loop runs in tests against saved XML and a fake model.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .checks import unmet
from .decision import confident, decide, summary
from .elements import describe
from .executor import execute
from .observe import resolve
from .policy import PolicyGate
from .trajectory import action

DONE, HANDOFF = 0, 3
log = logging.getLogger("laya_mobile")


@dataclass
class RunResult:
    code: int
    reason: str
    fingerprint: str
    steps: List[dict] = field(default_factory=list)
    policy: Optional[dict] = None
    trajectory: Optional[str] = None

    @property
    def result(self):
        return "done" if self.code == DONE else "handoff"

    def to_dict(self):
        d = {"result": self.result, "reason": self.reason, "fingerprint": self.fingerprint, "steps": self.steps}
        if self.policy:
            d["policy"] = self.policy
        if self.trajectory:
            d["trajectory"] = self.trajectory
        return d


def action_text(op, el=None):
    return "%s %s" % (op, describe(el, full=False)) if el else op


def run_goal(device, predictor, session, goal, cond=None, done_q=None, max_steps=6, min_confidence=0.6, yes=0.8,
             wait=1.5, limit=20, gate=None, sleep=time.sleep):
    """With `cond` (exact on-screen conditions) the goal is reached only when the screen meets
    them and Laya's p_done is just logged; without, Laya's done check decides."""
    gate = gate or PolicyGate()
    done_q = done_q or "Has this goal been reached on the current screen: %s?" % goal
    rec = session.recorder()
    steps = []

    def record(base, **kw):
        if rec:
            rec.step(**dict(base, **kw))

    def stop(code, reason, snap, policy=None):
        session.show(snap)
        session.save()
        if rec:
            rec.end("done" if code == DONE else "handoff", reason)
        log.info("stop: %s (%s)", "done" if code == DONE else "handoff", reason)
        return RunResult(code, reason, snap.fingerprint, steps, policy, session.trajectory)

    snap = device.observe()
    for step in range(1, max_steps + 2):
        miss = unmet(snap, cond) if cond else None
        if cond and not miss:
            return stop(DONE, "goal verified on screen", snap)
        if step > max_steps:
            break
        d = decide(predictor, goal, snap, session.history, done_q, limit)
        session.show(snap)
        session.laya = d
        el = snap.element(d["target"]) if d["operation"] == "CLICK" else None
        steps.append(dict(summary(d, snap), step=step))
        log.info("step %d: fingerprint=%s candidates=%d op=%s (%.3f) target=%s (%s) p_done=%s", step,
                 snap.fingerprint, len(d["candidates"]), d["operation"], d["op_confidence"],
                 describe(el) if el else "-", d.get("target_confidence", "-"), d.get("p_done"))
        n = session.next_step()
        base = dict(step=n, actor="laya", before=snap, history=list(session.history), model_decision=d)

        if not cond and d["p_done"] >= yes:
            record(base, outcome={"done": True}, note="done check")
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
            record(base, outcome={"handoff": reason})
            return stop(HANDOFF, reason, snap)

        policy = gate.evaluate(d["operation"], el, snap)
        log.info("policy: %s %s", policy.action, policy.reason or "")
        steps[-1]["policy"] = policy.to_dict()
        if not policy.allowed:
            record(base, policy=policy, outcome={"handoff": policy.reason})
            return stop(HANDOFF, policy.reason, snap, policy.to_dict())

        before = snap
        if el:
            # Laya decided on `snap`; tap only if that element is still there.
            before = device.observe()
            cur = resolve(el, snap, before)
            if cur is None:
                log.info("stale target %s; re-deciding", describe(el))
                record(base, policy=policy, after=before, outcome={"stale": True})
                steps[-1]["stale"] = True
                snap = before
                continue
            el = cur
        execute(device, d["operation"], el)
        print("step %d: %s (%dms)" % (step, action_text(d["operation"], el), d["ms"]), file=sys.stderr)
        session.history.append(action_text(d["operation"], el))
        sleep(wait)
        snap = device.observe()
        changed = snap.fingerprint != before.fingerprint
        log.info("screen changed: %s (%s -> %s)", changed, before.fingerprint, snap.fingerprint)
        record(base, before=before, policy=policy, executed=dict(action(d["operation"], el), success=True),
               after=snap, outcome={"screen_changed": changed})
        if not changed:
            return stop(HANDOFF, "screen did not change after %s" % action_text(d["operation"], el), snap)
    return stop(HANDOFF, "max steps reached", snap)

"""Adaptive UI settling: after an action, poll a cheap screen signature until it holds still,
instead of sleeping a fixed time. Then one full observation is taken.

  before=A   A A B C C      -> "stable" on C (a spinner or intermediate screen B is waited out)
  before=A   A A A A ...    -> "unchanged" once the action's grace period is over
  before=A   B C D E F ...  -> "dynamic" at the timeout (video, animation): observe anyway

The signature is the probe's (Device.probe); this module only sees opaque strings, so it is
tested with plain sequences.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import SettleConfig, action_settle

STABLE, UNCHANGED, DYNAMIC = "stable", "unchanged", "dynamic"


@dataclass
class SettleResult:
    status: str  # stable | unchanged | dynamic
    signature: Optional[str]  # the last probe's signature
    changed: bool  # the signature differed from `before` at some point
    polls: int
    ms: float
    probe_ms: float = 0.0  # average duration of one probe


    @property
    def settled(self):
        """The screen held still, so the last signature describes what a dump will see."""
        return self.status in (STABLE, UNCHANGED)

    def to_dict(self):
        return {"status": self.status, "changed": self.changed, "polls": self.polls, "ms": round(self.ms, 1),
                "probe_ms": round(self.probe_ms, 1)}


def settle(probe, before, operation, cfg=None, clock=time.monotonic, sleep=time.sleep):
    """Poll `probe()` until the screen is stable after `operation`.

    before     the signature the action started from (None: unknown, so only stability counts)
    operation  CLICK, BACK, SCROLL_DOWN, TYPE ... picks the expectations (config.ACTION_SETTLE)
    """
    cfg = cfg or SettleConfig()
    expect = action_settle(operation)
    t0 = clock()
    last, since, same, polls = object(), t0, 0, 0
    changed = False
    probing = 0.0
    while True:
        p0 = clock()
        sig = probe()
        now = clock()
        polls += 1
        probing += now - p0
        if sig != last:
            last, since, same = sig, now, 1
        else:
            same += 1
        if before is None or sig != before:
            changed = True  # an unreadable signature (None) counts as a change: wait for stability
        elapsed = (now - t0) * 1000
        held = same >= cfg.min_samples and (now - since) * 1000 >= cfg.stable_ms
        if held and changed:
            return SettleResult(STABLE, sig, True, polls, elapsed, probing * 1000 / polls)
        if held and (not expect.expect_change or elapsed >= expect.change_grace_ms):
            return SettleResult(UNCHANGED, sig, False, polls, elapsed, probing * 1000 / polls)
        if elapsed >= cfg.timeout_ms:
            # Never held still: an animation, a video, a ticking counter. Observe anyway; the
            # caller treats the result as unsettled (no fast path on it).
            return SettleResult(DYNAMIC if changed else UNCHANGED, sig, changed, polls, elapsed, probing * 1000 / polls)
        sleep(cfg.poll_ms / 1000.0)

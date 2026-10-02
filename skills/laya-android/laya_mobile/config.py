"""Performance knobs in one place: settle timing, timeouts, fast-path limits.

Only four are environment variables (LAYA_ANDROID_SETTLE_POLL_MS, _SETTLE_STABLE_MS,
_SETTLE_TIMEOUT_MS, _DAEMON_TIMEOUT); the rest are constants tuned on an emulator and a phone.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import UsageError

# Every external operation is bounded.
ADB_TIMEOUT_S = 30  # one adb call; a uiautomator dump waits up to 10 s for an idle UI by itself
DAEMON_START_TIMEOUT_S = 20  # daemon process accepting connections (imports only, no model)
MODEL_LOAD_TIMEOUT_S = 900  # first load may download the ~650 MB checkpoint
DAEMON_IDLE_EXIT_S = 1800  # an unused daemon exits and frees the model's memory
# A snapshot whose screen signature still matches may be tapped without a new dump; this caps
# how old it may be even then.
FAST_PATH_MAX_AGE_S = 120


def _env_num(name, default, cast=int):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise UsageError("%s must be a number, got %r" % (name, raw)) from None
    if v <= 0:
        raise UsageError("%s must be positive, got %r" % (name, raw))
    return v


def daemon_timeout():
    """Seconds one Laya daemon request may take once the model is loaded."""
    return _env_num("LAYA_ANDROID_DAEMON_TIMEOUT", 30.0, float)


@dataclass
class SettleConfig:
    """Adaptive settle after an action: poll a cheap screen signature until it holds still.

    poll_ms     pause between probes (a probe itself takes ~150 ms over adb, ~500 ms while the
                device is busy animating, so probes are ~225 ms apart at best)
    stable_ms   the signature must stay the same this long, over at least `min_samples` probes;
                with the defaults two equal probes in a row are enough
    timeout_ms  give up waiting and observe anyway (animated or video screens never hold still)
    """
    poll_ms: int = 75
    stable_ms: int = 150
    timeout_ms: int = 2500
    min_samples: int = 2

    @classmethod
    def from_env(cls):
        return cls(poll_ms=_env_num("LAYA_ANDROID_SETTLE_POLL_MS", cls.poll_ms),
                   stable_ms=_env_num("LAYA_ANDROID_SETTLE_STABLE_MS", cls.stable_ms),
                   timeout_ms=_env_num("LAYA_ANDROID_SETTLE_TIMEOUT_MS", cls.timeout_ms))


@dataclass(frozen=True)
class ActionSettle:
    """How one kind of action is expected to change the screen.

    expect_change  a visible change is the normal outcome, so an unchanged screen is waited on
                   for up to `change_grace_ms` before settling as "unchanged"
    """
    expect_change: bool
    change_grace_ms: int


# Tapping, going back and scrolling normally change the screen; a tap may also just focus a
# field or do nothing visible, so "unchanged" is a result, not an error. 1.5 s is as patient as
# the fixed wait it replaces: a busy emulator was seen starting a transition ~2 s after a tap. Typing changes the
# field's text quickly. Unknown keys (ENTER, TAB ...) may or may not change anything.
ACTION_SETTLE = {
    "CLICK": ActionSettle(True, 1500),
    "BACK": ActionSettle(True, 1500),
    "HOME": ActionSettle(True, 1500),
    "SCROLL": ActionSettle(True, 800),
    "TYPE": ActionSettle(True, 600),
    "KEY": ActionSettle(False, 0),
}


def action_settle(operation):
    op = (operation or "").upper()
    if op.startswith(("SCROLL", "SWIPE")):
        op = "SCROLL"
    elif op == "TYPE_TEXT":
        op = "TYPE"
    elif op.startswith("KEY_"):
        op = "KEY"
    return ACTION_SETTLE.get(op, ACTION_SETTLE["KEY"])

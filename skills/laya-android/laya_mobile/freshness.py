"""When may a decision taken on a snapshot be executed without a new dump, and is the run stuck?

Fast path: the snapshot's screen signature still matches a cheap probe (~150 ms), the target
is unambiguous and nothing about it or its screen is sensitive -> tap it directly.
Safe path: anything else -> a full re-observation and resolve() by stable key, as before.
Speed never skips the policy gate, identity checks or handoffs; it only skips a redundant dump.
"""
from __future__ import annotations

import hashlib
from collections import Counter

from .config import FAST_PATH_MAX_AGE_S
from .policy import PERMISSION_PACKAGES, SENSITIVE, matches

# Screen text that makes any tap on it worth a fresh look (a "Cancel" on "Delete 3 photos?").
CONTEXT_CATEGORIES = ("factory_reset", "account_removal", "payment", "money_transfer", "delete", "credential")
MAX_REPEATS = 2  # the same action on the same screen structure; the next one hands off


def fast_path_blocker(snapshot, el, policy):
    """None when `el` may take the fast path; otherwise the reason the safe path is required."""
    if snapshot is None or el is None:
        return "no_target"
    if snapshot.secure:
        return "secure_window"
    if not snapshot.signature:
        return "no_signature"
    if snapshot.age_ms > FAST_PATH_MAX_AGE_S * 1000:
        return "old_snapshot"
    if policy is None or not policy.allowed or policy.matched:
        return "sensitive_target"
    if el.password or any(e.password for e in snapshot.elements):
        return "credential_screen"
    if snapshot.package in PERMISSION_PACKAGES:
        return "permission_dialog"
    if sum(e.stable_key == el.stable_key for e in snapshot.elements) != 1:
        return "ambiguous_identity"
    for cat in CONTEXT_CATEGORIES:
        if matches(snapshot.visible_text, SENSITIVE[cat]):
            return "sensitive_context"
    return None


def structure_key(snapshot):
    """What a screen is made of: app, activity and the set of element identities -- not their
    state or the screen's other text, so a ticking counter or a toggled switch is the same screen."""
    blob = "\x1f".join([snapshot.package or "", snapshot.activity or ""] +
                       sorted({e.stable_key for e in snapshot.elements}))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


class ProgressTracker:
    """Detects an action repeated on the same screen without getting anywhere:
    X -CLICK 3-> Y -BACK-> X -CLICK 3-> Y -BACK-> X -CLICK 3  (stops before the third tap)."""

    def __init__(self, max_repeats=MAX_REPEATS):
        self.max_repeats = max_repeats
        self.seen = Counter()

    @staticmethod
    def key(snapshot, operation, el=None):
        return structure_key(snapshot), operation, el.stable_key if el is not None else None

    def repeats(self, snapshot, operation, el=None):
        return self.seen[self.key(snapshot, operation, el)]

    def stuck(self, snapshot, operation, el=None):
        return self.repeats(snapshot, operation, el) >= self.max_repeats

    def record(self, snapshot, operation, el=None):
        self.seen[self.key(snapshot, operation, el)] += 1

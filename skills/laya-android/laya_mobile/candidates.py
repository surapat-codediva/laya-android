"""Operation-aware candidate pruning: which elements a decision about `operation` may pick.

Rules and a small additive score -- no model. Deterministic: ties break on element id.
"""
from __future__ import annotations

import re

from .elements import SYSTEMUI, norm, rid_tail

ROLE_SCORE = {"button": 3, "switch": 3, "checkbox": 3, "radio": 3, "input": 2, "item": 2, "dropdown": 2,
              "text": 1, "image": 1, "list": 1, "scroll": 1}
STOPWORDS = {"the", "a", "an", "to", "of", "in", "on", "and", "or", "for", "open", "go", "tap", "click",
             "press", "screen", "page", "app", "my", "it", "is", "with", "turn", "set"}


def eligible(el, operation):
    op = operation.upper()
    if op == "CLICK":
        return (el.clickable or el.long_clickable or el.checkable) and not (el.scrollable and not el.clickable)
    if op in ("TYPE", "TYPE_TEXT"):
        return el.editable
    if op.startswith("SCROLL") or op.startswith("SWIPE"):
        return el.scrollable
    return False


def goal_terms(goal):
    return [t for t in re.findall(r"\w+", norm(goal)) if len(t) > 1 and t not in STOPWORDS]


def overlap(el, goal):
    """Goal words found in the element's label/context, plus label parts found in the goal
    (Thai has no spaces between words, so "เปิดบลูทูธ" is one token that contains "บลูทูธ")."""
    if not goal:
        return 0
    hay = norm(" ".join([el.label, el.context, rid_tail(el.resource_id).replace("_", " ")]))
    g = norm(goal)
    hits = sum(1 for t in goal_terms(goal) if t in hay)
    hits += sum(1 for p in el.label.split(" | ") if len(norm(p)) > 1 and norm(p) in g)
    return min(hits, 2)


def score(el, snapshot, goal=None):
    s = ROLE_SCORE.get(el.role, 0)
    if norm(el.label) not in (norm(rid_tail(el.resource_id)), el.role):
        s += 2  # a real name, not a resource-id fallback
    if el.resource_id:
        s += 1
    if not el.enabled:
        s -= 4
    if el.bounds[2] <= 0 or el.bounds[3] <= 0:
        s -= 2  # off screen
    if el.package == SYSTEMUI and snapshot.package != SYSTEMUI:
        s -= 3  # status bar, while an app is in front
    return s + 3 * overlap(el, goal)


def candidates_for_operation(snapshot, operation, limit=20, goal=None):
    """Elements a decision about `operation` may target, best first, at most `limit`."""
    pool = [e for e in snapshot.elements if eligible(e, operation)]
    ranked = sorted(pool, key=lambda e: (-score(e, snapshot, goal), e.id))
    return ranked[:limit]

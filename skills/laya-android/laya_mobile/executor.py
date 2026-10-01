"""Turn an operation into device input."""
from __future__ import annotations

from .errors import UsageError

SWIPE_FOR = {"SCROLL_DOWN": "up", "SCROLL_UP": "down", "SWIPE_LEFT": "left", "SWIPE_RIGHT": "right"}


def execute(device, operation, el=None, text=None):
    op = operation.upper()
    if op == "CLICK":
        device.tap(*el.center)
    elif op in SWIPE_FOR:
        device.swipe(SWIPE_FOR[op])
    elif op in ("BACK", "HOME"):
        device.key(op)
    elif op in ("TYPE", "TYPE_TEXT"):
        device.type_text(text)
    elif op != "DONE":
        raise UsageError("unknown operation %s" % operation)

"""Screen state: MobileElement and MobileSnapshot. Both round-trip through plain JSON dicts."""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MobileElement:
    id: int  # position in this snapshot; only valid for this snapshot
    stable_key: str  # same logical element across snapshots (see elements.stable_key)
    cls: str  # raw Android class, serialized as "class"
    role: str  # normalized: button, switch, input, list, item, ...
    label: str
    bounds: List[int]
    text: str = ""
    content_desc: str = ""
    hint: str = ""
    resource_id: str = ""
    package: str = ""
    context: str = ""  # nearby text that tells repeated elements apart
    clickable: bool = False
    long_clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    checkable: bool = False
    enabled: bool = True
    checked: bool = False
    selected: bool = False
    focused: bool = False
    password: bool = False

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def to_dict(self):
        d = dataclasses.asdict(self)
        d["class"] = d.pop("cls")
        x, y = self.center
        d["center"] = {"x": x, "y": y}
        return d

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        kw["cls"] = d.get("class", d.get("cls", ""))
        return cls(**kw)


@dataclass
class MobileSnapshot:
    package: Optional[str]
    activity: Optional[str]
    visible_text: str
    elements: List[MobileElement] = field(default_factory=list)
    fingerprint: str = ""
    timestamp: float = 0.0  # wall clock (time.time) of the dump
    # Freshness: the cheap screen signature taken just before the dump (None when unknown or the
    # screen is a secure window, whose screenshots are black), and the display rotation.
    signature: Optional[str] = None
    secure: bool = False
    rotation: int = 0

    def element(self, id):
        return self.elements[id] if 0 <= id < len(self.elements) else None

    @property
    def age_ms(self):
        return max(0.0, (time.time() - self.timestamp) * 1000) if self.timestamp else float("inf")

    def to_dict(self):
        return {"package": self.package, "activity": self.activity, "fingerprint": self.fingerprint,
                "timestamp": self.timestamp, "visible_text": self.visible_text,
                "elements": [e.to_dict() for e in self.elements],
                "signature": self.signature, "secure": self.secure, "rotation": self.rotation}

    @classmethod
    def from_dict(cls, d):
        return cls(package=d.get("package"), activity=d.get("activity"), visible_text=d.get("visible_text", ""),
                   elements=[MobileElement.from_dict(e) for e in d.get("elements", [])],
                   fingerprint=d.get("fingerprint", ""), timestamp=d.get("timestamp", 0.0),
                   signature=d.get("signature"), secure=d.get("secure", False), rotation=d.get("rotation", 0))

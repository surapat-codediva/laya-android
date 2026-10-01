"""Per-device CLI state between invocations: the last snapshot shown (so `tap N` refers to it),
the goal, the action history, Laya's last decision and where the run is recorded, if anywhere."""
from __future__ import annotations

import json
import os
import re
import tempfile

from .models import MobileSnapshot
from .trajectory import TrajectoryRecorder, new_run_id


def path(serial):
    return os.path.join(tempfile.gettempdir(), "laya-android-%s.json" % re.sub(r"[^\w.-]", "_", serial or "default"))


class Session:
    def __init__(self, serial, data=None):
        self.serial = serial
        d = data or {}
        self.id = d.get("id") or new_run_id()
        self.goal = d.get("goal")
        self.history = d.get("history", [])
        self.obs = MobileSnapshot.from_dict(d["obs"]) if d.get("obs") else None
        self.acted = d.get("acted", False)  # element numbers of `obs` no longer apply
        self.laya = d.get("laya")
        self.trajectory = d.get("trajectory")  # JSONL path when recording
        self.step = d.get("step", 0)

    @classmethod
    def load(cls, serial):
        try:
            with open(path(serial), encoding="utf-8") as f:
                return cls(serial, json.load(f))
        except (OSError, ValueError, KeyError, TypeError):
            return cls(serial)

    def save(self):
        p = path(self.serial)
        d = {"id": self.id, "goal": self.goal, "history": self.history,
             "obs": self.obs.to_dict() if self.obs else None, "acted": self.acted, "laya": self.laya,
             "trajectory": self.trajectory, "step": self.step}
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)

    def start(self, goal, record=False, trajectory=None, force=False):
        """Start a goal (a new run) unless this goal is already the current one."""
        if force or self.goal != goal:
            self.id, self.goal, self.history, self.laya, self.step, self.trajectory = (
                new_run_id(), goal, [], None, 0, None)
        if trajectory or (record and not self.trajectory):
            self.trajectory = TrajectoryRecorder.create(goal, trajectory, self.id).path
            self.recorder().start(serial=self.serial)
        return self

    def finish(self):
        self.id, self.goal, self.history, self.laya, self.step, self.trajectory = new_run_id(), None, [], None, 0, None

    def recorder(self):
        return TrajectoryRecorder(self.trajectory, self.id, self.goal) if self.trajectory and self.goal else None

    def show(self, snapshot):
        self.obs, self.acted = snapshot, False

    def current(self):
        """The snapshot element numbers refer to, or None after an action."""
        return None if self.acted else self.obs

    def next_step(self):
        self.step += 1
        return self.step

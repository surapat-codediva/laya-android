"""Versioned JSONL trajectories: debugging, replay, benchmarks and Mobile-Laya fine-tuning data.

One file per run. Every line has `version`, `run_id`, `event`, `timestamp`, `goal`. Events:
  start  -- once, run options
  step   -- before snapshot, model_decision (Laya), teacher_action (host override), executed,
            policy, after fingerprint, outcome
  end    -- result and reason
Secrets are never written: password fields carry no text, and typed text is "[REDACTED]" when
the target is a password/PIN/OTP field or the text looks like a PIN or code. New optional
fields (e.g. before.screenshot) can be added without bumping `version`; renames bump it.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import uuid

from .policy import SENSITIVE, matches

SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"
CODE_LIKE = re.compile(r"^\s*\d{4,8}\s*$")  # PIN, OTP, verification code
log = logging.getLogger("laya_mobile")


def data_dir():
    return os.environ.get("LAYA_ANDROID_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".laya-android")


def new_run_id():
    return uuid.uuid4().hex[:12]


def default_path(run_id, now=None):
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime(now))
    return os.path.join(data_dir(), "trajectories", "%s-%s.jsonl" % (stamp, run_id))


def redact_snapshot(snapshot):
    """Snapshot dict safe to store: password fields lose their text."""
    if snapshot is None:
        return None
    d = copy.deepcopy(snapshot if isinstance(snapshot, dict) else snapshot.to_dict())
    for e in d.get("elements", []):
        if e.get("password") and e.get("text"):
            e["text"] = REDACTED
    return d


def sensitive_text(text, target=None, snapshot=None):
    """Would storing this typed text leak a secret? Unknown means yes: without a known target,
    only a screen known to have no password field lets the text through."""
    if CODE_LIKE.match(text or ""):
        return True
    if target is None:
        return snapshot is None or any(e.password for e in snapshot.elements)
    about = " | ".join([target.label, target.hint, target.content_desc, target.resource_id, target.context])
    return target.password or bool(matches(about, SENSITIVE["credential"]))


def typed(text, target=None, snapshot=None):
    """The `text` part of a TYPE action record."""
    return {"text": REDACTED if sensitive_text(text, target, snapshot) else text, "chars": len(text)}


def element_ref(el):
    if el is None:
        return {}
    return {"target_id": el.id, "target_stable_key": el.stable_key, "target_label": el.label}


def action(operation, el=None, **extra):
    """An action as stored in model_decision / teacher_action / executed."""
    return dict({"operation": operation}, **element_ref(el), **extra)


def model_decision_record(d):
    """Laya's decision dict (decision.decide) as stored: what it chose and how sure it was."""
    if not d:
        return None
    out = {"operation": d["operation"], "confidence": d["op_confidence"], "fingerprint": d.get("fingerprint")}
    for k in ("target", "target_stable_key", "target_confidence", "p_done", "ms", "candidates"):
        if k in d:
            out["target_id" if k == "target" else k] = d[k]
    return out


def step_record(step, actor, before=None, history=(), model_decision=None, teacher_action=None, executed=None,
                policy=None, after=None, outcome=None, note=None):
    """One step. `actor` is "laya" (Laya chose, the run executed) or "agent" (the host chose --
    a teacher label wherever model_decision is present and differs)."""
    rec = {
        "event": "step", "step": step, "actor": actor,
        "before": redact_snapshot(before),
        "history": list(history),
        "model_decision": model_decision_record(model_decision),
        "teacher_action": teacher_action,
        "executed": executed,
        "policy": policy.to_dict() if hasattr(policy, "to_dict") else policy,
        "after": None if after is None else {"fingerprint": after.fingerprint, "package": after.package,
                                             "activity": after.activity},
        "outcome": outcome or {},
    }
    if note:
        rec["note"] = note
    return rec


def correction_record(step, before, model_decision, teacher_action, executed=None, policy=None, after=None,
                      history=(), note=None):
    """For integrations that know the host overrode Laya: model_decision vs teacher_action on `before`."""
    out = {}
    if after is not None and before is not None:
        out["screen_changed"] = after.fingerprint != before.fingerprint
    return step_record(step, "agent", before, history, model_decision, teacher_action, executed, policy, after,
                       out, note)


class TrajectoryRecorder:
    """Appends records to one JSONL file. Write failures warn on stderr and never stop a run."""

    def __init__(self, path, run_id, goal):
        self.path, self.run_id, self.goal = path, run_id, goal

    @classmethod
    def create(cls, goal, path=None, run_id=None):
        run_id = run_id or new_run_id()
        return cls(path or default_path(run_id), run_id, goal)

    def write(self, record):
        rec = dict({"version": SCHEMA_VERSION, "run_id": self.run_id, "timestamp": round(time.time(), 3),
                    "goal": self.goal}, **record)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("trajectory write failed: %s", e)
        return rec

    def start(self, **meta):
        return self.write({"event": "start", "meta": meta})

    def step(self, *args, **kw):
        return self.write(step_record(*args, **kw))

    def end(self, result, reason):
        return self.write({"event": "end", "result": result, "reason": reason})


def read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

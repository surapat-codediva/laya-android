"""A stand-in Laya for daemon tests (loaded in-thread or by a spawned daemon via --loader).

FAKE_LAYA_LOADS=<file>  append each model load to this file (to prove a model loads once)
FAKE_LAYA_DELAY=<s>     sleep this long in every predict (to test timeouts and serialization)
"""
import os
import threading
import time


class FakeLaya:
    lock = threading.Lock()
    active = 0
    max_active = 0

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def predict(self, state, questions):
        with FakeLaya.lock:
            FakeLaya.active += 1
            FakeLaya.max_active = max(FakeLaya.max_active, FakeLaya.active)
        try:
            time.sleep(float(os.environ.get("FAKE_LAYA_DELAY", "0")))
            self.calls += 1
            out = {}
            for key, q in questions.items():
                if q["type"] == "choice":
                    out[key] = {"choice": next(iter(q["criteria"])), "confidence": 0.9}
                else:
                    out[key] = {"noul": 0.25}
            return {"answers": out, "model": self.name}
        finally:
            with FakeLaya.lock:
                FakeLaya.active -= 1


def load(name):
    marker = os.environ.get("FAKE_LAYA_LOADS")
    if marker:
        with open(marker, "a") as f:
            f.write(name + "\n")
    return FakeLaya(name)

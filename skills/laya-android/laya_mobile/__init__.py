"""laya_mobile: Android mobile-use over adb + uiautomator, with Laya as the local decision model.

observe   uiautomator XML -> MobileSnapshot (elements, context, stable keys, fingerprint)
candidates operation-aware pruning of what a decision may target
decision  Laya questions and answers
policy    deterministic safety gate for autonomous actions
trajectory versioned JSONL recording
runner    the observe -> decide -> gate -> act -> settle loop
daemon    the persistent Laya runtime
"""
__version__ = "0.3.0"

from .models import MobileElement, MobileSnapshot  # noqa: F401

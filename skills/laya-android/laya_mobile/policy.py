"""Deterministic safety gate in front of autonomous actions.

Looks at the proposed action and its target (label, text, description, resource id, role,
password flag) -- not at every word on the screen, so a "Delete" button elsewhere does not
stop a tap on "Inbox". A generic confirmation ("OK", "Confirm") is judged by what it
confirms: its context, and on dialog-sized screens the screen text. Laya is never consulted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .elements import norm, rid_tail

ALLOW, HANDOFF, BLOCK = "allow", "handoff", "block"

# category -> (English phrases matched on word boundaries, Thai phrases matched as substrings)
SENSITIVE = {
    "factory_reset": (["factory reset", "factory data reset", "erase all data", "reset phone", "wipe"],
                      ["รีเซ็ตเป็นค่าเริ่มต้น", "ล้างข้อมูลทั้งหมด", "คืนค่าโรงงาน"]),
    "account_removal": (["remove account", "delete account", "close account"], ["ลบบัญชี"]),
    "payment": (["pay", "pay now", "payment", "checkout", "check out", "place order", "confirm order",
                 "confirm purchase", "buy", "buy now", "purchase", "subscribe", "top up", "order now"],
                ["ชำระเงิน", "จ่ายเงิน", "สั่งซื้อ", "ซื้อเลย", "ซื้อ", "ยืนยันคำสั่งซื้อ", "สมัครสมาชิก", "เติมเงิน"]),
    "money_transfer": (["transfer", "send money", "withdraw", "bank transfer"], ["โอนเงิน", "โอน", "ถอนเงิน"]),
    "delete": (["delete", "remove", "erase", "uninstall", "clear data", "clear storage", "discard"],
               ["ลบ", "ถอนการติดตั้ง", "ล้างข้อมูล"]),
    "logout": (["log out", "logout", "sign out", "signout"], ["ออกจากระบบ"]),
    "credential": (["password", "passcode", "pin", "otp", "verification code", "one-time code", "security code",
                    "cvv"], ["รหัสผ่าน", "รหัส pin", "รหัส otp", "รหัสยืนยัน"]),
    "send": (["send", "send message", "post", "publish"], ["ส่งข้อความ", "โพสต์"]),
    "permission": (["allow", "grant", "permit", "install", "trust"], ["อนุญาต", "ติดตั้ง"]),
}
# Never autonomous, even with --allow: irreversible for the whole device or account.
BLOCKED = {"factory_reset", "account_removal"}
# Targets that back out of something are always fine ("Don't allow" contains "allow").
SAFE = (["cancel", "deny", "don't allow", "dont allow", "do not allow", "not now", "no thanks", "no", "close",
         "dismiss", "back", "skip", "later", "keep"], ["ยกเลิก", "ไม่อนุญาต", "ปิด", "ไม่ใช่ตอนนี้", "ภายหลัง"])
# Targets whose meaning is whatever they confirm.
CONFIRM = (["ok", "okay", "yes", "confirm", "continue", "next", "submit", "done", "proceed", "agree", "accept",
            "got it", "apply", "save"], ["ตกลง", "ยืนยัน", "ใช่", "ถัดไป", "ดำเนินการต่อ", "ยอมรับ", "บันทึก"])
# System dialogs that grant permissions or install apps: any non-SAFE button escalates.
PERMISSION_PACKAGES = {"com.google.android.permissioncontroller", "com.android.permissioncontroller",
                       "com.google.android.packageinstaller", "com.android.packageinstaller"}
DIALOG_ELEMENTS = 8  # a screen with this few elements is treated as a dialog for CONFIRM targets
PIN_KEY = re.compile(r"^\d$")


@dataclass
class PolicyDecision:
    action: str = ALLOW
    reason: str = ""
    matched: Optional[str] = None  # category, e.g. "payment"
    keyword: Optional[str] = None
    target: Optional[str] = None

    @property
    def allowed(self):
        return self.action == ALLOW

    def to_dict(self):
        d = {"action": self.action, "reason": self.reason}
        for k in ("matched", "keyword", "target"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


def _words(text):
    return norm(text).replace("_", " ").replace("-", " ")


def matches(text, phrases):
    """First phrase of (english, thai) found in text."""
    en, th = phrases
    t = _words(text)
    for p in en:
        if re.search(r"(?<![\w])%s(?![\w])" % re.escape(p), t):
            return p
    for p in th:
        if p in t:
            return p
    return None


def _camel(rid):
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rid_tail(rid)).replace("_", " ")


def _sensitive(text):
    for cat, phrases in SENSITIVE.items():
        kw = matches(text, phrases)
        if kw:
            return cat, kw
    return None, None


def _target_text(el):
    return " | ".join(x for x in (el.label, el.text, el.content_desc, el.hint, _camel(el.resource_id)) if x)


def _is(text, phrases):
    """The whole label is one of these phrases (not merely contains one)."""
    t = norm(text)
    return t in phrases[0] or t in phrases[1]


class PolicyGate:
    """evaluate() -> PolicyDecision for an operation about to be executed autonomously."""

    def __init__(self, allow_categories=()):
        self.allow_categories = set(allow_categories) - BLOCKED

    def _decide(self, cat, kw, el, reason):
        if cat in BLOCKED:
            return PolicyDecision(BLOCK, "blocked_action", cat, kw, el.label if el else None)
        if cat in self.allow_categories:
            return None
        return PolicyDecision(HANDOFF, reason, cat, kw, el.label if el else None)

    def evaluate(self, operation, element=None, snapshot=None):
        op = (operation or "").upper()
        if op in ("BACK", "HOME", "DONE") or op.startswith("SCROLL") or op.startswith("SWIPE"):
            return PolicyDecision()
        el = element
        if el is None:
            # An action without a known target (raw coordinates, an unknown key) cannot be checked.
            return PolicyDecision(HANDOFF, "unknown_target") if op == "CLICK" else PolicyDecision()

        if op in ("TYPE", "TYPE_TEXT"):
            kw = "password" if el.password else matches(_target_text(el) + " | " + el.context, SENSITIVE["credential"])
            if kw:
                return self._decide("credential", kw, el, "sensitive_input") or PolicyDecision()
            return PolicyDecision()

        label = el.label
        if _is(label, SAFE):
            return PolicyDecision()
        if snapshot and snapshot.package in PERMISSION_PACKAGES:
            return self._decide("permission", snapshot.package, el, "sensitive_action") or PolicyDecision()

        cat, kw = _sensitive(_target_text(el))
        if cat and not (cat == "credential" and el.editable):  # focusing a password field is fine
            return self._decide(cat, kw, el, "sensitive_action") or PolicyDecision()

        screen = snapshot.visible_text if snapshot else ""
        if _is(label, CONFIRM) or (el.role == "button" and not el.text and not el.content_desc):
            # "OK" means whatever it confirms: look at its surroundings, and at the whole
            # screen when it is dialog-sized.
            around = el.context
            if snapshot and len(snapshot.elements) <= DIALOG_ELEMENTS:
                around += " | " + screen
            cat, kw = _sensitive(around)
            if cat and cat != "send":
                return self._decide(cat, kw, el, "sensitive_confirmation") or PolicyDecision()

        # Keypad digits on a PIN / password / OTP screen: a wrong PIN counts toward lockout.
        if PIN_KEY.match(norm(label)) and snapshot:
            if any(e.password for e in snapshot.elements) or matches(screen, SENSITIVE["credential"]):
                return self._decide("credential", label, el, "credential_entry") or PolicyDecision()
        return PolicyDecision()

import pytest

from helpers import by_label, snap
from laya_mobile.policy import ALLOW, BLOCK, HANDOFF, PolicyGate

gate = PolicyGate()


def click(screen, label, **kw):
    s = snap(screen)
    return gate.evaluate("CLICK", by_label(s, label, **kw), s)


def test_payment_target_hands_off_with_details():
    p = click("payment", "Confirm payment")
    assert p.action == HANDOFF and p.reason == "sensitive_action" and p.matched == "payment"
    assert p.to_dict() == {"action": "handoff", "reason": "sensitive_action", "matched": "payment",
                           "keyword": "payment", "target": "Confirm payment"}


def test_harmless_button_next_to_payment_is_allowed():
    assert click("payment", "Back to cart").allowed


def test_buy_buttons_hand_off():
    s = snap("duplicate_buy")
    for b in (e for e in s.elements if e.label == "Buy"):
        assert gate.evaluate("CLICK", b, s).matched == "payment"


def test_delete_elsewhere_on_screen_does_not_stop_unrelated_target():
    assert click("inbox", "Inbox").allowed  # "Delete" button and "Deleted items..." text on screen
    p = click("inbox", "Delete")
    assert p.action == HANDOFF and p.matched == "delete"


def test_generic_ok_is_judged_by_the_dialog_it_confirms():
    p = click("dialog_delete", "OK")
    assert p.action == HANDOFF and p.reason == "sensitive_confirmation" and p.matched == "delete"
    assert click("dialog_delete", "Cancel").allowed


def test_permission_dialog():
    assert click("permission", "While using the app").action == HANDOFF
    assert click("permission", "Only this time").action == HANDOFF
    assert click("permission", "Don’t allow").allowed  # curly apostrophe, as Android renders it


def test_pin_pad_digits_hand_off_but_cancel_does_not():
    p = click("pin_pad", "1")
    assert p.action == HANDOFF and p.reason == "credential_entry"
    assert click("pin_pad", "Cancel").allowed


def test_password_typing_hands_off_but_focusing_the_field_is_fine():
    s = snap("password")
    pw = next(e for e in s.elements if e.password)
    assert gate.evaluate("CLICK", pw, s).allowed
    p = gate.evaluate("TYPE", pw, s)
    assert p.action == HANDOFF and p.reason == "sensitive_input" and p.matched == "credential"
    assert gate.evaluate("TYPE", by_label(s, "somchai"), s).allowed


def test_scroll_back_done_are_always_allowed():
    s = snap("payment")
    for op in ("SCROLL_DOWN", "SCROLL_UP", "BACK", "DONE", "SWIPE_LEFT"):
        assert gate.evaluate(op, None, s).allowed


def test_click_without_known_target_hands_off():
    assert gate.evaluate("CLICK", None, snap("basic")).reason == "unknown_target"


def test_ordinary_settings_navigation_is_allowed():
    s = snap("settings")
    assert all(gate.evaluate("CLICK", e, s).allowed for e in s.elements if not e.scrollable)


@pytest.mark.parametrize("label,category,action", [
    ("Factory data reset", "factory_reset", BLOCK),
    ("Remove account", "account_removal", BLOCK),
    ("Transfer", "money_transfer", HANDOFF),
    ("Send money", "money_transfer", HANDOFF),
    ("Sign out", "logout", HANDOFF),
    ("Uninstall", "delete", HANDOFF),
    ("Enter OTP", "credential", HANDOFF),
    ("ชำระเงิน", "payment", HANDOFF),
    ("โอนเงิน", "money_transfer", HANDOFF),
    ("ออกจากระบบ", "logout", HANDOFF),
    ("Display", None, ALLOW),  # contains "pay" but not the word
    ("Pinned chats", None, ALLOW),  # contains "pin" but not the word
])
def test_keywords(label, category, action):
    from laya_mobile.models import MobileElement
    el = MobileElement(id=0, stable_key="k", cls="android.widget.Button", role="button", label=label,
                       text=label, bounds=[0, 0, 10, 10], clickable=True)
    p = gate.evaluate("CLICK", el, None)
    assert (p.action, p.matched) == (action, category)


def test_resource_id_words_count():
    from laya_mobile.models import MobileElement
    el = MobileElement(id=0, stable_key="k", cls="android.widget.ImageButton", role="button", label="btn_delete",
                       resource_id="com.x:id/btnDeleteAll", bounds=[0, 0, 10, 10], clickable=True)
    assert gate.evaluate("CLICK", el, None).matched == "delete"


def test_allowed_categories_but_never_blocked_ones():
    relaxed = PolicyGate(allow_categories=["payment", "factory_reset"])
    s = snap("payment")
    assert relaxed.evaluate("CLICK", by_label(s, "Confirm payment"), s).allowed
    from laya_mobile.models import MobileElement
    reset = MobileElement(id=0, stable_key="k", cls="android.widget.Button", role="button",
                          label="Factory reset", bounds=[0, 0, 10, 10], clickable=True)
    assert relaxed.evaluate("CLICK", reset, None).action == BLOCK

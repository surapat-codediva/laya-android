import xml.etree.ElementTree as ET

import pytest

from helpers import by_label, snap
from laya_mobile.elements import describe, extract, role_of, stable_key
from laya_mobile.models import MobileElement


@pytest.mark.parametrize("cls,clickable,role", [
    ("android.widget.EditText", True, "input"),
    ("android.widget.AutoCompleteTextView", True, "input"),
    ("android.widget.Button", True, "button"),
    ("com.google.android.material.button.MaterialButton", True, "button"),
    ("android.widget.ImageButton", True, "button"),
    ("android.widget.Switch", True, "switch"),
    ("androidx.appcompat.widget.SwitchCompat", True, "switch"),
    ("android.widget.CheckBox", True, "checkbox"),
    ("android.widget.RadioButton", True, "radio"),
    ("androidx.recyclerview.widget.RecyclerView", False, "list"),
    ("android.widget.ScrollView", False, "scroll"),
    ("androidx.core.widget.NestedScrollView", False, "scroll"),
    ("android.widget.TextView", False, "text"),
    ("android.widget.ImageView", True, "image"),
    ("android.widget.LinearLayout", True, "item"),
    ("android.view.ViewGroup", False, "group"),
    ("android.view.View", False, "view"),
    ("", False, "view"),
])
def test_role_normalization(cls, clickable, role):
    assert role_of(cls, clickable) == role


def test_basic_screen_elements_and_attributes():
    s = snap("basic")
    assert [e.role for e in s.elements] == ["input", "switch", "button", "button"]
    email = by_label(s, "you@example.com")
    assert email.cls == "android.widget.EditText"
    assert email.editable and email.focused and email.clickable and not email.password
    assert email.hint == "Email address"
    assert email.resource_id == "com.example.basic:id/email_input"
    assert email.center == (540, 390)
    assert email.context == "Sign up | Email"

    switch = by_label(s, "newsletter_switch")
    assert switch.checkable and not switch.checked
    assert switch.text == "OFF"  # raw text kept, but "OFF" is state, not a name
    assert switch.context == "Subscribe to newsletter"

    guest = by_label(s, "Continue as guest")
    assert guest.enabled is False


def test_missing_attributes_get_defaults():
    root = ET.fromstring('<hierarchy><node class="android.widget.Button" text="Go" clickable="true" '
                         'bounds="[0,0][10,10]"/></hierarchy>')
    (el,) = extract(root)
    assert el.enabled is True and el.checked is False and el.password is False and el.focused is False
    assert el.resource_id == "" and el.content_desc == "" and el.context == "" and el.package == ""


def test_zero_size_and_unlabeled_nodes_are_skipped():
    s = snap("basic")
    assert all(e.bounds[2] > e.bounds[0] and e.bounds[3] > e.bounds[1] for e in s.elements)
    root = ET.fromstring('<hierarchy><node class="android.widget.ImageButton" clickable="true" '
                         'bounds="[0,0][10,10]"/></hierarchy>')
    assert extract(root) == []


def test_label_falls_back_to_descendants_then_resource_id():
    s = snap("settings")
    row = by_label(s, "Bluetooth | Off")  # clickable row labelled by its child TextViews
    assert row.role == "item"
    assert by_label(s, "Navigate up").content_desc == "Navigate up"
    assert len([e for e in s.elements if e.label == "switch_widget"]) == 3


def test_password_text_is_never_read():
    s = snap("password")
    pw = next(e for e in s.elements if e.password)
    assert pw.text == "" and pw.label == "password"
    assert "hunter2secret" not in s.visible_text
    assert "hunter2secret" not in str(s.to_dict())


def test_duplicate_buttons_get_their_own_context():
    s = snap("duplicate_buy")
    buys = [e for e in s.elements if e.label == "Buy"]
    assert [b.context for b in buys] == ["iPhone Case A | ฿199", "iPhone Case B | ฿159", "iPhone Case C | ฿99"]
    assert len({b.stable_key for b in buys}) == 3
    assert 'button "Buy"' in describe(buys[1]) and 'context="iPhone Case B | ฿159"' in describe(buys[1])


def test_flat_list_context_does_not_borrow_the_next_item():
    s = snap("flat_list")
    buys = [e for e in s.elements if e.label == "Buy"]
    assert [b.context for b in buys] == ["iPhone Case A | ฿199", "iPhone Case B | ฿159"]


def test_context_leaves_out_other_tap_targets_and_the_label():
    s = snap("settings")
    sw = next(e for e in s.elements if e.role == "switch" and e.context.startswith("Bluetooth"))
    assert sw.context == "Bluetooth | Off"
    # Rows don't describe each other; they share the page title.
    assert by_label(s, "Wi-Fi | HomeNet").context == "Network & internet"
    # The OK of a dialog is described by the dialog, not by "Cancel".
    ok = by_label(snap("dialog_delete"), "OK")
    assert ok.context.startswith("Delete 3 photos?")
    assert "Cancel" not in ok.context
    for e in s.elements:
        assert e.label.split(" | ")[0] not in e.context.split(" | ")


def test_context_is_bounded():
    long_text = "word " * 200
    root = ET.fromstring(
        '<hierarchy><node class="android.widget.LinearLayout" bounds="[0,0][100,100]">'
        '<node class="android.widget.TextView" text="%s" bounds="[0,0][100,40]"/>'
        '<node class="android.widget.Button" text="Go" clickable="true" bounds="[0,50][100,100]"/>'
        '</node></hierarchy>' % long_text)
    (el,) = extract(root)
    assert 0 < len(el.context) <= 200


def test_react_native_nested_clickables_collapse_to_one_target():
    s = snap("rn_nested")
    assert [e.label for e in s.elements] == ["Orders | 3 pending", "Track parcel", "Home tab", "Profile"]
    # The Button inside a same-sized clickable ViewGroup replaces the wrapper.
    assert by_label(s, "Track parcel").role == "button"
    assert by_label(s, "Profile").selected
    assert [e.id for e in s.elements] == [0, 1, 2, 3]


def test_scrollable_containers_are_elements():
    s = snap("scrollable")
    lists = [e for e in s.elements if e.scrollable]
    assert {(e.role, e.label) for e in lists} == {("list", "chips"), ("list", "contact_list"), ("scroll", "scroll")}
    assert by_label(s, "Favorites").checked and not by_label(s, "Work").checked


def test_stable_keys_survive_reorder_shift_and_state_change():
    base = {e.label + "@" + e.context: e.stable_key for e in snap("settings").elements}
    moved = {e.label + "@" + e.context: e.stable_key for e in snap("settings_reordered").elements}
    assert base == moved
    on = snap("settings_bt_on")
    sw_off = next(e for e in snap("settings").elements if e.role == "switch" and e.context.startswith("Bluetooth"))
    sw_on = next(e for e in on.elements if e.role == "switch" and e.context.startswith("Bluetooth"))
    assert sw_off.stable_key == sw_on.stable_key and sw_on.checked
    # The row's summary changed (Off -> On) but it is the same row.
    assert by_label(snap("settings"), "Bluetooth | Off").stable_key == by_label(on, "Bluetooth | On").stable_key


def test_stable_key_ignores_id_and_position():
    a = stable_key("button", "x:id/buy", "Buy", "iPhone Case A | ฿199")
    assert a == stable_key("button", "x:id/buy", "Buy", "iPhone Case A | ฿149")  # first context part only
    assert a != stable_key("button", "x:id/buy", "Buy", "iPhone Case B | ฿199")
    assert a != stable_key("button", "x:id/buy", "Buy", "iPhone Case A", container="x:id/cart")


def test_describe_is_compact():
    s = snap("settings")
    sw = next(e for e in s.elements if e.role == "switch" and e.context.startswith("Bluetooth"))
    assert describe(sw) == 'switch "switch_widget" [off] context="Bluetooth | Off"'
    assert describe(sw, full=False) == 'switch "switch_widget" [off]'
    email = by_label(snap("basic"), "you@example.com")
    assert describe(email) == 'input "you@example.com" [focused] context="Sign up | Email" resource="email_input"'
    assert "[disabled]" in describe(by_label(snap("basic"), "Continue as guest"))


def test_element_round_trips_through_dict():
    for el in snap("settings").elements:
        d = el.to_dict()
        assert d["class"] == el.cls and d["center"] == {"x": el.center[0], "y": el.center[1]}
        assert MobileElement.from_dict(d) == el

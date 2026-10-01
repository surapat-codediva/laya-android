import json

import pytest

from helpers import by_label, snap, xml
from laya_mobile.errors import ObservationError
from laya_mobile.models import MobileSnapshot
from laya_mobile.observe import ACTIVITY_MARK, parse_activity, resolve, snapshot_from_xml, split_dump


def test_snapshot_fields():
    s = snap("settings", activity="com.android.settings.SubSettings")
    assert s.package == "com.android.settings"  # the app, not the status bar
    assert s.activity == "com.android.settings.SubSettings"
    assert "12:30" not in s.visible_text  # status bar left out
    assert s.visible_text.startswith("Navigate up | Network & internet | Wi-Fi")
    assert len(s.fingerprint) == 16


def test_snapshot_serializes_to_json_and_back():
    s = snap("duplicate_buy")
    d = json.loads(json.dumps(s.to_dict(), ensure_ascii=False))
    assert set(d) == {"package", "activity", "fingerprint", "timestamp", "visible_text", "elements"}
    assert d["elements"][1]["stable_key"] == s.elements[1].stable_key
    assert MobileSnapshot.from_dict(d) == s


def test_fingerprint_ignores_order_formatting_coordinates_and_clock():
    assert snap("settings").fingerprint == snap("settings_reordered").fingerprint


def test_fingerprint_changes_with_state():
    assert snap("settings").fingerprint != snap("settings_bt_on").fingerprint


def test_fingerprint_changes_with_only_checked():
    base = xml("settings")
    i = base.index('bounds="[880,880][1020,960]"')  # airplane mode switch
    j = base.rindex('checked="false"', 0, i)
    toggled = base[:j] + 'checked="true"' + base[j + len('checked="false"'):]
    assert snapshot_from_xml(base).fingerprint != snapshot_from_xml(toggled).fingerprint


def test_fingerprint_includes_activity():
    assert snap("basic", activity="a.B").fingerprint != snap("basic", activity="a.C").fingerprint


def test_in_app_clock_is_ignored():
    a = xml("basic").replace("Terms apply", "Updated 10:41")
    b = xml("basic").replace("Terms apply", "Updated 10:42")
    assert snapshot_from_xml(a).fingerprint == snapshot_from_xml(b).fingerprint


def test_unparseable_dump_raises():
    with pytest.raises(ObservationError):
        snapshot_from_xml("<hierarchy><node")


def test_split_dump_and_activity():
    out = (xml("basic") + "UI hierchary dumped to: /dev/tty\n" + ACTIVITY_MARK + "\n"
           "  mCurrentFocus=Window{1a2b3c u0 com.example.basic/com.example.basic.SignupActivity}\n"
           "  mFocusedApp=ActivityRecord{4d5e u0 com.example.basic/.SignupActivity t42}\n")
    x, focus = split_dump(out)
    assert x.startswith("<?xml") and x.endswith("</hierarchy>")
    assert parse_activity(focus) == ("com.example.basic", "com.example.basic.SignupActivity")
    assert parse_activity("  mCurrentFocus=Window{9 u0 com.a/com.a.Main}") == ("com.a", "com.a.Main")
    assert parse_activity("  mCurrentFocus=Window{9 u0 PopupWindow:abc}") == (None, None)
    assert split_dump("ERROR: null root node returned by UiTestAutomationBridge.") == (None, "")


def test_resolve_finds_moved_element_and_refuses_ambiguous():
    old, moved = snap("settings"), snap("settings_reordered")
    row = by_label(old, "Bluetooth | Off")
    cur = resolve(row, old, moved)
    assert cur is not None and cur.label == row.label and cur.center == (row.center[0] + 2, row.center[1] + 2)

    shop = snap("duplicate_buy")
    buy_b = shop.elements[2]
    assert resolve(buy_b, shop, shop) is buy_b  # unique key thanks to context

    # Truly identical repeated elements on a changed screen are ambiguous.
    gone = snap("settings_bt_on")
    nav = by_label(old, "Navigate up")
    assert resolve(nav, old, gone).label == "Navigate up"
    assert resolve(by_label(old, "Bluetooth | Off"), old, snap("basic")) is None

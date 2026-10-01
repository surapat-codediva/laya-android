from helpers import snap
from laya_mobile.observe import snapshot_from_xml
from laya_mobile.candidates import candidates_for_operation, eligible


def labels(els):
    return [e.label for e in els]


def test_click_candidates_exclude_pure_scroll_containers():
    s = snap("scrollable")
    click = candidates_for_operation(s, "CLICK")
    assert not any(e.scrollable and not e.clickable for e in click)
    assert set(labels(click)) == {"Search contacts", "Favorites", "Work", "Alice Wong", "Bob Chan",
                                  "Bluetooth Speaker Support"}


def test_type_candidates_are_only_inputs():
    assert labels(candidates_for_operation(snap("password"), "TYPE_TEXT")) == ["somchai", "password"]
    assert labels(candidates_for_operation(snap("settings"), "TYPE")) == []


def test_scroll_candidates_are_scrollable_containers():
    assert set(labels(candidates_for_operation(snap("scrollable"), "SCROLL_DOWN"))) == {"chips", "contact_list",
                                                                                         "scroll"}
    assert labels(candidates_for_operation(snap("settings"), "SCROLL_UP")) == ["recycler_view"]


def test_no_candidates_for_back_or_done():
    s = snap("settings")
    assert candidates_for_operation(s, "BACK") == [] and candidates_for_operation(s, "DONE") == []


def test_ranking_prefers_named_enabled_controls():
    s = snap("basic")
    ranked = labels(candidates_for_operation(s, "CLICK"))
    assert ranked[-1] == "Continue as guest"  # disabled sinks
    assert ranked[0] == "Create account"  # button with a name and an id


def test_goal_overlap_ranks_first_and_limit_applies():
    s = snap("settings")
    top = candidates_for_operation(s, "CLICK", limit=3, goal="turn on Bluetooth")
    assert len(top) == 3
    assert {top[0].label, top[1].label} == {"Bluetooth | Off", "switch_widget"}
    assert all("Bluetooth" in (e.label + e.context) for e in top[:2])


def test_thai_goal_overlap_without_spaces():
    s = snapshot_from_xml(
        '<hierarchy>'
        '<node class="android.widget.Button" text="Wi-Fi" clickable="true" bounds="[0,0][100,100]"/>'
        '<node class="android.widget.Button" text="บลูทูธ" clickable="true" bounds="[0,100][100,200]"/>'
        '</hierarchy>')
    # "เปิดบลูทูธ" is one token; the label is found inside it.
    assert candidates_for_operation(s, "CLICK", goal="เปิดบลูทูธ")[0].label == "บลูทูธ"


def test_candidates_are_deterministic():
    s = snap("duplicate_buy")
    runs = {tuple(e.id for e in candidates_for_operation(s, "CLICK", goal="buy case B")) for _ in range(5)}
    assert len(runs) == 1
    # Ties break on screen order.
    assert [e.id for e in candidates_for_operation(s, "CLICK")] == [1, 2, 3]


def test_eligibility_flags():
    s = snap("basic")
    sw = next(e for e in s.elements if e.role == "switch")
    assert eligible(sw, "click") and not eligible(sw, "TYPE") and not eligible(sw, "SCROLL_DOWN")

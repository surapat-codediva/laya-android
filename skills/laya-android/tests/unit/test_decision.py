from helpers import FakePredictor, pick, snap
from laya_mobile import decision
from laya_mobile.decision import ask_check, confident, decide, laya_state, summary


def test_decide_offers_only_pruned_candidates():
    s = snap("scrollable")
    p = FakePredictor([{"operation": ("CLICK", 0.9), "target": (pick("Bob Chan"), 0.8), "done": 0.1}])
    d = decide(p, "open Bob Chan", s, [], done_q="done?", limit=3)
    _, q = p.calls[0]
    options = q["target"]["criteria"]
    assert len(options) == 3
    assert not any("[scrollable]" in v and "item" not in v for v in options.values())
    assert d["operation"] == "CLICK" and s.element(d["target"]).label == "Bob Chan"
    assert d["target_stable_key"] == s.element(d["target"]).stable_key
    assert d["candidates"] == [int(k) for k in options]
    assert d["fingerprint"] == s.fingerprint and d["p_done"] == 0.1


def test_options_use_the_canonical_description():
    s = snap("duplicate_buy")
    p = FakePredictor([{}])
    decide(p, "buy case B", s, [])
    options = p.calls[0][1]["target"]["criteria"]
    assert options["2"] == 'button "Buy" context="iPhone Case B | ฿159"'  # resource "buy" repeats the label


def test_operations_are_pruned_to_what_the_screen_allows():
    p = FakePredictor([{}])
    decide(p, "x", snap("payment"), [])  # nothing scrollable
    assert set(p.calls[0][1]["operation"]["criteria"]) == {"CLICK", "BACK", "DONE"}
    p = FakePredictor([{}])
    decide(p, "x", snap("settings"), [])
    assert set(p.calls[0][1]["operation"]["criteria"]) == {"CLICK", "SCROLL_DOWN", "SCROLL_UP", "BACK", "DONE"}


def test_state_is_compact_text_with_history():
    s = snap("settings", activity="com.android.settings.SubSettings")
    st = laya_state(s, ["CLICK a", "CLICK b", "BACK", "CLICK c", "CLICK d", "CLICK e"])
    assert st.splitlines()[0] == "app: com.android.settings / SubSettings"
    assert "CLICK a" not in st and "CLICK e" in st  # last 5 only
    assert "<node" not in st


def test_confident_and_summary():
    s = snap("settings")
    d = {"operation": "CLICK", "op_confidence": 0.9, "target": 2, "target_confidence": 0.5, "candidates": [2, 4]}
    assert not confident(d, 0.6) and confident(dict(d, target_confidence=0.7), 0.6)
    assert confident({"operation": "BACK", "op_confidence": 0.7}, 0.6)
    out = summary(d, s)
    assert out["candidates"] == 2 and out["target"]["id"] == 2 and "Wi-Fi" in out["target"]["element"]


def test_ask_check():
    assert ask_check(FakePredictor([{"q": 0.91}]), "is wifi on?", snap("settings")) == 0.91


def test_laya_is_not_loaded_by_unit_tests():
    assert decision._models == {}

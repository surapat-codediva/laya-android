"""uiautomator XML -> MobileElements: roles, labels, surrounding context and stable keys."""
from __future__ import annotations

import hashlib
import re

from .models import MobileElement

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
SYSTEMUI = "com.android.systemui"
LABEL_MAX = 80
CONTEXT_MAX = 200
CONTEXT_PARTS = 4  # nearest text nodes kept as context
CONTEXT_LEVELS = 4  # ancestors climbed looking for context
CONTEXT_SLACK = 100  # px: context parts may be this much farther than twice the nearest one
SAME_CENTER = 4  # px: a nested clickable with the same centre is the same tap target

# Short class name -> role. Checked in order; the first substring match wins.
ROLES = [
    ("EditText", "input"), ("AutoCompleteTextView", "input"),
    ("Switch", "switch"), ("ToggleButton", "switch"),
    ("CheckBox", "checkbox"), ("CheckedTextView", "checkbox"),
    ("RadioButton", "radio"),
    ("ImageButton", "button"), ("FloatingActionButton", "button"), ("Button", "button"),
    ("RecyclerView", "list"), ("ListView", "list"), ("GridView", "list"),
    ("ScrollView", "scroll"), ("ViewPager", "scroll"),
    ("SeekBar", "slider"), ("ProgressBar", "progress"), ("Spinner", "dropdown"),
    ("WebView", "web"), ("ImageView", "image"), ("TextView", "text"),
]
GENERIC = {"view", "group", "item", "text", "image"}  # roles a nested, more specific node replaces
CLOCK = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")  # times change by themselves; keys and fingerprints ignore them
STATE_TEXT = {"on", "off", "เปิด", "ปิด"}  # a switch's own text that is its state, not its name


def role_of(cls, clickable=False):
    short = cls.rsplit(".", 1)[-1]
    for name, role in ROLES:
        if name in short:
            return role
    if not short or short == "View":
        role = "view"
    else:  # LinearLayout, ViewGroup, FrameLayout, ConstraintLayout ...
        role = "group"
    # A clickable layout is a row/card/tile the user taps as a whole.
    return "item" if clickable else role


def norm(t):
    return " ".join((t or "").replace("\u2019", "'").split()).casefold()


def _flag(node, name):
    return node.get(name) == "true"


def _bounds(node):
    m = BOUNDS.match(node.get("bounds", ""))
    return list(map(int, m.groups())) if m else None


def _own_text(node):
    """Text a node shows itself. A password field's text is never read."""
    if _flag(node, "password"):
        return ""
    return (node.get("text") or "").strip()


def node_text(node):
    return _own_text(node) or (node.get("content-desc") or "").strip()


def _reading_order(nodes, order):
    """Top-to-bottom, left-to-right on screen; XML order only breaks exact ties. Keeps labels
    and context the same when a dump lists the same nodes in another order."""
    def key(n):
        b = _bounds(n) or [0, 0, 0, 0]
        return b[1], b[0], order.get(n, 0)
    return sorted(nodes, key=key)


def _parts(nodes):
    seen, out = set(), []
    for n in nodes:
        t = node_text(n)
        if t and norm(t) not in seen:
            seen.add(norm(t))
            out.append(t)
    return out


def rid_tail(rid):
    return rid.rsplit("/", 1)[-1] if rid else ""


def _label(node, role, order):
    text = _own_text(node)
    if role in ("switch", "checkbox", "radio") and norm(text) in STATE_TEXT:
        text = ""
    own = text or (node.get("content-desc") or "").strip() or (node.get("hint") or "").strip()
    if own:
        return own
    # React Native puts the text in child TextViews of a clickable ViewGroup. A list's
    # children are its content, not its name.
    if role not in ("list", "scroll", "web"):
        joined = " | ".join(_parts(_reading_order(list(node.iter())[1:], order)))
        if joined:
            return joined
    return rid_tail(node.get("resource-id", "")) or (role if role in ("list", "scroll", "web") else "")


def _distance(a, b):
    """How far text box `b` is from element box `a`. Text below the element counts triple:
    a list item's title and price sit above or beside its button, the next item sits below."""
    dx = max(0, b[0] - a[2], a[0] - b[2])
    dy = max(0, b[1] - a[3], a[1] - b[3])
    return dx + dy * (3 if b[1] >= a[3] else 1)


def _clip(parts, limit):
    """Join whole parts while they fit in `limit` chars."""
    out = ""
    for p in parts:
        nxt = out + " | " + p if out else p
        if len(nxt) > limit:
            return out or p[:limit]
        out = nxt
    return out


def _owners(root):
    """node -> the innermost tap target (button, row, switch, input ...) containing it. Text a
    tap target owns describes that target, not its neighbours."""
    owner = {}

    def walk(n, cur):
        cls = n.get("class", "")
        role = role_of(cls, _flag(n, "clickable"))
        if (_actionable(n, role == "input") and role not in ("text", "list", "scroll", "web")
                and not (_flag(n, "scrollable") and not _flag(n, "clickable"))):
            cur = n
        owner[n] = cur
        for c in n:
            walk(c, cur)
    walk(root, None)
    return owner


def _context(node, box, label, parents, order, owner):
    """Nearby text from the closest ancestor that has text outside this element: the product
    name and price next to a "Buy" button, the title of a settings row next to its switch,
    the message of the dialog an "OK" button closes. Text inside other tap targets (the next
    row, the "Cancel" beside "OK") is left out."""
    own = set(node.iter())
    mine = {node}
    p = parents.get(node)
    while p is not None:
        mine.add(p)
        p = parents.get(p)
    skip = {norm(p) for p in label.split(" | ")}
    anc, level = parents.get(node), 0
    while anc is not None and level < CONTEXT_LEVELS:
        found, seen = [], set()
        pool = [n for n in anc.iter() if n not in own and owner.get(n) in mine | {None}]
        for n in _reading_order(pool, order):
            t, b = node_text(n), _bounds(n)
            if not t or b is None or norm(t) in skip or norm(t) in seen:
                continue
            seen.add(norm(t))
            found.append((_distance(box, b), b[1], b[0], order[n], t))
        if found:
            # Nearest parts only, and none much farther than the nearest: in a flat list the
            # next item's title is close too, but farther than this item's own text.
            near = sorted(found)[:CONTEXT_PARTS]
            cutoff = near[0][0] * 2 + CONTEXT_SLACK
            near = [x for x in near if x[0] <= cutoff]
            return _clip([x[-1] for x in sorted(near, key=lambda x: x[1:4])], CONTEXT_MAX)
        anc, level = parents.get(anc), level + 1
    return ""


def _container_id(node, parents):
    anc, level = parents.get(node), 0
    while anc is not None and level < 3:
        if anc.get("resource-id"):
            return anc.get("resource-id")
        anc, level = parents.get(anc), level + 1
    return ""


def stable_key(role, resource_id, label, context, container=""):
    """Same logical element -> same key across dumps. Built from what the element is (role, id,
    name) and where it sits (first context part, enclosing container id) -- never from its
    position or its index, which shift when a list scrolls. Only the first part of a label or
    context is used: later parts are often state ("Bluetooth | Off"); clock times are masked.
    Truly identical elements (same everything) share a key; `resolve` treats that as ambiguous."""
    first = lambda s: CLOCK.sub("#:##", norm(s.split(" | ")[0])) if s else ""
    blob = "\x1f".join([role, resource_id, first(label), first(context), container])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _actionable(node, editable):
    return any(_flag(node, a) for a in ("clickable", "long-clickable", "checkable", "scrollable")) or editable


def extract(root):
    """Actionable elements of a parsed uiautomator dump, in document order, ids 0..n-1."""
    parents = {c: p for p in root.iter() for c in p}
    order = {n: i for i, n in enumerate(root.iter())}
    owner = _owners(root)
    kept = []  # (node, element)
    for node in root.iter("node"):
        cls = node.get("class", "")
        clickable = _flag(node, "clickable")
        role = role_of(cls, clickable)
        editable = role == "input"
        if not _actionable(node, editable):
            continue
        box = _bounds(node)
        if not box or box[2] <= box[0] or box[3] <= box[1]:
            continue
        label = _label(node, role, order)[:LABEL_MAX]
        if not label:
            continue
        el = MobileElement(
            id=0, stable_key="", cls=cls, role=role, label=label, bounds=box,
            text=_own_text(node), content_desc=(node.get("content-desc") or "").strip(),
            hint=(node.get("hint") or "").strip(), resource_id=node.get("resource-id", ""),
            package=node.get("package", ""),
            clickable=clickable, long_clickable=_flag(node, "long-clickable"), editable=editable,
            scrollable=_flag(node, "scrollable"), checkable=_flag(node, "checkable"),
            enabled=node.get("enabled", "true") != "false", checked=_flag(node, "checked"),
            selected=_flag(node, "selected"), focused=_flag(node, "focused"), password=_flag(node, "password"),
        )
        el.context = _context(node, box, label, parents, order, owner)
        el.stable_key = stable_key(role, el.resource_id, label, el.context, _container_id(node, parents))
        dup = _nested_duplicate(node, el, kept, parents)
        if dup is None:
            kept.append((node, el))
        elif el.role not in GENERIC and kept[dup][1].role in GENERIC:
            # A Button inside a same-sized clickable wrapper: the Button says more.
            kept[dup] = (node, el)
    for i, (_, el) in enumerate(kept):
        el.id = i
    return [el for _, el in kept]


def _scroll_only(el):
    return el.scrollable and not el.clickable


def _nested_duplicate(node, el, kept, parents):
    """Index of a kept ancestor that is the same tap target (React Native nests clickable
    ViewGroups with identical bounds), or None."""
    if _scroll_only(el):
        return None
    ancestors = set()
    p = parents.get(node)
    while p is not None:
        ancestors.add(p)
        p = parents.get(p)
    x, y = el.center
    for i, (n, k) in enumerate(kept):
        if n in ancestors and not _scroll_only(k):
            kx, ky = k.center
            if abs(kx - x) <= SAME_CENTER and abs(ky - y) <= SAME_CENTER:
                return i
    return None


# ---------- descriptions ----------

def states(el):
    s = []
    if el.checkable:
        s.append("[on]" if el.checked else "[off]")
    if el.selected:
        s.append("[selected]")
    if el.focused and el.editable:
        s.append("[focused]")
    if el.password:
        s.append("[password]")
    if el.scrollable:
        s.append("[scrollable]")
    if not el.enabled:
        s.append("[disabled]")
    return s


def describe(el, full=True):
    """The one text form of an element, used for Laya's options, `screen` and the history.
    full=False drops context and resource id (for the short action history)."""
    out = ['%s "%s"' % (el.role, el.label)] + states(el)
    if full:
        if el.context:
            ctx = el.context if len(el.context) <= 120 else el.context[:117] + "..."
            out.append('context="%s"' % ctx)
        tail = rid_tail(el.resource_id)
        if tail and norm(tail) != norm(el.label):
            out.append('resource="%s"' % tail)
    return " ".join(out)

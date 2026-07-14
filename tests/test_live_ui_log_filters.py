"""The activity-log type filter must offer a chip for every known log type.

The chip set is the only way to toggle a type, but the filter's default state is
seeded from ``LOG_TYPE_KEYS`` (derived from ``LOG_TYPE_META``) rather than from
the markup. A type with no chip is therefore permanently enabled: it cannot be
hidden, and it silently skews the "filter is active" badge, which compares the
selection size against ``LOG_TYPE_KEYS.length``. That is exactly how ``read_file``
and ``write_file`` were shipped without filters, so these tests pin the three
places that have to agree: the JS type table, the chip markup, and the ``.lt-*``
badge colors.

Parsed with regexes rather than executed - the dashboard has no JS test runner,
and these declarations are flat literals.
"""

import re
from pathlib import Path

from synnodb.observability.live_ui import live_dashboard

_UI_DIR = Path(live_dashboard.__file__).parent
_LOG_JS = (_UI_DIR / "js" / "log.js").read_text()
_INDEX_HTML = (_UI_DIR / "index.html").read_text()
_STYLE_CSS = (_UI_DIR / "style.css").read_text()

# `llm: { label:'LLM', cls:'lt-llm' },` inside the LOG_TYPE_META object literal.
_META_ENTRY_RE = re.compile(
    r"(\w+)\s*:\s*\{\s*label\s*:\s*'([^']*)'\s*,\s*cls\s*:\s*'([^']*)'\s*\}"
)
# `<button ... class="lfc-btn active lt-llm" data-val="llm">LLM</button>`
_CHIP_RE = re.compile(
    r'<button[^>]*class="([^"]*\blfc-btn\b[^"]*)"[^>]*data-val="([^"]+)"[^>]*>([^<]*)</button>'
)


def _js_types() -> dict[str, tuple[str, str]]:
    """LOG_TYPE_META as {type: (label, css_class)}, in declaration order."""
    block = re.search(r"const LOG_TYPE_META = \{(.*?)\n\};", _LOG_JS, re.S)
    assert block, "LOG_TYPE_META object literal not found in log.js"
    types = {m[1]: (m[2], m[3]) for m in _META_ENTRY_RE.finditer(block.group(1))}
    assert types, "no entries parsed out of LOG_TYPE_META"
    return types


def _chips() -> dict[str, tuple[str, str, list[str]]]:
    """Type-filter chips as {data-val: (label, css_class, all_classes)}, in DOM order."""
    block = re.search(
        r'<div class="log-filter-chips" data-filter="type">(.*?)</div>',
        _INDEX_HTML,
        re.S,
    )
    assert block, 'type chip container (data-filter="type") not found in index.html'
    chips = {}
    for classes, val, label in (m.groups() for m in _CHIP_RE.finditer(block.group(1))):
        cls_list = classes.split()
        lt_cls = [c for c in cls_list if c.startswith("lt-")]
        assert len(lt_cls) == 1, (
            f"chip {val!r} must carry exactly one lt-* class, got {lt_cls}"
        )
        chips[val] = (label.strip(), lt_cls[0], cls_list)
    assert chips, "no chips parsed out of the type filter"
    return chips


def test_every_log_type_has_a_filter_chip() -> None:
    """Chips cover exactly LOG_TYPE_KEYS: every JS type, plus the 'other' catch-all.

    Read/write tools regressed here, so they are named explicitly - a chip set
    that drops them must fail loudly, not just shrink.
    """
    chips = _chips()
    expected = [*_js_types(), "other"]

    assert list(chips) == expected, (
        "type filter chips are out of sync with LOG_TYPE_META in log.js "
        f"(missing: {sorted(set(expected) - set(chips))}, "
        f"unexpected: {sorted(set(chips) - set(expected))})"
    )
    for tool in ("read_file", "write_file", "apply_patch"):
        assert tool in chips, f"no activity filter chip for the {tool} tool"


def test_chip_labels_and_colors_match_the_badges() -> None:
    """A chip must look like the badge it filters: same text, same .lt-* color."""
    chips = _chips()
    for log_type, (label, cls) in _js_types().items():
        chip_label, chip_cls, _ = chips[log_type]
        assert chip_label == label, (
            f"{log_type} chip reads {chip_label!r}, badge reads {label!r}"
        )
        assert chip_cls == cls, (
            f"{log_type} chip is styled {chip_cls!r}, badge is {cls!r}"
        )


def test_chip_styles_are_defined() -> None:
    """Every chip's .lt-* class has a rule in style.css (else it renders unstyled)."""
    for val, (_, cls, _) in _chips().items():
        assert re.search(rf"\.{re.escape(cls)}\s*\{{", _STYLE_CSS), (
            f"chip {val!r} uses .{cls}, which style.css does not define"
        )


def test_all_chips_start_enabled() -> None:
    """Chips default to 'active', matching the filter state seeded from LOG_TYPE_KEYS.

    An inactive chip would render as off while its type still passed the filter.
    """
    for val, (_, _, cls_list) in _chips().items():
        assert "active" in cls_list, f"chip {val!r} does not start out active"

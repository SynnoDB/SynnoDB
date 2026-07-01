"""The cgroup launch policy is gated on env booleans that feed the HotpatchPool key;
a sloppy truthiness check (`SYNNO_ENABLE_CGROUP=0` reading as on) would both enable
the path unexpectedly and pollute the pool key. Pin the parser."""

import pytest

from synnodb.tools.run import _cgroup_launch_policy, _env_bool

_FLAG = "SYNNO_TEST_ENV_BOOL"
_BUDGET = 64 << 20


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),  # unset
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        (" 0 ", False),  # whitespace tolerated
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("anything", True),
    ],
)
def test_env_bool(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(_FLAG, raising=False)
    else:
        monkeypatch.setenv(_FLAG, value)
    assert _env_bool(_FLAG) is expected


def _set(monkeypatch, enable, require=None):
    # Clear the shared-parent vars so suffix assertions are deterministic; tests that
    # exercise them set them explicitly.
    monkeypatch.delenv("SYNNO_CGROUP_PARENT", raising=False)
    monkeypatch.delenv("SYNNO_CGROUP_PARENT_MAX", raising=False)
    if enable is None:
        monkeypatch.delenv("SYNNO_ENABLE_CGROUP", raising=False)
    else:
        monkeypatch.setenv("SYNNO_ENABLE_CGROUP", enable)
    if require is None:
        monkeypatch.delenv("SYNNO_REQUIRE_CGROUP", raising=False)
    else:
        monkeypatch.setenv("SYNNO_REQUIRE_CGROUP", require)


def test_cgroup_policy_disabled_by_default(monkeypatch):
    _set(monkeypatch, enable=None)
    assert _cgroup_launch_policy(_BUDGET) == ({}, "")


def test_cgroup_policy_disabled_when_off_value(monkeypatch):
    _set(monkeypatch, enable="0", require="1")  # 0 must read as off despite require=1
    assert _cgroup_launch_policy(_BUDGET) == ({}, "")


def test_cgroup_policy_enabled_not_required(monkeypatch):
    _set(monkeypatch, enable="1", require=None)
    kwargs, suffix = _cgroup_launch_policy(_BUDGET)
    assert kwargs == {"memory_max_bytes": _BUDGET, "require_cgroup": False}
    assert suffix == f"|cgroup_max={_BUDGET}|require=False|parent=|parent_max="


def test_cgroup_policy_required(monkeypatch):
    _set(monkeypatch, enable="1", require="1")
    kwargs, suffix = _cgroup_launch_policy(_BUDGET)
    assert kwargs == {"memory_max_bytes": _BUDGET, "require_cgroup": True}
    assert suffix == f"|cgroup_max={_BUDGET}|require=True|parent=|parent_max="


def test_pool_key_suffix_distinguishes_shared_parent(monkeypatch):
    """A warm runner under the old per-orchestrator parent must not be reused once a
    shared parent (or a different slice / budget) is configured, or it would bypass the
    aggregate slice. The pool-key suffix must therefore vary with SYNNO_CGROUP_PARENT and
    SYNNO_CGROUP_PARENT_MAX."""
    _set(monkeypatch, enable="1")
    _, none = _cgroup_launch_policy(_BUDGET)
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", "/sys/fs/cgroup/synnodb.slice")
    _, slice_a = _cgroup_launch_policy(_BUDGET)
    monkeypatch.setenv("SYNNO_CGROUP_PARENT", "/sys/fs/cgroup/other.slice")
    _, slice_b = _cgroup_launch_policy(_BUDGET)
    monkeypatch.setenv("SYNNO_CGROUP_PARENT_MAX", "500G")
    _, slice_b_budgeted = _cgroup_launch_policy(_BUDGET)
    assert len({none, slice_a, slice_b, slice_b_budgeted}) == 4


def test_pool_key_suffix_distinguishes_policies(monkeypatch):
    """Merge-blocker invariant: disabled, enabled-not-required, and enabled-required
    must yield three distinct pool-key suffixes, so a warm runner from one policy is
    never reused for another (which would bypass the ceiling / fail-closed check)."""
    _set(monkeypatch, enable=None)
    _, off = _cgroup_launch_policy(_BUDGET)
    _set(monkeypatch, enable="1", require=None)
    _, on_notreq = _cgroup_launch_policy(_BUDGET)
    _set(monkeypatch, enable="1", require="1")
    _, on_req = _cgroup_launch_policy(_BUDGET)
    assert len({off, on_notreq, on_req}) == 3

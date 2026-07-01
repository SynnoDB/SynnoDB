"""The hotpatch pool must restart a warm runner when its build fingerprint changes.

A libloader.so source change makes the engine's in-RAM Arrow input stale, which an
in-place hotpatch cannot fix; the pool keys reuse on a fingerprint so the stale
engine is retired instead of silently answering with stale data. These tests pin
that behavior with a fake runner (no real engine launched).
"""
from synnodb.cpp_runner.hotpatch.pool import _HotpatchHolder


class _FakeRunner:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def _factory_seq():
    """A factory returning a fresh fake runner per call, recording them."""
    created: list[_FakeRunner] = []

    def factory() -> _FakeRunner:
        r = _FakeRunner()
        created.append(r)
        return r

    return factory, created


def test_reuses_runner_when_fingerprint_unchanged():
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    r1 = pool.get("k", factory, fingerprint="id-A")
    r2 = pool.get("k", factory, fingerprint="id-A")
    assert r1 is r2
    assert len(created) == 1
    assert not r1.terminated


def test_restarts_runner_when_fingerprint_changes():
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    r1 = pool.get("k", factory, fingerprint="id-A")
    r2 = pool.get("k", factory, fingerprint="id-B")
    assert r1 is not r2
    assert r1.terminated is True  # stale engine retired
    assert r2.terminated is False
    assert len(created) == 2


def test_none_fingerprint_does_not_restart():
    """A None fingerprint (e.g. the .so is not built yet) is 'no change signal':
    the warm runner must be reused, never thrashed."""
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    r1 = pool.get("k", factory, fingerprint="id-A")
    r2 = pool.get("k", factory, fingerprint=None)
    assert r1 is r2
    assert len(created) == 1
    assert not r1.terminated


def test_unknown_then_known_fingerprint_does_not_restart():
    """A runner first created when the fingerprint was unknown (None) must not be
    thrashed the moment the fingerprint becomes readable - None is 'no signal' in
    both the stored and the incoming direction."""
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    r1 = pool.get("k", factory, fingerprint=None)
    r2 = pool.get("k", factory, fingerprint="id-A")
    assert r1 is r2
    assert len(created) == 1
    assert not r1.terminated


def test_fingerprint_isolated_per_key():
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    a = pool.get("a", factory, fingerprint="id-A")
    b = pool.get("b", factory, fingerprint="id-B")
    assert a is not b
    # Re-getting each with its own fingerprint reuses, not restarts.
    assert pool.get("a", factory, fingerprint="id-A") is a
    assert pool.get("b", factory, fingerprint="id-B") is b
    assert len(created) == 2


def test_terminate_clears_fingerprint():
    """After terminate, a re-get with the same fingerprint builds a fresh runner
    (the fingerprint must not linger and suppress the rebuild)."""
    pool = _HotpatchHolder()
    factory, created = _factory_seq()
    r1 = pool.get("k", factory, fingerprint="id-A")
    assert pool.terminate("k") is True
    r2 = pool.get("k", factory, fingerprint="id-A")
    assert r1 is not r2
    assert len(created) == 2

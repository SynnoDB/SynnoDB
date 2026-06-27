"""Regression tests for the compaction cache key.

The key used to be {response_id, model} only. litellm/local models report
response_id=None, so that key was constant per model: every compaction in a run -
and across runs/days - collided on ONE cache entry, silently re-injecting a stale
summary (and stale re-anchor) from an unrelated earlier compaction point. The key
must be content-addressed: transcript digest + mode + reinserted re-anchor prompt +
response_id + model.

These tests build a bare instance via __new__ (no __init__): _get_cache_path only
reads _response_id, compaction_model_name and cache_dir.
"""

import json
from pathlib import Path

from synnodb.llm.llm_caching.cached_compaction_session import (
    CachedOpenAIResponsesCompactionSession,
)


def _session(response_id=None, model="openai/unsloth/MiniMax-M3", cache_dir=Path("/tmp")):
    inst = CachedOpenAIResponsesCompactionSession.__new__(
        CachedOpenAIResponsesCompactionSession
    )
    inst._response_id = response_id  # litellm/local models: always None
    inst.compaction_model_name = model
    inst.cache_dir = cache_dir
    return inst


_ITEMS_A = [{"role": "user", "content": "transcript A"}]
_ITEMS_B = [{"role": "user", "content": "transcript B (more turns)"}]


def _key(session, items, mode="input", reanchor="STAGE: storage"):
    """Return just the hash (path stem); cache_dir is constant within a test."""
    path, _payload = session._get_cache_path(items, mode, reanchor)
    return path.stem


# ---------- determinism (replay must still hit) ----------


def test_same_content_and_stage_is_deterministic():
    s = _session()
    p1, _ = s._get_cache_path(_ITEMS_A, "input", "STAGE: storage")
    p2, _ = s._get_cache_path(_ITEMS_A, "input", "STAGE: storage")
    assert p1 == p2


def test_returned_path_is_under_cache_dir(tmp_path):
    s = _session(cache_dir=tmp_path)
    path, _ = s._get_cache_path(_ITEMS_A, "input", "STAGE: storage")
    assert path.parent == tmp_path
    assert path.suffix == ".pkl"


# ---------- the bug: response_id=None must NOT collapse distinct compactions ----------


def test_different_transcript_does_not_collide_with_response_id_none():
    s = _session()
    assert _key(s, _ITEMS_A) != _key(s, _ITEMS_B)


def test_different_reanchor_prompt_does_not_collide():
    s = _session()
    a = _key(s, _ITEMS_A, reanchor="STAGE: storage")
    b = _key(s, _ITEMS_A, reanchor="STAGE: implement queries")
    # the reinserted prompt is embedded in the cached output, so a different stage
    # prompt must not reuse another stage's compaction.
    assert a != b


# ---------- every field independently participates in the key ----------


def test_mode_participates():
    s = _session()
    assert _key(s, _ITEMS_A, mode="input") != _key(s, _ITEMS_A, mode="previous_response_id")


def test_model_participates():
    assert _key(_session(model="model-x"), _ITEMS_A) != _key(
        _session(model="model-y"), _ITEMS_A
    )


def test_response_id_participates():
    # for the OpenAI path response_id IS set; it must still distinguish entries.
    assert _key(_session(response_id="resp_1"), _ITEMS_A) != _key(
        _session(response_id="resp_2"), _ITEMS_A
    )


def test_none_vs_empty_reanchor_are_distinct():
    s = _session()
    assert _key(s, _ITEMS_A, reanchor=None) != _key(s, _ITEMS_A, reanchor="")


# ---------- None session_items: deterministic, never a wrong hit ----------


def test_none_session_items_is_deterministic():
    s = _session()
    p1, payload = s._get_cache_path(None, "input", None)
    p2, _ = s._get_cache_path(None, "input", None)
    assert p1 == p2  # hashes stable_json(None) == "null"
    assert isinstance(json.loads(payload)["session_digest"], str)


# ---------- non-serialisable items: degrade to recompute, never a wrong hit ----------


def test_non_serialisable_distinct_objects_get_distinct_keys():
    # repr carries the object address, so DIFFERENT objects (e.g. across runs) miss
    # the cache rather than wrongly hit. We assert distinctness, not cross-run reuse:
    # for non-JSON items the key deliberately degrades to "always recompute".
    s = _session()

    class _Unjsonable:
        pass

    a = [{"role": "user", "content": _Unjsonable()}]
    b = [{"role": "user", "content": _Unjsonable()}]
    assert _key(s, a) != _key(s, b)


# ---------- payload shape ----------


def test_stable_payload_contains_all_key_fields():
    s = _session(response_id="r", model="m")
    _path, payload = s._get_cache_path(_ITEMS_A, "input", "STAGE: storage")
    parsed = json.loads(payload)
    assert set(parsed) == {
        "response_id",
        "model",
        "mode",
        "session_digest",
        "reanchor_prompt",
    }
    assert parsed["response_id"] == "r"
    assert parsed["model"] == "m"
    assert parsed["mode"] == "input"
    assert parsed["reanchor_prompt"] == "STAGE: storage"
    assert isinstance(parsed["session_digest"], str)  # sha256 hex of the transcript

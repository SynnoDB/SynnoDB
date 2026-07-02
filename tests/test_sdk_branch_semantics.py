"""Pin the SDK session's turn-indexing semantics for conversation branching.

The per-query optimization loop branches the conversation once per query via
``create_branch_from_turn(turn_nr)``. This test pins the exact semantics the
loop's branch-anchor logic relies on:

- user messages are 1-indexed turns (``get_conversation_turns``),
- ``create_branch_from_turn(n)`` copies turns *strictly before* ``n`` - the
  turn at the branch point is excluded from the new branch,
- branching from turn 0 (empty conversation) is impossible.

Consequence: branching from the last turn number sacrifices that turn from
every per-query branch, which is why the multi-threading round emits a
disposable no-op "branch anchor" turn before branching.
"""

from __future__ import annotations

import asyncio

import pytest
from agents.extensions.memory.advanced_sqlite_session import AdvancedSQLiteSession


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _make_session(tmp_path) -> AdvancedSQLiteSession:
    return AdvancedSQLiteSession(
        session_id="branch-semantics-test",
        db_path=tmp_path / "session.db",
        create_tables=True,
    )


def test_turns_are_one_indexed_user_messages(tmp_path):
    async def scenario():
        session = _make_session(tmp_path)
        assert await session.get_conversation_turns() == []
        await session.add_items([_user("turn one"), _assistant("answer one")])
        await session.add_items([_user("turn two"), _assistant("answer two")])
        turns = await session.get_conversation_turns()
        assert [t["turn"] for t in turns] == [1, 2]
        return turns

    turns = asyncio.run(scenario())
    assert "turn one" in turns[0]["content"]


def test_branch_from_turn_excludes_the_branch_point_turn(tmp_path):
    async def scenario():
        session = _make_session(tmp_path)
        await session.add_items([_user("turn one"), _assistant("answer one")])
        await session.add_items([_user("turn two"), _assistant("answer two")])
        await session.add_items([_user("turn three"), _assistant("answer three")])

        last_turn = (await session.get_conversation_turns())[-1]["turn"]
        assert last_turn == 3

        await session.create_branch_from_turn(last_turn, branch_name="per-query")
        branch_turns = await session.get_conversation_turns()
        return branch_turns

    branch_turns = asyncio.run(scenario())
    # The turn at the branch point (turn 3) is NOT copied into the branch:
    # only turns strictly before it survive.
    assert [t["turn"] for t in branch_turns] == [1, 2]
    contents = " | ".join(t["content"] for t in branch_turns)
    assert "turn three" not in contents


def test_branching_an_empty_conversation_is_impossible(tmp_path):
    async def scenario():
        session = _make_session(tmp_path)
        assert await session.get_conversation_turns() == []
        with pytest.raises(ValueError):
            await session.create_branch_from_turn(0, branch_name="impossible")

    asyncio.run(scenario())

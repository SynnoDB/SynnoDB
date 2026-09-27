"""An unattended run must never block on a question nobody can answer.

The dirty-workspace confirmation in ``main`` was asked unconditionally, so the ordinary
way the Python API is driven - a notebook - died on the kernel's
``StdinNotImplementedError`` the second time it ran over the same workspace. Two things
have to hold: the flag that answers every other prompt answers this one too, and where
there is genuinely no one to ask, the failure names the question instead of the stdin.
"""

from __future__ import annotations

import builtins

import pytest

from synnodb.utils.confirm_dialog import NoOneToAskError, await_user_confirmation


def test_yes_and_no_are_read_from_stdin(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _prompt: " Y ")
    assert await_user_confirmation("proceed?") is True
    monkeypatch.setattr(builtins, "input", lambda _prompt: "no")
    assert await_user_confirmation("proceed?") is False


def test_invalid_answer_is_re_asked(monkeypatch):
    answers = iter(["maybe", "", "yes"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    assert await_user_confirmation("proceed?") is True


@pytest.mark.parametrize(
    "failure",
    [
        EOFError("EOF when reading a line"),  # piped stdin
        RuntimeError("raw_input was called, but this frontend..."),  # Jupyter kernel
    ],
)
def test_no_interactive_stdin_names_the_question(monkeypatch, failure):
    def no_stdin(_prompt):
        raise failure

    monkeypatch.setattr(builtins, "input", no_stdin)
    with pytest.raises(NoOneToAskError) as excinfo:
        await_user_confirmation("Remove the uncommitted changes?")
    message = str(excinfo.value)
    assert "Remove the uncommitted changes?" in message  # what was being asked
    assert "auto_confirm=True" in message  # and how to answer it in advance

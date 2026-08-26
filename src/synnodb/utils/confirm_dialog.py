import logging

logger = logging.getLogger(__name__)


class NoOneToAskError(RuntimeError):
    """A confirmation was needed but there is no interactive user to ask.

    Raised instead of letting ``input()`` fail with whatever the host frontend happens to
    throw (a bare ``EOFError`` under a pipe, ``StdinNotImplementedError`` in a Jupyter
    kernel), so an unattended run - a notebook, a script, CI - says what it wanted and how
    to answer it in advance rather than dying on an opaque stdin error.
    """


def await_user_confirmation(message: str) -> bool:
    """
    Await user confirmation for a given message.

    Args:
        message (str): The message to display to the user.
    Returns:
        bool: True if the user confirms, False otherwise.
    Raises:
        NoOneToAskError: if there is no interactive stdin to answer the prompt.
    """
    while True:
        try:
            user_input = input(f"{message} (y/n): ").strip().lower()
        except Exception as exc:
            # No usable stdin: a notebook kernel, a pipe, a systemd unit. Nobody can ever
            # answer, so say what was being asked instead of surfacing a stdin error.
            raise NoOneToAskError(
                f"{message}\n\nThis run is not interactive, so the question above cannot "
                f"be answered. Re-run with auto_confirm=True (CLI: --auto_u) to answer it "
                f"automatically, or resolve it before starting."
            ) from exc
        if user_input in ["y", "yes"]:
            return True
        elif user_input in ["n", "no"]:
            return False
        else:
            logger.warning("Invalid input. Please enter 'y' for yes or 'n' for no.")

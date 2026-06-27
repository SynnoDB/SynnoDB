import logging

logger = logging.getLogger(__name__)


def await_user_confirmation(message: str) -> bool:
    """
    Await user confirmation for a given message.

    Args:
        message (str): The message to display to the user.
    Returns:
        bool: True if the user confirms, False otherwise.
    """
    while True:
        user_input = input(f"{message} (y/n): ").strip().lower()
        if user_input in ["y", "yes"]:
            return True
        elif user_input in ["n", "no"]:
            return False
        else:
            logger.warning("Invalid input. Please enter 'y' for yes or 'n' for no.")

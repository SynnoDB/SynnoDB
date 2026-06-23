import pytest

from conversations.filenames import get_filenames, get_plan_filename


class UsecaseLike:
    def __init__(self, value: str):
        self.value = value


def test_plan_filename_is_usecase_specific():
    assert get_plan_filename("olap") == "storage_plan.txt"
    assert get_plan_filename("bff") == "file_format_plan.txt"
    assert get_plan_filename(UsecaseLike("bff")) == "file_format_plan.txt"


def test_get_filenames_uses_usecase_plan_filename():
    filenames = get_filenames("bff")

    assert filenames["plan_filename"] == "file_format_plan.txt"
    assert filenames["storage_plan_filename"] == "file_format_plan.txt"


def test_plan_filename_rejects_unknown_usecase():
    with pytest.raises(ValueError):
        get_plan_filename("unknown")

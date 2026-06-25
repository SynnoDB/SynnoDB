from conversations.filenames import get_filenames, get_plan_filename
from utils.cli_config import Usecase


def test_plan_filename_is_usecase_specific():
    assert get_plan_filename(Usecase.OLAP) == "storage_plan.txt"
    assert get_plan_filename(Usecase.BFF) == "file_format_plan.txt"


def test_get_filenames_uses_usecase_plan_filename():
    filenames = get_filenames(Usecase.BFF)

    assert filenames["plan_filename"] == "file_format_plan.txt"
    assert filenames["storage_plan_filename"] == "file_format_plan.txt"

from synnodb.conversations.filenames import get_plan_filename
from synnodb.utils.cli_config import Usecase


def test_plan_filename_is_usecase_specific():
    assert get_plan_filename(Usecase.OLAP) == "storage_plan.txt"

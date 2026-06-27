import random
from datetime import datetime

from synnodb.utils.utils import DBStorage


# conv modes enum
class ConvMode:
    STORAGE_PLAN = "storageplan"
    SCRIPTED = "scripted"
    BASE = "base"
    OPTIM = "optim"
    MAKE_MT = "mt"
    CHECK_SF = "checksf"


def generate_conv_name(
    conv_type: str,
    benchmark: str,
    queries_str: str,
    model: str,
    bespoke_storage: bool,
    db_storage: DBStorage,
) -> tuple[str, str]:

    # assemble conversation name
    assert conv_type in ConvMode.__dict__.values(), (
        f"Unknown conversation type {conv_type}"
    )

    # shorten model name for better readability in conversation name
    if "claude" in model:
        # strip claude
        assert model.startswith("anthropic/claude-")
        model_name = model[len("anthropic/claude-") :]
    else:
        model_name = model

    # prune / - from model name to avoid issues with conversation name parsing and readability
    model_name = model_name.replace("/", "-").replace("_", "-")

    date_time_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    assert db_storage is not None, "db_storage must be provided to generate_conv_name"
    suffix = f"_{db_storage.value.lower()}"

    if bespoke_storage and conv_type != ConvMode.STORAGE_PLAN:
        suffix += "_bstorage"

    rnd_nr = random.randint(1000, 9999)
    conv_name = f"{benchmark}_{conv_type}_q{queries_str}_{model_name}{suffix}"
    conv_name_withdatetime = conv_name + f"_{date_time_str}_{rnd_nr}"
    return conv_name, conv_name_withdatetime

import random
from datetime import datetime

from synnodb.utils.utils import DBStorage


def generate_conv_name(
    stage_name: str,
    benchmark: str,
    query_subset: str | None,
    model: str,
    bespoke_storage: bool,
    db_storage: DBStorage,
) -> tuple[str, str]:

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

    if bespoke_storage and stage_name != "createStoragePlan":
        suffix += "_bstorage"

    rnd_nr = random.randint(1000, 9999)
    # No query subset (None = every registered query) -> no q-segment in the name.
    q_part = f"_q{query_subset}" if query_subset is not None else ""
    conv_name = f"{benchmark}_{stage_name}{q_part}_{model_name}{suffix}"
    conv_name_withdatetime = conv_name + f"_{date_time_str}_{rnd_nr}"
    return conv_name, conv_name_withdatetime

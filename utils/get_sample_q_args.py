import random
from pathlib import Path

from tools.validate.query_validator_class import QueryValidator
from utils.sf_list_gen import gen_sf
from utils.utils import DBStorage
from workloads.dataset.dataset_tables_dict import get_dataset_name
from workloads.dataset.query_gen_factory import get_placeholders_fn, get_query_gen


def get_sample_query_args(args, seed=42):
    workspace_path = Path("./output")
    workspace_path.mkdir(exist_ok=True)

    cache_path = Path(args.artifacts_dir) / "cache"

    snapshotter = None

    # prepare query gen
    gen_query_fn = get_query_gen(args.benchmark)
    gen_placeholders_fn = get_placeholders_fn(
        args.benchmark,
        do_not_cache=args.do_not_cache,
        cache_dir=cache_path / "placeholders_cache",
    )
    parquet_path = args.artifacts_dir + f"/{get_dataset_name(args.benchmark)}_parquet/"

    query_list = [q.strip() for q in args.query_list.split(",")]

    # assemble default sf values for the selected benchmark
    verify_sf_list, max_scale_factor = gen_sf(args.benchmark)

    query_validator = QueryValidator(
        benchmark=args.benchmark,
        gen_query_fn=gen_query_fn,
        sf_list=verify_sf_list + [max_scale_factor],
        parquet_path=parquet_path,
        wandb_pin_worker=True,
        all_query_ids=query_list,
        num_random_query_instantiations=10,
        query_cache_dir=cache_path / "query_cache",
        validate_cache_dir=cache_path / "validate",
        workspace_path=workspace_path,
        git_snapshotter=snapshotter,
        db_storage=DBStorage.IN_MEMORY,
    )

    sample_arg_list_dict = {}

    rnd = random.Random(seed)

    for query_id in query_list:
        # get query instantiations and convert to arg list
        args_list, instantiations, num_queries = (
            query_validator._get_instantiations_and_convert_to_arg_list(
                scale_factor=verify_sf_list[-1],
                query_id=[query_id],
                repetitions=1,
                trace_mode=False,
            )
        )

        # pick a random entry from args_list
        sample_arg_list_dict[query_id] = rnd.choice(args_list)

    return sample_arg_list_dict

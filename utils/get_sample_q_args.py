import random
from collections import defaultdict
from pathlib import Path

from tools.run_tool_mode import RunToolMode
from workloads.workload_provider import WorkloadProvider


def get_sample_query_args(args, workload_provider: WorkloadProvider, seed=42):
    workspace_path = Path("./output")
    workspace_path.mkdir(exist_ok=True)

    query_ids = workload_provider.query_ids

    # generate a batch with all
    query_batch = workload_provider.produce_workload(
        run_mode=RunToolMode.FAST_CHECK, num_threads=1, core_ids=None, query_ids=None
    )
    query_batch = query_batch[0]  # use the first: we don't care about SFs, ...

    sample_arg_list_dict = defaultdict(list)

    # split the batch into individual queries and pick a random arg list for each query
    for query in query_batch.query_list:
        qid_str = query.query_id
        sample_arg_list_dict[qid_str].append(query.query_args)

    rnd = random.Random(seed)
    final_sample_arg_dict = {}
    for qid, arg_list in sample_arg_list_dict.items():
        final_sample_arg_dict[qid] = rnd.choice(arg_list)

    assert set(final_sample_arg_dict.keys()) == set(query_ids), (
        f"Expected to have sample args for all query ids {query_ids}, but got {list(final_sample_arg_dict.keys())}"
    )

    return final_sample_arg_dict

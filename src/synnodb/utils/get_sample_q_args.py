import random
from collections import defaultdict

from synnodb.tools.run_tool_mode import RunToolMode
from synnodb.workloads.workload_provider import WorkloadProvider, format_sample_args


def get_sample_query_args(workload_provider: WorkloadProvider, seed=42):
    query_ids = workload_provider.query_ids

    # generate a batch with all
    query_batch_list = workload_provider.produce_workload(
        run_mode=RunToolMode.FAST_CHECK, num_threads=1, core_ids=None, query_ids=None
    )
    query_batch = query_batch_list[0]  # use the first: we don't care about SFs, ...

    sample_arg_list_dict = defaultdict(list)

    # split the batch into individual queries and pick a random arg list for each query.
    # Render placeholder values without the execution-time req_id so the LLM prompt
    # only ever sees a deterministic example instantiation of the query placeholders.
    for query in query_batch.query_list:
        qid_str = query.query_id
        sample_arg_list_dict[qid_str].append(
            format_sample_args(qid_str, query.placeholders)
        )

    rnd = random.Random(seed)
    final_sample_arg_dict = {}
    for qid, arg_list in sample_arg_list_dict.items():
        final_sample_arg_dict[qid] = rnd.choice(arg_list)

    assert set(final_sample_arg_dict.keys()) == set(query_ids), (
        f"Expected to have sample args for all query ids {query_ids}, but got {list(final_sample_arg_dict.keys())}"
    )

    return final_sample_arg_dict


def get_sample_exec_settings(workload_provider: WorkloadProvider):
    # generate a batch with all
    query_batch_list = workload_provider.produce_workload(
        run_mode=RunToolMode.BENCHMARK, num_threads=1, core_ids=None, query_ids=None
    )
    return query_batch_list[0].exec_settings

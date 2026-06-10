# add parent to path
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


from pipeline.tools.validate.query_cache import QueryCache

if __name__ == "__main__":
    scale_factor = 20
    queries = [1, 2, 3]
    num_samples = 10

    artifacts_dir = "/mnt/labstore/bespoke_olap/"
    cache_path = Path(artifacts_dir) / "cache/validate_tool"

    query_cache = QueryCache(
        query_ids=queries,
        sf_list=[scale_factor],
        num_instantiations_per_query=num_samples,
        duckdb_managers=None,
        cache_dir=cache_path / "query_cache",
        only_from_cache=True,
        gen_query_fn=None,
    )

    # Sample query instantiations from cache
    instantiations = query_cache.get_instantiations(
        scale_factor=scale_factor,
        query_id=queries,
        num_samples=num_samples,
    )

    # Prepare arguments for implementation
    args_list = []
    for inst in instantiations:
        tmp_vals = " ".join([f'"{v}"' for v in inst.placeholders.values()])
        args_list.append(f"{inst.query_id} {tmp_vals}")

    cmd = f"./db {scale_factor}"
    stdin_data = "\n".join(args_list) + "\n"

    cmd_with_input = f"printf {repr(stdin_data)} | {cmd}"
    print(f"Command to run: {cmd_with_input}")

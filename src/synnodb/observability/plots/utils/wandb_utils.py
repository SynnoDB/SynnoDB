import pickle
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from synnodb.utils.utils import (
    create_dir_and_set_permissions,
    dump_pickle,
    load_pickle,
    sha256,
    stable_json,
)

# W&B history column carrying the scale factor of each validation row. Runs no
# longer sweep a fixed per-benchmark scale factor; instead each validation logs
# the scale factor it ran at under this key (correctness is checked across
# several cheapest-first rungs, e.g. 0.02 / 0.1 / 1.0).
SCALE_FACTOR_COL = "validation/_scale_factor"


def _resolve_wandb_entity_project(
    entity: str | None, project: str | None
) -> tuple[str, str]:
    from synnodb.settings import get_wandb_entity_project

    return get_wandb_entity_project(entity, project)


def get_wandb_run(
    run_id: str,
    entity: str | None = None,
    project: str | None = None,
):
    import wandb

    entity, project = _resolve_wandb_entity_project(entity, project)
    api = wandb.Api()
    # With no entity, use the 2-part "project/run_id" form so wandb resolves the
    # caller's own default entity instead of a hardcoded one.
    path = f"{entity}/{project}/{run_id}" if entity else f"{project}/{run_id}"
    return api.run(path)


def get_wandb_snapshot_hash(summary: dict) -> str:
    return summary.get("code/snapshot_hash") or summary.get("current_hash") or "N/A"


def get_wandb_max_scale_factor(history: pd.DataFrame) -> Optional[int]:
    if SCALE_FACTOR_COL not in history.columns:
        return None

    scale_factors = history[SCALE_FACTOR_COL].dropna()
    if scale_factors.empty:
        return None

    return int(scale_factors.max())


def get_wandb_latest_query_runtimes(run, scale_factor: int) -> pd.DataFrame:
    table_art_list = [
        artifact
        for artifact in run.logged_artifacts()
        if f"sf{scale_factor}_all_queries_data" in artifact.name
    ]
    assert len(table_art_list) > 0, (
        f"No speedup measurements found for scale factor {scale_factor} in run {run.id} / {run.name}"
    )

    table_art_list.sort(key=lambda artifact: artifact.created_at, reverse=True)
    table = table_art_list[0].get(f"validation/sf{scale_factor}_all_queries_data")
    return table.get_dataframe()


def get_wandb_stats(
    run_id: str,
    entity: str | None = None,
    project: str | None = None,
    samples: int = 10000,
    skip_cache: bool = False,
    wandb_run_cache_path: Optional[Path] = None,
):
    """
    Fetch W&B run data with error handling.

    Note: wandb is imported on first use to avoid compatibility issues.

    Args:
        run_id: W&B run ID
        entity: W&B entity/workspace name
        project: W&B project name
        samples: Number of history samples to retrieve

    Returns:
        Tuple of (summary_dict, history_dataframe)
    """

    entity, project = _resolve_wandb_entity_project(entity, project)
    hash_payload = {"entity": entity, "project": project, "run_id": run_id}
    hash = sha256(stable_json(hash_payload))
    if wandb_run_cache_path is None:
        cache_path_summary = None
        cache_path_history = None
        cache_path_config = None
    else:
        # create cache dir if needed
        create_dir_and_set_permissions(wandb_run_cache_path)
        cache_path_summary, cache_path_history, cache_path_config = (
            _cache_path_for_hash(wandb_run_cache_path, hash)
        )

    # check compile cache - replay compile result from cache if available
    if (
        not skip_cache
        and cache_path_summary is not None
        and cache_path_summary.exists()
    ):
        assert cache_path_history is not None
        assert cache_path_config is not None
        with ProcessPoolExecutor(max_workers=3) as executor:
            f_summary = executor.submit(load_pickle, cache_path_summary, dict)
            f_history = executor.submit(pd.read_parquet, cache_path_history)
            f_config = executor.submit(load_pickle, cache_path_config, dict)
        summary, history, config = (
            f_summary.result(),
            f_history.result(),
            f_config.result(),
        )

        # sort history columns aplphabetically
        assert isinstance(history, pd.DataFrame)
        history = history.reindex(sorted(history.columns), axis=1)

        print(f"Loaded wandb data from cache: {cache_path_summary}")

        return summary, history, config

    try:
        run = get_wandb_run(run_id, entity, project)

        print(f"✓ Run loaded: {run.name}")
        print(f"  State: {run.state}")
        print(f"  Created: {run.created_at}")

        summary = dict(run.summary)
        summary["_run_name"] = run.name
        history = run.history(samples=samples)
        config = dict(run.config)

        # sort history columns aplphabetically
        assert isinstance(history, pd.DataFrame)
        history = history.reindex(sorted(history.columns), axis=1)

        print(f"✓ Data fetched: {len(history)} turns, {len(history.columns)} columns")

        # store output in cache
        if cache_path_summary is not None:
            for key in ["_wandb"]:
                summary.pop(key, None)  # remove non-serializable entry

            for key, value in list(summary.items()):
                # check if artifact reference (SummarySubDict raises KeyError in hasattr)
                try:
                    path = value.path
                    summary[key] = str(path)
                except (AttributeError, KeyError):
                    pass
                # drop values that can't be pickled (e.g. wandb objects with thread locks)
                try:
                    pickle.dumps(value)
                except Exception:
                    summary.pop(key, None)

            dump_pickle(
                cache_path_summary, summary, do_not_cache=False, assert_not_exists=False
            )
            print(f"✓ W&B data cached to: {cache_path_summary}")

            # Replace infinite values with None for parquet compatibility
            history = history.replace("Infinity", float("inf"))
            history.to_parquet(cache_path_history)

            # set to 777
            assert cache_path_history is not None
            try:
                cache_path_history.chmod(0o777)
            except Exception:
                pass

            assert cache_path_config is not None
            dump_pickle(
                cache_path_config, config, do_not_cache=False, assert_not_exists=False
            )

        return summary, history, config

    except Exception as e:
        print(f"✗ Error loading W&B data: {e}")
        # return summary, history
        raise e


def _cache_path_for_hash(cache_dir: Path, hash: str) -> Tuple[Path, Path, Path]:
    return (
        cache_dir / f"{hash}.pkl",
        cache_dir / f"{hash}_hist.parquet",
        cache_dir / f"{hash}_config.pkl",
    )


def combine_histories(hists: List) -> pd.DataFrame:
    # conitue counting steps across runs
    combined_parts = []
    step_offset = 0
    runtime_offset = 0
    dollar_offset = 0

    for hist in hists:
        if "total/runtime" not in hist.columns and "total_runtime" not in hist.columns:
            continue  # skip runs that don't have runtime info (e.g. base impl runs before 3/16/2026)

        if "total/runtime" not in hist.columns:
            # backward compatibility: if total/runtime doesn't exist, use total_runtime (changed to total/runtime on 3/16/2026)
            assert "total_runtime" in hist.columns, (
                "Expected 'total/runtime' or 'total_runtime' column in history"
            )
            hist["total/runtime"] = hist["total_runtime"]

        part = hist.copy()

        # ensure columns are identical: _step, turn
        assert part["turn"].equals(part["_step"]), (
            "Expected 'turn' and '_turn' columns to be identical"
        )

        # add offset to cols
        part["_step"] = part["_step"] + step_offset
        part["total/runtime"] = part["total/runtime"] + runtime_offset
        part["total/cost_usd"] = part["total/cost_usd"] + dollar_offset

        part["turn"] = part["_step"]
        combined_parts.append(part)

        step_offset += hist["turn"].max()

        runtime_offset += hist["total/runtime"].max()
        dollar_offset += hist["total/cost_usd"].max()

    combined = pd.concat(combined_parts, ignore_index=True)

    print(
        f"Combined history has {len(combined)} rows ({[len(part) for part in combined_parts]})"
    )

    return combined

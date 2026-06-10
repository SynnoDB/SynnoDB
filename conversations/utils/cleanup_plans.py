import json


def cleanup_numeric_value(value: float) -> float | str:
    # show everything until the first 2 non zero digits, then round to that
    if value == 0:
        return str(value)

    import math

    magnitude = math.floor(math.log10(abs(float(value))))
    decimal_places = -(magnitude - 1)
    rounded = round(float(value), decimal_places)
    if decimal_places > 0:
        return f"{rounded:.{decimal_places}f}"
    return str(int(rounded))


def cleanup_duckdb_plan(plan: dict, keep_storage_info: bool = False) -> str:
    """
    Cleans up a DuckDB plan by removing unnecessary details and simplifying the structure.
    Reduces token consumption by ~2x

    Args:
        plan (dict): The original DuckDB plan.

    Returns:
        str: The cleaned up DuckDB plan as a JSON string.
    """
    # Implementation for cleaning up DuckDB plan

    keys_blacklist = [
        "attach_load_storage_latency",
        "attach_replay_wal_latency",
        "blocked_thread_time",
        "checkpoint_latency",
        "commit_local_storage_latency",
        "cpu_time",  # do not show aggregated time
        "cumulative_cardinality",
        "cumulative_rows_scanned",
        "Delim Index",
        "Estimated Cardinality",
        "operator_name",  # this is redundant with the operator-type entry
        "query_name",
        "result_set_size",  # bytes is not interesting
        "rows_returned",
        "Type",  # redundant: this is in operator_name already covered
        "waiting_to_attach_latency",
        "wal_replay_entry_count",
        "write_to_wal_latency",
    ]

    if not keep_storage_info:
        keys_blacklist += [
            "system_peak_buffer_memory",
            "system_peak_temp_dir_size",
            "total_bytes_written",
            "total_bytes_read",
            "total_memory_allocated",
        ]

    # Recursively clean the plan
    def clean_plan_node(node: dict) -> dict:
        if not isinstance(node, dict):
            return node

        cleaned_node = {}
        for key, value in node.items():
            # do rewrites
            if key == "Table":
                if value.startswith("memory.main."):
                    value = value[len("memory.main.") :]

            elif key in ["operator_timing", "cpu_time"]:
                # don't show that many digits
                # show the first 2 non zero digits
                value = cleanup_numeric_value(value)
            elif key == "Projections":
                if isinstance(value, list):
                    # filter out #123 and __internal_decompress_string entries
                    proj_list = []
                    for item in value:
                        assert isinstance(item, str)
                        if (
                            item.startswith("#")
                            or item.startswith("__internal_decompress")
                            or item.startswith("__internal_compress")
                        ):
                            continue
                        proj_list.append(item)

                    if len(proj_list) == 0:
                        # only if all projections are filtered out, otherwise we keep the original list
                        value = proj_list
                else:
                    assert isinstance(value, str)
                    if value.startswith("#") or value.startswith(
                        "__internal_decompress_string"
                    ):
                        # filter out the projection
                        value = []

            # do filtering
            if key in keys_blacklist:
                # skip this entry
                continue

            elif key == "children" and len(value) == 0:  # type: ignore[arg-type]
                # skip empty children
                continue
            elif key == "extra_info" and isinstance(value, dict) and len(value) == 0:
                # skip empty extra_info
                continue
            elif key == "operator_rows_scanned" and value == 0:
                # skip operator_rows_scanned if it's 0
                continue
            elif key == "Projections" and isinstance(value, list) and len(value) == 0:
                # skip empty projections
                continue

            elif isinstance(value, dict):
                cl = clean_plan_node(value)
                if len(cl) > 0:
                    cleaned_node[key] = cl
            elif isinstance(value, list):
                cleaned_node[key] = [clean_plan_node(item) for item in value]
            else:
                cleaned_node[key] = value

        return cleaned_node

    cleaned_plan = clean_plan_node(plan)
    return json.dumps(cleaned_plan, indent=2)


def cleanup_umbra_plan(plan: dict) -> str:
    # Implementation for cleaning up DuckDB plan

    keys_blacklist = [
        "analyzePlanId",
        "analyzePlanPipelines",
        "cardinality",  # estimated?
        "forcedestimate",
        "ius",
        "operatorId",
        "operator",
        "rowstate",
        "schema",
        "table",  # use tablename
        "tid",
        "tableoid",
    ]

    # Recursively clean the plan
    def clean_plan_node(node: dict) -> dict:
        if not isinstance(node, dict):
            return node

        cleaned_node = {}
        for key, value in node.items():
            # do filtering

            if key in keys_blacklist:
                # skip this entry
                continue
            elif isinstance(value, str) and value == "":
                # skip empty strings
                continue

            elif isinstance(value, dict):
                cl = clean_plan_node(value)
                if len(cl) > 0:
                    cleaned_node[key] = cl
            elif isinstance(value, list):
                if len(value) > 0:
                    cleaned_node[key] = [clean_plan_node(item) for item in value]
            else:
                cleaned_node[key] = value

        return cleaned_node

    cleaned_plan = clean_plan_node(plan)
    return json.dumps(cleaned_plan, indent=2)

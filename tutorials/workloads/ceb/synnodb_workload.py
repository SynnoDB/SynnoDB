"""The built-in CEB (IMDB/JOB) workload, expressed as a
:class:`~synnodb.workloads.workload_spec.WorkloadSpec` and registered into the core registry
from the outside.

The core ``synnodb`` package is workload-agnostic and ships no concrete workload; this module
owns everything CEB-specific - the SQL templates, the IMDB schema DDL, the on-disk query
parameter generation (and its disk cache), and the fuzzy query-range expander (``"2-9"`` ->
``2a..9b``) that travels on the spec. Import this module and call :func:`register` (or read
:data:`CEB_SPEC`) before running the pipeline against ``"ceb"``.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any, List

from synnodb import settings
from synnodb.utils import utils
from synnodb.workloads.workload_provider_olap import (
    PlaceholdersCacheType,
    _cache_path_for_hash,
)
from synnodb.workloads.workload_spec import WorkloadSpec, register_workload

from tutorials.workloads.ceb.ceb_queries import ceb_templates

logger = logging.getLogger(__name__)


def _ceb_query_dir() -> Path:
    """CEB query-template directory, resolved lazily so importing this module
    needs no SYNNO_DATA_DIR (config is resolved on first use via settings)."""
    return settings.get_data_dir() / "workloads" / "ceb" / "queries"


def _ceb_schema() -> str:
    from tutorials.workloads.ceb.imdb_schema import imdb_schema

    return imdb_schema


def _ceb_query_gen_factory(provider: Any):
    from tutorials.workloads.ceb.gen_ceb_query import gen_query_single_only

    return functools.partial(gen_query_single_only, ceb_dir=_ceb_query_dir())


def _ceb_placeholders_factory(provider: Any, do_not_cache: bool = False):
    from tutorials.workloads.ceb.gen_ceb_query import gen_query_single_only

    def gen_placeholder_ceb(**kwargs):
        # placeholders are loaded from disk; cache them per query to avoid re-reading
        hash_payload = {"benchmark": "ceb", "query_name": kwargs["query_name"]}
        stable_payload = utils.stable_json(hash_payload)
        hash = utils.sha256(stable_payload)

        if provider.query_cache_dir is None:
            cache_path = None
        else:
            utils.create_dir_and_set_permissions(provider.query_cache_dir)
            cache_path = _cache_path_for_hash(provider.query_cache_dir, hash)

        if cache_path is not None and cache_path.exists():
            cached: PlaceholdersCacheType | None = utils.load_pickle(
                cache_path, PlaceholdersCacheType
            )
            assert cached is not None
            logger.debug(f"Loaded placeholders from cache: {cache_path}")
            return cached.placeholders

        placeholders = gen_query_single_only(**kwargs, ceb_dir=_ceb_query_dir())[2]

        if cache_path is not None and not do_not_cache:
            utils.dump_pickle(
                cache_path,
                PlaceholdersCacheType(
                    placeholders=placeholders, hash_payload=stable_payload
                ),
                do_not_cache=do_not_cache,
            )

        return placeholders

    return gen_placeholder_ceb


def _parse_ceb_fuzzy_range(
    start_q: str, end_q: str, ceb_query_order: list[str]
) -> List[str]:
    """Expand a CEB query-range short name whose endpoints are not exact catalog ids
    (e.g. ``"2-9"`` -> ``2a..9b``). Carried on :data:`CEB_SPEC` as its
    ``query_range_expander`` so the core stays workload-agnostic."""

    def parse_qstr(q: str, is_start: bool) -> str:
        if len(q) == 1:
            assert q.isdigit()
            q = f"0{q}a"
        elif len(q) == 2:
            if q[0].isdigit() and q[1].isdigit():
                if is_start:
                    q = f"{q}a"
                else:
                    # upper bound: append z
                    q = f"{q}z"
            elif q[0].isdigit() and q[1].isalpha():
                # prepend 0
                q = f"0{q}"
            else:
                raise Exception(f"Could not parse start query {q}")
        elif len(q) == 3:
            assert q[0].isdigit() and q[1].isdigit() and q[2].isalpha()
        else:
            raise Exception(f"Could not parse start query {q}")
        return q

    start_q = parse_qstr(start_q, is_start=True)
    end_q = parse_qstr(end_q, is_start=False)

    assert len(start_q) == 3, f"start_q: {start_q}"
    assert len(end_q) == 3, f"end_q: {end_q}"

    queries = []
    for q in ceb_query_order:
        q_str = f"{q}"
        if len(q) == 2:
            q_str = "0" + q_str

        assert len(q_str) == 3, f"q_str: {q_str}"
        assert q_str[0].isdigit() and q_str[1].isdigit() and q_str[2].isalpha(), (
            f"q_str: {q_str}"
        )

        if q_str >= start_q and q_str <= end_q:
            queries.append(q)

    return queries


CEB_SPEC = WorkloadSpec(
    name="ceb",
    tables=(
        "aka_name",
        "aka_title",
        "cast_info",
        "char_name",
        "comp_cast_type",
        "company_name",
        "company_type",
        "complete_cast",
        "info_type",
        "keyword",
        "kind_type",
        "link_type",
        "movie_companies",
        "movie_info",
        "movie_info_idx",
        "movie_keyword",
        "movie_link",
        "name",
        "person_info",
        "role_type",
        "title",
    ),
    dataset_name="imdb",
    all_query_ids=(
        "1a",
        "2a",
        "2b",
        "2c",
        "3a",
        "3b",
        "4a",
        "5a",
        "6a",
        "7a",
        "8a",
        "9a",
        "9b",
        "10a",
        "11a",
        "11b",
    ),
    benchmark_sf=5,
    fast_check_sfs=(0.25, 0.5),
    exhaustive_sfs=(0.25, 0.5, 5),
    ingest_sfs=(5,),
    example_query="Q42a",
    example_query_params="42a",
    schema_example_table="title",
    sql_dict_factory=lambda: ceb_templates,
    schema_factory=_ceb_schema,
    query_gen_factory=_ceb_query_gen_factory,
    placeholders_factory=_ceb_placeholders_factory,
    # CEB's dataset was regenerated; bump to invalidate stale cache entries.
    dataset_version="3",
    large_check_sf=10,
    query_range_expander=_parse_ceb_fuzzy_range,
)


def register() -> WorkloadSpec:
    """Register the built-in CEB workload into the core registry. Idempotent."""
    register_workload(CEB_SPEC)
    return CEB_SPEC

"""The Rust workspace scaffold is well-formed.

Cheap structural checks only -- actually building it pulls in arrow-rs and takes
minutes, which belongs in the conformance/e2e suites, not here. What this pins
down is the stuff that is easy to break silently while refactoring the
generators:

  * the model gets exactly the files it is told to edit, and nothing else is
    writable;
  * the ABI export lands in exactly one crate per plugin (three plugin_query
    definitions in one .so is a link error that only shows up at cargo time);
  * the dispatch and the arg structs actually cover the run's query set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synnodb.conversations.filenames import Filenames
from synnodb.cpp_runner.prepare_repo.prepare_features import PrepareFeatures
from synnodb.cpp_runner.prepare_repo.prepare_workspace_rust import RustPrepareWorkspace
from synnodb.utils.utils import DBStorage, EngineLang
from synnodb.workloads.workload_provider_olap import OLAPWorkload, OLAPWorkloadProvider

QUERY_IDS = ["1", "6"]


@pytest.fixture(scope="module")
def scaffold(tmp_path_factory) -> dict[str, str]:
    from synnodb import settings

    try:
        parquet_root = (
            settings.get_data_dir() / "workloads" / "tpch" / "tpch_parquet"
        )
    except RuntimeError:
        pytest.skip("no data dir configured")

    provider = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=parquet_root,
        db_storage=DBStorage.IN_MEMORY,
        query_ids=QUERY_IDS,
    )
    prep = RustPrepareWorkspace(
        workload_provider=provider,
        workspace_dir=tmp_path_factory.mktemp("rust_ws"),
        git_snapshotter=None,
        db_storage=DBStorage.IN_MEMORY,
    )
    features = PrepareFeatures(language="rust").resolve(DBStorage.IN_MEMORY)
    return prep.build_scaffold_files(features)


def test_emits_a_cargo_workspace(scaffold):
    assert "Cargo.toml" in scaffold
    # Without this, rustc emits no GNU build-id, the host's needs_reload() compares
    # "" == "" and always answers false, and a recompiled engine is never reloaded.
    assert "--build-id=sha1" in scaffold[".cargo/config.toml"]


def test_model_owns_exactly_the_builder_and_the_query_files(scaffold):
    names = Filenames.for_usecase(language=EngineLang.RUST)
    writable = set(scaffold) - RustPrepareWorkspace._readonly_scaffold_files()
    # queries.md is read-only-but-tracked, and is not a source file.
    writable.discard("queries.md")

    expected = {names.builder_path} | {names.query_file(q) for q in QUERY_IDS}
    assert writable == expected


def test_the_abi_export_lands_in_exactly_one_crate_per_plugin(scaffold):
    """plugin_query is #[no_mangle]. The query crate statically links builder and
    loader for their types, so if the export lived in those crates too, linking
    libquery.so would fail on a duplicate symbol -- which is exactly what happened
    before the shims were split out. Keep it in the shims only."""
    exporters = {
        name for name, src in scaffold.items() if "pub extern \"C\" fn plugin_query" in src
    }
    assert exporters == {
        "plugins/loader/src/lib.rs",
        "plugins/builder/src/lib.rs",
        "plugins/query/src/lib.rs",
    }


def test_dispatch_and_args_cover_the_query_set(scaffold):
    lib = scaffold["query/src/lib.rs"]
    args = scaffold["query/src/args.rs"]
    for qid in QUERY_IDS:
        assert f"pub mod q{qid};" in lib
        assert f'"{qid}" => args::parse_q{qid}' in lib
        assert f"pub struct Q{qid}Args" in args
        assert f"pub fn parse_q{qid}" in args


def test_loader_declares_every_dataset_table(scaffold):
    loader = scaffold["loader/src/lib.rs"]
    for table in ("lineitem", "orders", "customer", "part", "supplier", "nation", "region"):
        assert f"pub {table}: std::sync::Arc<RecordBatch>," in loader
        assert f'{table}: read_parquet_table(&format!("{{path}}{table}.parquet"))?,' in loader


def test_ssd_is_refused_rather_than_silently_wrong(scaffold, tmp_path):
    """The Rust engine has no buffer-pool scaffold yet. Selecting SSD must fail
    loudly instead of handing back an in-memory scaffold for a persistent run."""
    from synnodb import settings

    provider = OLAPWorkloadProvider(
        benchmark=OLAPWorkload.TPCH,
        base_parquet_dir=settings.get_data_dir() / "workloads" / "tpch" / "tpch_parquet",
        db_storage=DBStorage.SSD,
        query_ids=QUERY_IDS,
    )
    prep = RustPrepareWorkspace(
        workload_provider=provider,
        workspace_dir=tmp_path,
        git_snapshotter=None,
        db_storage=DBStorage.SSD,
    )
    features = PrepareFeatures(language="rust").resolve(DBStorage.SSD)
    with pytest.raises(NotImplementedError, match="in-memory"):
        prep.build_scaffold_files(features)

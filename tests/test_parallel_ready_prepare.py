from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from synnodb.conversations.examples import add_mt as mt_builder
from synnodb.conversations.filenames import Filenames
from synnodb.cpp_runner.prepare_repo.assemble_query_impl import assemble_query_impl_file
from synnodb.cpp_runner.prepare_repo.prepare_features import (
    PrepareFeatures,
    apply_prepare_features,
)
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.utils.utils import DBStorage


def test_in_memory_base_prepare_enables_thread_pool_but_ssd_base_does_not():
    calls = []

    def fake_assemble(features, **kwargs):
        calls.append(features)
        return SimpleNamespace(
            artifacts_str="",
            tracked_files={},
            readonly_files_not_git_tracked={},
            tracked_artifacts_str="",
            readonly_artifacts_str="",
        )

    provider = SimpleNamespace(
        assemble=fake_assemble,
        write_prepared_files=lambda _part, write_tracked=True: None,
    )

    # base features leave parallel_ready_impl on "auto": in-memory resolves to
    # a parallel-ready scaffold, SSD does not
    apply_prepare_features(
        PrepareFeatures.base().resolve(DBStorage.IN_MEMORY),
        provider,
        source_features=None,
    )
    assert calls[-1].parallel_ready_impl is True

    apply_prepare_features(
        PrepareFeatures.base().resolve(DBStorage.SSD),
        provider,
        source_features=None,
    )
    assert calls[-1].parallel_ready_impl is False


def test_thread_pool_headers_are_readonly_workspace_artifacts():
    ro_files, tracked_files = PrepareWorkspace._get_readonly_files()
    assert "thread_pool.hpp" in ro_files
    assert "query_pool.hpp" in ro_files
    assert "thread_pool.hpp" not in tracked_files
    assert "query_pool.hpp" not in tracked_files

    templates = Path("src/synnodb/cpp_runner/prepare_repo/templates")
    assert "// FILE_VERSION:" in (templates / "thread_pool.hpp").read_text()
    assert "// FILE_VERSION:" in (templates / "query_pool.hpp").read_text()


def test_query_impl_pool_wiring_does_not_enable_trace_by_itself():
    query_impl = assemble_query_impl_file(
        add_thread_pool_to_query_impl=True,
        tracing=False,
        add_sample_trace_to_query_impl=False,
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=False,
    )

    assert '#include "thread_pool.hpp"' in query_impl
    assert "ThreadPool& get_query_pool()" in query_impl
    assert "(void)get_query_pool();" in query_impl
    assert "pin_process_to_cpu(3)" not in query_impl
    assert '#include "trace.hpp"' not in query_impl
    assert "trace_get_and_clear()" not in query_impl


def test_query_impl_pool_wiring_remains_trace_compatible():
    query_impl = assemble_query_impl_file(
        add_thread_pool_to_query_impl=True,
        tracing=False,
        add_sample_trace_to_query_impl=True,  # sample counting implies the wiring
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=False,
    )

    assert '#include "thread_pool.hpp"' in query_impl
    assert '#include "trace.hpp"' in query_impl
    assert "TRACE_RESET();" in query_impl
    assert "TRACE_FLUSH();" in query_impl
    assert "trace_get_and_clear()" in query_impl


def test_query_impl_tracing_wires_instrumentation_without_sample_counts():
    query_impl = assemble_query_impl_file(
        add_thread_pool_to_query_impl=False,
        tracing=True,
        add_sample_trace_to_query_impl=False,
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=False,
    )

    assert '#include "trace.hpp"' in query_impl
    assert "TRACE_RESET();" in query_impl
    assert "TRACE_FLUSH();" in query_impl
    assert "trace_get_and_clear()" in query_impl
    assert "SAMPLE_TRACE_" not in query_impl


def test_query_impl_flush_caches_wires_drop_before_each_run():
    query_impl = assemble_query_impl_file(
        add_thread_pool_to_query_impl=False,
        tracing=False,
        add_sample_trace_to_query_impl=False,
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=True,
    )

    # the helper is defined, called per run, and clears the buffer pool too
    assert "void drop_buffer_and_os_caches(Database* db)" in query_impl
    assert "drop_buffer_and_os_caches(db);" in query_impl
    assert "db->pool->clear();" in query_impl
    # no leftover template placeholders
    assert "<<drop_buffer_and_os_caches" not in query_impl
    assert "<<clear_buffer_pool_call>>" not in query_impl


def test_query_impl_without_flush_caches_omits_drop_helper_entirely():
    query_impl = assemble_query_impl_file(
        add_thread_pool_to_query_impl=False,
        tracing=False,
        add_sample_trace_to_query_impl=False,
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=False,
    )

    # the whole helper definition and its call are stripped, not just left dormant
    assert "drop_buffer_and_os_caches" not in query_impl
    assert "db->pool->clear();" not in query_impl
    assert "<<drop_buffer_and_os_caches" not in query_impl
    assert "<<clear_buffer_pool_call>>" not in query_impl


def test_in_memory_query_template_teaches_parallel_ready_arrow_output():
    template = Path(
        "src/synnodb/cpp_runner/prepare_repo/templates/olap/queryX.cpp"
    ).read_text()
    assert '#include "query_pool.hpp"' in template
    assert "parallel_reduce<Acc>" in template
    assert "CORE_IDS=1" in template
    assert "Do not create separate ST/MT code paths" in template
    assert "make_table" in template
    assert "decimal_column" in template


def _mt_ctx(db_storage: DBStorage):
    ctx = SimpleNamespace(
        bespoke_storage=True,
        persistent_storage=db_storage == DBStorage.SSD,
        filenames=Filenames.for_usecase(),
        single_threaded_rt_ms={"1": 100.0},
        sample_exec_settings=lambda: None,
    )
    return ctx


def test_in_memory_mt_conversation_tunes_instead_of_introducing_mt():
    ctx = _mt_ctx(DBStorage.IN_MEMORY)
    assert mt_builder._assemble_pre_stages(ctx, "constraints", "pretext") == []
    stages = mt_builder.build_query_stages(ctx, "1", "constraints", "pretext")
    descriptors = [s.descriptor for s in stages]
    assert descriptors == ["Optimize Parallel-Ready MT w. Trace (1)"]
    assert all("Introduce Multi-Threading" not in d for d in descriptors)
    assert all("Add ThreadPool" not in d for d in descriptors)


def test_ssd_mt_conversation_keeps_legacy_threadpool_intro():
    ctx = _mt_ctx(DBStorage.SSD)
    stages = mt_builder._assemble_pre_stages(ctx, "constraints", "pretext")
    descriptors = [s.descriptor for s in stages if hasattr(s, "descriptor")]
    assert descriptors == ["Add ThreadPool"]

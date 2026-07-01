from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from synnodb.cpp_runner.prepare_repo.assemble_query_impl import assemble_query_impl_file
from synnodb.cpp_runner.prepare_repo.prepare_olap import prepare_base
from synnodb.cpp_runner.prepare_repo.prepare_workspace import PrepareWorkspace
from synnodb.conversations.in_mem_2_mt_conv import InMem2MTConversation
from synnodb.conversations.ssd_2_mt_conv import SSD2MTOptConv
from synnodb.utils.utils import DBStorage


def test_in_memory_base_prepare_enables_thread_pool_but_ssd_base_does_not():
    calls = []

    def fake_prepare(**kwargs):
        calls.append(kwargs)
        return ""

    ctx = SimpleNamespace(
        prepare_workspace_provider=SimpleNamespace(
            db_storage=DBStorage.IN_MEMORY,
            prepare=fake_prepare,
        ),
        usecase_prepare_args={},
        write_non_tracked_only=False,
        only_from_cache=False,
        do_not_cache=True,
        add_sample_trace=False,
    )
    prepare_base(ctx)
    assert calls[-1]["usecase_args"]["add_thread_pool_to_query_impl"] is True

    ctx.prepare_workspace_provider.db_storage = DBStorage.SSD
    prepare_base(ctx)
    assert calls[-1]["usecase_args"]["add_thread_pool_to_query_impl"] is False


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
        add_sample_trace_to_query_impl=True,
        query_list=["1"],
        pin_to_core=3,
        drop_os_caches_for_each_query=False,
    )

    assert '#include "thread_pool.hpp"' in query_impl
    assert '#include "trace.hpp"' in query_impl
    assert "TRACE_RESET();" in query_impl
    assert "TRACE_FLUSH();" in query_impl
    assert "trace_get_and_clear()" in query_impl


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


def test_in_memory_mt_conversation_tunes_instead_of_introducing_mt():
    conv = object.__new__(InMem2MTConversation)
    conv.bespoke_storage = True
    conv.persistent_storage = False
    conv.file_paths = {"thread_pool_filename": "thread_pool.hpp"}
    conv.single_threaded_rt_ms = {"1": 100.0}

    assert conv._assemble_pre_stages("constraints", "pretext") == []
    stages = conv._build_stages("1", "constraints", "pretext")
    descriptors = [s.descriptor for s in stages]
    assert descriptors == ["Optimize Parallel-Ready MT w. Trace (1)"]
    assert all("Introduce Multi-Threading" not in d for d in descriptors)
    assert all("Add ThreadPool" not in d for d in descriptors)


def test_ssd_mt_conversation_keeps_legacy_threadpool_intro():
    conv = object.__new__(SSD2MTOptConv)
    conv.bespoke_storage = True
    conv.file_paths = {
        "builder_hpp_path": "db_loader.hpp",
        "thread_pool_filename": "thread_pool.hpp",
    }

    stages = conv._assemble_pre_stages("constraints", "pretext")
    descriptors = [s.descriptor for s in stages if hasattr(s, "descriptor")]
    assert descriptors == ["Add ThreadPool"]

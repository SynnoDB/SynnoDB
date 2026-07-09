import string
from pathlib import Path

_QUERY_IMPL_TEMPLATE = Path(__file__).parent / "templates" / "query_impl.cpp"


def assemble_query_impl_file(
    add_thread_pool_to_query_impl: bool,
    tracing: bool,
    add_sample_trace_to_query_impl: bool,
    query_list: list[str],
    pin_to_core: int,
    drop_os_caches_for_each_query: bool,
):
    # ``tracing`` wires the trace instrumentation (trace.hpp include, per-query
    # TRACE_RESET/FLUSH, trace emission into the query result). ``add_sample_
    # trace_to_query_impl`` additionally emits a sample TRACE_COUNT per query;
    # counting requires the wiring, so it implies it.
    wire_tracing = tracing or add_sample_trace_to_query_impl

    # read query impl file
    assert _QUERY_IMPL_TEMPLATE.is_file(), (
        f"Query impl template not found: {_QUERY_IMPL_TEMPLATE}"
    )
    query_impl_content = _QUERY_IMPL_TEMPLATE.read_text()

    impl_keyword = "// <<impl_fn_calls>>"
    assert impl_keyword in query_impl_content, (
        f"Keyword '{impl_keyword}' not found in query_impl.cpp template"
    )
    case_block, include_headers = gen_query_impl_query_select_block(
        query_list, add_sample_trace=add_sample_trace_to_query_impl
    )
    query_impl = query_impl_content.replace(impl_keyword, case_block)

    query_headers_kw = "// <<include_query_headers>>"
    assert query_headers_kw in query_impl, (
        f"Keyword '{query_headers_kw}' not found in query_impl.cpp template"
    )
    query_impl = query_impl.replace(query_headers_kw, include_headers)

    thread_pool_include_kw = "// <<thread_pool_include>>"
    thread_pool_placeholder_kw = "// <<get_thread_pool_placeholder>>"
    trace_include_kw = "// <<trace_include>>"

    if add_thread_pool_to_query_impl:
        query_impl = query_impl.replace(
            thread_pool_include_kw, '#include "thread_pool.hpp"'
        )
        query_impl = query_impl.replace(
            thread_pool_placeholder_kw,
            """
// ── Shared thread pool (get_query_pool) ───────────────────────────────────────
// Initialized once at program start; warm-up dispatches a no-op parallel_for
// so all worker threads are alive and spinning before the first query arrives.
ThreadPool& get_query_pool() {
    static struct Holder {
        ThreadPool pool;
        Holder() {
            init_thread_pool(pool);
            pool.parallel_for([](int, int) {});  // warm-up
        }
    } h;
    return h.pool;
}
""",
        )
    else:
        query_impl = query_impl.replace(thread_pool_include_kw, "")
        query_impl = query_impl.replace(thread_pool_placeholder_kw, "")

    if wire_tracing:
        query_impl = query_impl.replace(trace_include_kw, '#include "trace.hpp"')
        # emit the collected trace into the query result
        trace_kw = 'results.push_back(QueryResult{req.query_id, req.req_id, "", elapsed_ms, error});'
        trace_target = "results.push_back(QueryResult{req.query_id, req.req_id, trace_get_and_clear(), elapsed_ms, error});"
        assert trace_kw in query_impl, (
            f"Could not find '{trace_kw}' in query_impl.cpp template"
        )
        query_impl = query_impl.replace(trace_kw, trace_target)

        # activate the per-query TRACE_RESET/FLUSH the template carries commented
        trace_kw_list = ["TRACE_FLUSH();", "TRACE_RESET();"]
        for kw in trace_kw_list:
            query_impl = query_impl.replace(f"// {kw}", kw)
    else:
        query_impl = query_impl.replace(trace_include_kw, "")

    pin_thread_to_core_kw = "// <<pin_thread_to_core>>"
    assert pin_thread_to_core_kw in query_impl, (
        f"Keyword '{pin_thread_to_core_kw}' not found in query_impl.cpp template"
    )
    if add_thread_pool_to_query_impl:
        # Initialize once before the query loop. With CORE_IDS=1 this is a cheap
        # serial fast path; with multiple CORE_IDS it warms and pins the workers.
        query_impl = query_impl.replace(
            pin_thread_to_core_kw,
            """(void)get_query_pool();""",
        )
    else:
        query_impl = query_impl.replace(
            pin_thread_to_core_kw,
            f"""// Pin the process to CPU core {pin_to_core} for deterministic, low-noise performance measurements.
    pin_process_to_cpu({pin_to_core});""",
        )

    drop_caches_def_kw_start = "// <<drop_buffer_and_os_caches_def_start>>"
    drop_caches_def_kw_end = "// <<drop_buffer_and_os_caches_def_end>>"
    drop_caches_call_kw = "// <<drop_buffer_and_os_caches_call>>"
    buffer_pool_clear_kw = "// <<clear_buffer_pool_call>>"

    for kw in [
        drop_caches_def_kw_start,
        drop_caches_def_kw_end,
        drop_caches_call_kw,
        buffer_pool_clear_kw,
    ]:
        assert kw in query_impl, f"Keyword '{kw}' not found in query_impl.cpp template"

    if drop_os_caches_for_each_query:
        query_impl = query_impl.replace(
            drop_caches_call_kw, "\t\t\tdrop_buffer_and_os_caches(db);"
        )
        query_impl = query_impl.replace(buffer_pool_clear_kw, "\tdb->pool->clear();")

        # remove the start / end keywords since the function is now always defined
        query_impl = query_impl.replace(drop_caches_def_kw_start, "")
        query_impl = query_impl.replace(drop_caches_def_kw_end, "")

    else:
        query_impl = query_impl.replace(drop_caches_call_kw, "")
        query_impl = query_impl.replace(buffer_pool_clear_kw, "")

        # remove the entire function definition since it's not needed
        start_idx = query_impl.index(drop_caches_def_kw_start)
        end_idx = query_impl.index(drop_caches_def_kw_end) + len(drop_caches_def_kw_end)
        query_impl = query_impl[:start_idx] + query_impl[end_idx:]

    return query_impl


def gen_query_impl_query_select_block(
    query_ids: list[str], add_sample_trace: bool = False
) -> tuple[str, str]:
    """Emit the if/else dispatch chain that fills the <<impl_fn_calls>>
    placeholder in `query_impl.cpp`.

    The surrounding bookkeeping (try/catch, elapsed_ms, error capture, push_back)
    lives in the template, not here, so this function only produces the dispatch
    chain itself.  The placeholder sits at column 12 inside the template's
    try-block, so the first line emitted has no leading indent (the template
    provides it) and all subsequent lines carry their own 12-space indent.
    """
    indent = " " * 12
    body = " " * 16

    sample_trace_str = (
        '${body}TRACE_COUNT("SAMPLE_TRACE_${qid}",1);\n' if add_sample_trace else ""
    )

    case_template = string.Template(
        '${prefix}${kw} (req.query_id == "${qid}") {\n'
        "${body}Q${qid}Args args = parse_q${qid}(req);\n"
        "${body}auto start = std::chrono::steady_clock::now();\n"
        f"{sample_trace_str}"
        "${body}std::shared_ptr<arrow::Table> result = run_q${qid}(db, args);\n"
        "${body}auto end = std::chrono::steady_clock::now();\n"
        "${body}elapsed_ms = std::chrono::duration<double, std::milli>(end - start).count();\n"
        "${body}synnodb::write_result(result, req.req_id);\n"
        "${indent}}"
    )

    cases = []
    for i, qid in enumerate(query_ids):
        cases.append(
            case_template.substitute(
                # First case starts at the placeholder's column (already indented).
                prefix="" if i == 0 else indent,
                kw="if" if i == 0 else "else if",
                qid=qid,
                body=body,
                indent=indent,
            )
        )
    cases.append(
        f"{indent}else {{\n"
        f'{body}throw std::runtime_error("Unsupported query id: " + req.query_id);\n'
        f"{indent}}}"
    )
    case_str = "\n".join(cases)

    include_query_impl_list = [f'#include "query{qid}.hpp"' for qid in query_ids]

    return case_str, "\n".join(include_query_impl_list)

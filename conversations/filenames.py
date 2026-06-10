def get_filenames():
    queries_path = "queries.txt"
    builder_path = "db_loader.hpp/db_loader.cpp"
    builder_cpp_path = "db_loader.cpp"
    builder_hpp_path = "db_loader.hpp"
    query_impl_path = "query_impl.cpp"
    args_path = "args_parser.hpp"
    base_impl_todo_filename = "base_impl_todo.txt"
    storage_plan_filename = "storage_plan.txt"
    thread_pool_filename = "thread_pool.hpp"

    return {
        "queries_path": queries_path,
        "builder_path": builder_path,
        "builder_hpp_path": builder_hpp_path,
        "builder_cpp_path": builder_cpp_path,
        "query_impl_path": query_impl_path,
        "args_path": args_path,
        "base_impl_todo_filename": base_impl_todo_filename,
        "storage_plan_filename": storage_plan_filename,
        "thread_pool_filename": thread_pool_filename,
    }

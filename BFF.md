## Extend these files:

Query generation & datasets:
```
workloads/workload_provider_bff.py
```

Initial files in the workspace:
```
cpp_runner/prepare_repo/prepare_workspace_bff.py
```

CPP Header files (against which the LLM is programming):
```
cpp_runner/api/bff
```

Reference systems - DuckDB reading from parquet (prio) + hand coded Rust engine reading from parquet (low prio):
```
workloads/system_factory_bff.py
```

## TODO

Long:
- Generate random Queries in datasets (with llm) ~100 queries for single-table and multi-table with filters

Jigao:
- CPP Header for Ingest (write to our fileformat) & read (read from fileformat)

Johannes:
- Agent Stuff

Upcoming:
- Runner (single table)
- Investigate DuckDB Filesystem Plugin (and implement blueprint)
- Collect Expert Knowledge on what makes an efficient file format
- Datasets searching (internet) - ~2 datasets (collection of schema + data)
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

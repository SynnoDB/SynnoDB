import wandb

from workloads.workload_provider import ExecSettings


def create_wandb_speedup_plot(wandb_table, exec_settings: ExecSettings):
    return wandb.plot.bar(
        table=wandb_table,
        label="query_id",
        value="speedup",
        title=f"Speedup over DuckDB per Query (SF={exec_settings.scale_factor})",
    )

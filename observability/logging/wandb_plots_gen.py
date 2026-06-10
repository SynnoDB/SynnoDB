import wandb


def create_wandb_speedup_plot(wandb_table, sf):
    return wandb.plot.bar(
        table=wandb_table,
        label="query_id",
        value="speedup",
        title=f"Speedup over DuckDB per Query (SF={sf})",
    )

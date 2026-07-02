# ---------------------------------------------------------------------------
# Data drain abstractions
# ---------------------------------------------------------------------------

import json
from abc import ABC, abstractmethod
from pathlib import Path

from wandb.plot.custom_chart import CustomChart

from wandb import Table

_DUCKDB_TYPE_MAP = {
    int: "BIGINT",
    float: "DOUBLE",
    bool: "BOOLEAN",
}


def _duckdb_col_type(value) -> str:
    """Return a DuckDB column type string for a Python value."""
    return _DUCKDB_TYPE_MAP.get(type(value), "VARCHAR")


def _duckdb_col_value(value):
    """Coerce a value to something DuckDB can store; non-primitive → JSON string."""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    elif isinstance(value, str):
        return value
    elif isinstance(value, Path):
        return value.as_posix()
    elif isinstance(value, Table) or isinstance(value, CustomChart):
        return None
    return json.dumps(value)


def get_timeline_plot(data: dict):
    """Build a timeline figure from the WandbDrain internal data dict."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    from synnodb.observability.plots.plot_timeline import PlotConfig, TimelineEngine

    if not data:
        fig, _ = plt.subplots()
        return fig

    rows = [
        {"turn": step, "_step": step, **metrics}
        for step, metrics in sorted(data.items())
    ]
    history = pd.DataFrame(rows)

    engine = TimelineEngine(history, drill_down_to_query_level=True)
    config = PlotConfig(
        left_axis_series=["input_tokens"],
        right_axis_series=["code_size"],
        right_axis2_series=["speedup"],
        highlight_correction_span=True,
        figsize=(12, 4),
        legend_y_offset=1.35,
        x_label_pad=8,
        query_row_ymin=-0.1,
    )
    fig, _ = engine.plot(config=config)
    return fig


class DataDrain(ABC):
    """Abstract sink that receives emitted metrics."""

    @abstractmethod
    def emit(self, metrics: dict, step: int) -> None: ...

    def register_planned_stages(
        self, previews: list[dict], stage_name: str | None = None
    ) -> None:
        """Record the not-yet-executed stage previews for the current stage.

        Optional forward-looking hook: a conversation registers the previews of
        its whole (scheduled) stage list the moment it is built. Sinks that
        cannot surface future stages ignore it; the live dashboard uses it to
        show upcoming prompts in the prompts pane. No-op by default.
        """
        return None


class WandbDrain(DataDrain):
    """Emits metrics to Weights & Biases."""

    _last_timeline_plot_step = -1
    _data: dict[int, dict] = {}

    def __init__(self, create_timeline_plot_every_turns: int = 100) -> None:
        self.create_timeline_plot_every_turns = create_timeline_plot_every_turns

    def emit(self, metrics: dict, step: int) -> None:
        # update internal data store
        row = self._data.setdefault(step, {})
        for k, v in metrics.items():
            coerced = _duckdb_col_value(v)
            if coerced is not None:
                row[k] = coerced

        import wandb

        # create timeline plot if needed
        if (
            step
            >= self._last_timeline_plot_step + self.create_timeline_plot_every_turns
        ):
            self._last_timeline_plot_step = step
            metrics["timeline_plot"] = wandb.Image(get_timeline_plot(self._data))

        wandb.log(metrics, step=step, commit=False)


class DuckDBDrain(DataDrain):
    """Writes metrics rows into a DuckDB table, adding columns on the fly."""

    def __init__(self, db_path: Path | str, table_name: str = "run_metrics") -> None:
        import duckdb

        self.db_path = str(db_path)
        self.table_name = table_name
        self._con = duckdb.connect(self.db_path)
        self._con.execute(f"CREATE TABLE IF NOT EXISTS {self.table_name} (step BIGINT)")
        self._known_columns: set[str] = {"step"}

    def emit(self, metrics: dict, step: int) -> None:
        row = {"step": step}
        row.update({k: _duckdb_col_value(v) for k, v in metrics.items()})

        # delete where value is None
        row = {k: v for k, v in row.items() if v is not None}

        # Add any new columns
        for col, val in row.items():
            if col not in self._known_columns:
                col_type = _duckdb_col_type(metrics.get(col))
                self._con.execute(
                    f"ALTER TABLE {self.table_name} ADD COLUMN IF NOT EXISTS"
                    f' "{col}" {col_type}'
                )
                self._known_columns.add(col)

        exists = self._con.execute(
            f"SELECT 1 FROM {self.table_name} WHERE step = ?", [step]
        ).fetchone()

        if exists:
            non_step = {k: v for k, v in row.items() if k != "step"}
            if non_step:
                set_clause = ", ".join(f'"{c}" = ?' for c in non_step)
                self._con.execute(
                    f"UPDATE {self.table_name} SET {set_clause} WHERE step = ?",
                    list(non_step.values()) + [step],
                )
        else:
            cols = ", ".join(f'"{c}"' for c in row)
            placeholders = ", ".join("?" for _ in row)
            self._con.execute(
                f"INSERT INTO {self.table_name} ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )

    def close(self) -> None:
        self._con.close()

from typing import Protocol


class SystemRunner(Protocol):
    name: str

    def run_scale_factor(
        self,
        scale_factor: float,
        query_list: list[str],
        sql_list: list[str],
        args_list: list[str],
    ) -> list[float | None]:
        """Run all queries for one scale factor."""
        ...

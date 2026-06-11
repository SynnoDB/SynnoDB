from abc import abstractmethod


class WorkloadProvider:
    benchmark_name: str
    query_ids: list[str]
    sql_dict: dict[str, str]

    @abstractmethod
    def get_placeholders_fn(self):
        raise NotImplementedError("Subclasses must implement get_placeholders_fn")

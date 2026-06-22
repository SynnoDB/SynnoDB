import enum
import json
from pathlib import Path
from typing import Any


class JsonEncoder(json.JSONEncoder):
    """JSON encoder that handles Path and Enum objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, enum.Enum):
            return obj.value
        return super().default(obj)


def json_dumps(obj: Any, **kwargs: Any) -> str:
    kwargs.setdefault("cls", JsonEncoder)
    return json.dumps(obj, **kwargs)


def json_loads(s: str) -> Any:
    return json.loads(s)

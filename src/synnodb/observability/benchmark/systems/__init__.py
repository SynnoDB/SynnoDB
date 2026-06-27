"""Pluggable benchmark system runners.

To add a new system:
1. Implement a class with ``name: str`` and ``run_scale_factor(...)`` matching
   the ``SystemRunner`` protocol.
2. Register it in ``SYSTEM_REGISTRY`` with a lower-case key.
"""

from observability.benchmark.systems.base import SystemRunner

__all__ = [
    "SystemRunner",
]

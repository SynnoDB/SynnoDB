import csv
import os
from pathlib import Path
from typing import Iterable, Sequence


class BenchmarkWriter:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.output_path, "a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self._lock_supported = False
        try:
            import fcntl  # type: ignore

            self._fcntl = fcntl
            self._lock_supported = True
        except Exception:
            self._fcntl = None

    def _lock(self) -> None:
        if self._lock_supported:
            self._fcntl.flock(self.file.fileno(), self._fcntl.LOCK_EX)

    def _unlock(self) -> None:
        if self._lock_supported:
            self._fcntl.flock(self.file.fileno(), self._fcntl.LOCK_UN)

    def write_header_if_needed(self, header: Sequence[str]) -> None:
        if self.output_path.exists() and self.output_path.stat().st_size > 0:
            return
        self.write_row(header)

    def write_rows(self, rows: Iterable[Sequence[object]]) -> None:
        self._lock()
        try:
            for row in rows:
                self.writer.writerow(row)
            self.file.flush()
            os.fsync(self.file.fileno())
        finally:
            self._unlock()
        try:
            os.chmod(self.output_path, 0o777)
        except PermissionError:
            pass

    def write_row(self, row: Sequence[object]) -> None:
        self.write_rows([row])

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> "BenchmarkWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

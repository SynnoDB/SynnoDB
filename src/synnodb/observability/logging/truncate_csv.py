import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def truncate_large_csv(file_path: Path, max_size_mb: float) -> None:
    """
    Truncates a .csv file to at most max_size_mb (MiB).
    Adds a final marker line: "... truncated (original size) ..."
    """
    if not file_path.is_file() or file_path.suffix.lower() != ".csv":
        return

    max_bytes = int(max_size_mb * 1024 * 1024)
    original_size = file_path.stat().st_size
    if original_size <= max_bytes:
        return

    logger.debug(
        f"Truncating {file_path} from {original_size} bytes to {max_bytes} bytes."
    )

    marker = f"\n... truncated ({original_size} bytes) ...\n"
    marker_bytes = marker.encode("utf-8")

    # If marker alone would exceed the limit, write only a clipped marker.
    if len(marker_bytes) >= max_bytes:
        clipped = marker_bytes[:max_bytes]
        with file_path.open("wb") as f:
            f.write(clipped)
        return

    # Leave room for marker so final size stays <= max_bytes.
    allowed_bytes = max_bytes - len(marker_bytes)

    with file_path.open("rb") as f:
        content = f.read(allowed_bytes)

    with file_path.open("wb") as f:
        f.write(content)
        f.write(marker_bytes)


def truncate_csvs_recursively(base_path: Path, max_size_mb: float) -> None:
    for csv_file in base_path.rglob("*.csv"):
        truncate_large_csv(csv_file, max_size_mb)

import os
from pathlib import Path


def configure_weave_cache_dirs() -> None:
    cache_dir = Path.home() / ".cache" / "weave" / "server_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["WEAVE_SERVER_CACHE_DIR"] = cache_dir.as_posix()

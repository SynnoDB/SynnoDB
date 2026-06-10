import logging
import subprocess

logger = logging.getLogger(__name__)


def check_pkg(*packages):
    try:
        subprocess.run(
            ["pkg-config", "--exists", *packages],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        raise RuntimeError("pkg-config is not installed")


def get_flags(packages):
    cflags = subprocess.check_output(
        ["pkg-config", "--cflags", *packages], text=True
    ).strip()

    libs = subprocess.check_output(
        ["pkg-config", "--libs", *packages], text=True
    ).strip()

    return cflags, libs


if __name__ == "__main__":
    if check_pkg("arrow", "parquet"):
        cflags, libs = get_flags(["arrow", "parquet"])
        logger.info("CFLAGS:", cflags)
        logger.info("LIBS:", libs)

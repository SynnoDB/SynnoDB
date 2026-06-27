import logging
import sys
from pathlib import Path

sys.path.append(Path(__file__).parent.parent.parent.as_posix())

from synnodb.cpp_runner.compiler.compiler import Compiler

logger = logging.getLogger(__name__)


def main() -> None:
    cwd = Path(__file__).parent.resolve()
    compiler = Compiler(
        working_dir=cwd,
        libs={
            "mylib": ["mylib.cpp"],
            "mylib2": ["mylib2.cpp"],
            "mylib3": ["mylib3.cpp"],
        },
        main_src="main.cpp",
        app_extra_srcs=[cwd / "../utils/build_id.cpp"],
        build_dir="build",
        link_libs=[],
        pkgconfig_libs=[],
        include_dirs=[cwd.parent],
    )
    err = compiler.build()
    if err is not None:
        logger.error(err)
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()

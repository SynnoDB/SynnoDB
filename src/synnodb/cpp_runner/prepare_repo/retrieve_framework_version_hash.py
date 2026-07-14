import re
from pathlib import Path

# The framework source that is compiled into every engine but does NOT live in
# the workspace, so it is invisible to the git-snapshot half of the compile-cache
# key. Everything here must therefore be hashed into the key explicitly,
# otherwise editing it would silently reuse .so files built against the old code.
#
# Scoped to these three directories rather than a blanket rglob because
# prepare_repo/templates/ also sits under cpp_runner/, and those files are the
# workspace scaffold -- already covered by the prepare artifacts string, and
# carrying basenames (db_loader.cpp, parquet_reader.cpp) that collide with each
# other across the in-memory/ssd variants.
_FRAMEWORK_SUBDIRS = ("api", "cpp_helpers", "hotpatch")

_SOURCE_SUFFIXES = {".h", ".hpp", ".cpp"}


def _framework_files(framework_dir: Path) -> list[Path]:
    files = [
        f
        for f in framework_dir.glob("*")
        if f.is_file() and f.suffix in _SOURCE_SUFFIXES
    ]
    for subdir in _FRAMEWORK_SUBDIRS:
        files.extend(
            f
            for f in (framework_dir / subdir).rglob("*")
            if f.is_file() and f.suffix in _SOURCE_SUFFIXES
        )
    return files


def get_framework_version_artifacts_str() -> str:
    # load the framework files and extract IDs
    framework_dir = Path(__file__).parent.parent

    assert framework_dir.name == "cpp_runner"

    assert framework_dir.exists(), f"Framework directory {framework_dir} does not exist"

    artifacts_dict: dict[str, str] = {}

    for file in _framework_files(framework_dir):
        # extract the VERSION id from the file content, if not found use the full file content
        version_id, content = extract_version_id(file_path=file, content=None)

        # Key by path relative to cpp_runner/, not by bare filename: the
        # subdirectories contain distinct files with the same basename.
        name = str(file.relative_to(framework_dir))

        if version_id is not None:
            artifacts_dict[name] = version_id
        else:
            assert content is not None
            artifacts_dict[name] = content

    # concatenate all the version ids (or file content) and hash them to get the framework version hash
    artifacts_str = "\n\n".join(
        f"// ---- {name} ----\n{content}"
        for name, content in sorted(artifacts_dict.items())
    )

    return artifacts_str


def extract_version_id(
    file_path: Path | None, content: str | None, must_be_version: bool = False
) -> tuple[str | None, str | None]:
    if content is None:
        assert file_path is not None, "Either file_path or content must be provided"
        content = file_path.read_text()

    # apply regex
    file_version_regex = r"// FILE_VERSION: ([0-9]+)"
    match = re.search(file_version_regex, content)

    if must_be_version:
        assert match, (
            f"Expected to find version string in {file_path}. Ensure the file contains a line like '// FILE_VERSION: 123'. E.g. file was marked as read-only, then requires such a version string to be used in cache keys / ..."
        )

    if match:
        version = match.group(1)
        return version, None

    else:
        return None, content

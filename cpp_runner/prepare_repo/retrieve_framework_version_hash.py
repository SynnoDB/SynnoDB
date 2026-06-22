import re
from pathlib import Path


def get_framework_version_artifacts_str() -> str:
    # load the framework files and extract IDs
    framework_dir = Path(__file__).parent.parent

    assert framework_dir.name == "cpp_runner"

    assert framework_dir.exists(), f"Framework directory {framework_dir} does not exist"

    artifacts_dict: dict[str, str] = {}

    # go over each hpp and cpp file
    for file in Path(framework_dir).glob("*.[hc]pp"):
        # extract the VERSION id from the file content, if not found use the full file content
        version_id, content = extract_version_id(file_path=file, content=None)

        if version_id is not None:
            artifacts_dict[file.name] = version_id
        else:
            assert content is not None
            artifacts_dict[file.name] = content

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

from pathlib import Path

from prepare_repo.prepare import extract_version_id


def get_framework_version_artifacts_str() -> str:
    # load the framework files and extract IDs
    framework_dir = Path(__file__).parent.parent / "pipeline/cpp_runner"

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

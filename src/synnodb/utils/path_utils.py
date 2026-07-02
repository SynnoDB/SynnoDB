from pathlib import Path


def repo_root() -> Path:
    """The enclosing git checkout (walks up for .git / pyproject.toml)."""
    for d in (Path.cwd().resolve(), *Path.cwd().resolve().parents):
        if (d / ".git").exists() or (d / "pyproject.toml").exists():
            return d
    return Path.cwd().resolve()

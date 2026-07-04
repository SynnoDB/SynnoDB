# Developer guide

Checks that run in CI ([.github/workflows/lint.yml](.github/workflows/lint.yml)). Run them locally
before pushing.

**Strip tutorial notebooks** (no outputs/execution counts/non-sequential cell IDs - CI rejects
otherwise). Do this before committing any `tutorials/*.ipynb` change:

```bash
git ls-files 'tutorials/*.ipynb' | xargs uvx nbstripout==0.8.1            # strip
git ls-files 'tutorials/*.ipynb' | xargs uvx nbstripout==0.8.1 --verify   # check only (CI command)
```

**Lint / format** (Ruff, pinned `0.15.20`):

```bash
uvx ruff==0.15.20 check
uvx ruff==0.15.20 format --check   # drop --check to apply
```

**Tests** ([`tests/`](tests/)):

```bash
.venv/bin/python -m pytest
```

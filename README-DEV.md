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

## Releasing to PyPI

Publishing is triggered by pushing a tag ([.github/workflows/publish-to-pypi.yml](.github/workflows/publish-to-pypi.yml)
runs on any tag push, and the publish job only fires for tag refs). To cut a release:

1. Bump `version` in [pyproject.toml](pyproject.toml) and commit it. The tag must match this version -
   PyPI rejects re-uploads of an existing version, so a failed publish needs a version bump, not just a
   re-pushed tag.
2. Tag the commit as `v<version>` (matching the existing `v0.1.1`, `v0.0.1` convention) and push it:

   ```bash
   git tag -a v0.1.2 -m "Release 0.1.2"
   git push origin v0.1.2
   ```

The workflow builds the wheel/sdist and uploads via PyPI trusted publishing (OIDC, `pypi` GitHub
environment).

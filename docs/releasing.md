# Releasing

Releases publish to PyPI as `causeway-ai` through PyPI trusted publishing. No token is stored in the repo.

## One-time setup

1. Create a PyPI account and enable two-factor authentication.
2. On PyPI, add a pending trusted publisher (Account → Publishing): project `causeway-ai`, owner `Cygnux-Labs`, repository `causeway`, workflow `release.yml`, environment `pypi`.
3. In the GitHub repo: Settings → Environments → New environment `pypi`. Optionally require a reviewer.

## Each release

1. Update `__version__` in `causeway/__init__.py` and `version` in `pyproject.toml`.
2. Add a section to `CHANGELOG.md`.
3. Tag and push: `git tag v0.3.0 && git push --tags`.

The workflow checks that the tag matches the package version, builds, runs `twine check` and publishes.

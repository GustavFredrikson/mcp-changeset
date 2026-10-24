# Contributing

Thanks for taking a look. Issues and pull requests are welcome; for anything
security-related, please follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.

## Setup

```console
$ uv sync            # Python >= 3.10, installs the package and dev tools
```

## Checks

Everything CI runs:

```console
$ uv run ruff check .
$ uv run ruff format --check .
$ uv run mypy                      # strict; src/ and examples/
$ uv run pyright                   # strict; src/
$ uv run pytest --cov              # add -m 'not stdio' to skip the subprocess test
```

To test against mcp 1.x: `uv pip install "mcp>=1.10,<2"` and re-run pytest
(the MRTR example test is skipped there).

## Guidelines

* Keep the core small: `guard.py` (tools and workflow) and `ledger.py`
  (state machine). New behaviour should come with tests, including the failure
  path.
* Never weaken a fail-closed check to make a test pass. If a refusal is
  wrong, explain why in the PR.
* Error messages that can reach the agent must not contain tokens, file paths
  or remote error text.
* Update `CHANGELOG.md` under *Unreleased*.
* Commits: small, descriptive messages; sign off if you like, it isn't
  required.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `Guard(write_mode=...)` is annotated as `Literal["guarded", "disabled"] | None`
  instead of `str | None`. Runtime behaviour is unchanged.
- The release workflow runs when a `v*` tag is pushed (it checks that the tag
  matches the package version) instead of on a published GitHub release; the
  manual dry-run trigger is gone, since CI already builds and checks the wheel.

## [0.1.0] - 2026-10-24

First release.

### Added

- `Guard` and `@guard.guarded(apply=..., undo=..., observe=...)`, registering
  `preview_<op>` plus shared `apply_change`, `undo_change` and
  `get_change_status` tools on FastMCP (mcp 1.x) or MCPServer (mcp 2.x);
  `shared_tools=False` to expose `Guard.apply`/`undo`/`status` through your
  own tools.
- SQLite ledger state machine (`pending → applying → applied → undoing →
  undone`, plus `expired`, `apply_failed`, `undo_failed`) with
  `BEGIN IMMEDIATE` claims: at-most-once apply across threads and processes.
- Single-use, expiring, hashed confirmation and undo tokens bound to the
  change id and payload hash. Undo tokens expire after `undo_ttl` (default
  24 h, `None` for no expiry); the original design note has no undo expiry.
- Optional `observe` hook binding approval to observed state at apply and undo.
- Writes disabled unless `MCP_CHANGESET_WRITE_MODE=guarded` (or
  `write_mode="guarded"`).
- Optional `scope` binding; `0600` ledger file created with `O_NOFOLLOW`
  (symlinks refused); `max_payload_bytes` and `max_result_bytes` limits
  (64 KiB each); refusal on large backward clock jumps; `LedgerError` for busy,
  corrupt or unreadable ledgers; sanitized errors for agents.
- Examples: file-backed notes server, todo server, and composition with the
  SDK's MRTR elicitation (mcp 2.x).
- `mcp-changeset` command-line tool (also `python -m mcp_changeset`) for
  operators: `list`, `show`, `reconcile` and `prune` on an existing ledger.
  Changing commands require `--yes`; tokens are never printed.
- `Ledger.reconcile(change_id, note)`: move a change stuck in `applying` to
  `apply_failed` (or `undoing` to `undo_failed`) after manual review, recording
  the operator's note.
- `Ledger.prune(older_than, include_applied=False, dry_run=False)`: age-based
  retention for terminal rows. Never deletes `pending`, `applying` or
  `undoing` rows, nor `applied` rows that can still be undone.
- `Ledger(path, create=False)` opens an existing ledger only, refusing missing
  files and files that are not an mcp-changeset ledger.
- Configurable TTL bounds: `Guard(min_ttl=60, max_ttl=3600, max_undo_ttl=None)`.
- `examples/github_labels_server.py`: guarded label changes on GitHub issues,
  tested against a fake API with network access blocked.
- `docs/recipes.md`: multi-tenant scope, elicitation, auditing with
  `list_changes`, operating a ledger. Its code blocks run in the test suite.
- A state-machine diagram (mermaid) in the README.
- Ledger transactions from `preview`, `apply`, `undo` and the
  `get_change_status` tool run in a worker thread (`anyio.to_thread`), not on
  the event loop. Transactions are never abandoned mid-way, so the
  at-most-once claim holds.
- `mcp-changeset expire [--yes]`: mark every pending change past its TTL as
  `expired`, so `prune` can delete stale previews.
- Examples: `tickets_server.py` (SQLite, compare-and-set writes on a row
  version), `client_demo.py` (an MCP client over stdio, also a CI smoke test)
  and a Claude Desktop configuration, with tests.
- `docs/security-model.md`: token format and hashing, the single-use claim,
  claim-time checks, state binding and what each ledger column stores.
- Release workflow for PyPI Trusted Publishing (`.github/workflows/release.yml`,
  environment `pypi`, no API token); `workflow_dispatch` runs it as a dry run.
- CI: 95% coverage gate, the stdio client demo, an install of the built wheel
  with a CLI run, and actionlint on the workflows.

[Unreleased]: https://github.com/GustavFredrikson/mcp-changeset/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GustavFredrikson/mcp-changeset/releases/tag/v0.1.0

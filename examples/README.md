# Examples

Every example here is exercised by the test suite, on mcp 1.x and 2.x unless
noted.

| File | What it shows | Tests |
| --- | --- | --- |
| [`client_demo.py`](client_demo.py) | A real MCP client over stdio: preview → apply → replay refused → undo against `notes_server.py`, in a temporary directory | `tests/test_client_demo.py`, and run directly in CI |
| [`notes_server.py`](notes_server.py) | Notes as files: guarded write and delete bound to the file's content hash, with undo, name validation and symlink refusal | `tests/test_notes_server.py` |
| [`tickets_server.py`](tickets_server.py) | A SQLite ticket tracker whose writes are compare-and-set on a row version, closing the window `observe` alone leaves open | `tests/test_tickets_server.py` |
| [`github_labels_server.py`](github_labels_server.py) | Add/remove labels on GitHub issues through the REST API, with a repository allowlist; state bound to the issue's current labels | `tests/test_github_labels.py` (fake GitHub, network blocked) |
| [`todo_server.py`](todo_server.py) | The smallest complete server: in-memory create, update and delete | `tests/test_workflow.py` and others |
| [`elicit_server.py`](elicit_server.py) | Asking the *user* to approve the stored diff with MRTR elicitation before `guard.apply` (mcp 2.x only) | `tests/test_elicit_server.py` |
| [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json) | Registering the notes and tickets servers in Claude Desktop | `tests/test_client_demo.py` checks the paths |

## Try it

```console
$ uv run examples/client_demo.py
```

## Use the servers from Claude Desktop

1. Clone the repository and run `uv sync` in it once.
2. Copy the entries from
   [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json)
   into the `mcpServers` key of Claude Desktop's configuration file
   (`~/Library/Application Support/Claude/claude_desktop_config.json` on
   macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows).
3. Replace every `/ABSOLUTE/PATH/TO/...` with real absolute paths. If Claude
   Desktop cannot find `uv`, put the output of `which uv` in `command`.
4. Restart Claude Desktop completely.

`MCP_CHANGESET_WRITE_MODE=guarded` in `env` is what turns writes on; remove it
and the agent can still preview, but every apply and undo is refused. Each
server keeps its ledger under `$XDG_STATE_HOME` (default
`~/.local/state/mcp-changeset-<name>/ledger.db`); inspect it with

```console
$ uv run mcp-changeset --ledger ~/.local/state/mcp-changeset-notes/ledger.db list
```

Any other MCP client that launches stdio servers from a command, arguments and
environment variables can use the same three values.

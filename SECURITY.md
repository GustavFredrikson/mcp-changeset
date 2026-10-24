# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
**[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**:
open the repository's **Security** tab and choose **Report a vulnerability**.
Do not open a public issue for security problems.

Contact: `<SECURITY CONTACT: to be filled in before publishing>`

Only the latest release is supported. You can expect an acknowledgement
within a week; this is a personal project maintained on a best-effort basis.

## What mcp-changeset is for

mcp-changeset is a library that MCP **server authors** use to turn
state-changing tools into preview → apply → undo. It is a safety mechanism
against a *model* that is mistaken or manipulated (for example by prompt
injection), not an access-control system.

### Assets

* The external state your `apply`/`undo` functions change.
* The ledger file (payloads, results and error text in clear text; token
  hashes).
* Confirmation and undo tokens while they are valid.

### Actors and trust

| Actor | Trusted? | Notes |
| --- | --- | --- |
| Server code (your preview/apply/undo/observe) | yes | runs with the server's credentials |
| Operator (env vars, ledger file, config) | yes | |
| The model / agent | **no** | can call any tool with any arguments |
| Content the model reads (web pages, documents) | **no** | may carry prompt injection |
| The MCP client / host | partly | trusted to show tool results and elicitations to a person, if you rely on that |
| Other local users | no | the ledger is created `0600` |

### What it defends against (and how it is tested)

| Threat | Mitigation | Tests |
| --- | --- | --- |
| Agent gets diff A approved, then executes B | `apply_change` takes no change data; the stored payload runs | `test_apply_tool_takes_no_change_data`, `test_apply_runs_the_stored_normalized_payload` |
| Same approval used twice (retry, replay, race) | `BEGIN IMMEDIATE` claim, status never returns to `pending` | `test_token_is_single_use`, `test_concurrent_apply_runs_once`, `test_processes_racing_apply_exactly_one_wins`, `test_replay_after_restart` |
| Approval used for a different change | token hash covers the change id | `test_token_of_one_change_does_not_apply_another` |
| Old approval used much later | TTL (1–60 min); clock moving backwards refuses claims | `test_token_expires`, `test_clock_moving_backwards_is_refused_without_burning` |
| Target changed between preview and apply | optional `observe` hash, fail closed | `test_state_changed_after_preview_fails_closed`, `test_external_edit_invalidates_approval` |
| Undo overwrites someone's later edit | post-apply snapshot checked before undo | `test_undo_refuses_to_overwrite_later_edit` |
| Payload edited in the ledger | payload hash + token bound to it | `test_tampered_payload_is_refused`, `test_tampered_payload_and_hash_invalidates_token` |
| Writes enabled by accident | off unless `guarded`; refusing does not burn previews | `test_writes_disabled_by_default` |
| Change previewed for account A applied as B | optional `scope` hash | `test_scope_mismatch_is_refused` |
| Tokens leaking from storage or status | only SHA-256 stored; status never includes tokens | `test_tokens_are_not_stored_or_exposed` |
| Internal error text leaking to the agent | non-`ChangeError` exceptions reduced to type name | `test_failed_apply_consumes_token`, `test_undo_failure_is_recorded` |
| Oversized proposals filling the ledger | `max_payload_bytes` (64 KiB default) | `test_default_payload_limit` |

### Review notes

* **Token generation:** `secrets.token_urlsafe(24)` (192 bits, OS CSPRNG).
  Change ids use `secrets.token_hex(10)` and are not secret.
* **Storage and comparison:** only
  `sha256(purpose ‖ change_id ‖ payload_hash ‖ token)` is stored, compared with
  `hmac.compare_digest`. Unsalted SHA-256 is adequate because tokens are
  high-entropy random values, not passwords.
* **Payload binding:** payloads are normalized through canonical JSON (sorted
  keys, no whitespace, no NaN) and hashed with SHA-256; the hash is checked
  again at claim time.
* **Files:** the ledger file is created and re-chmodded `0600`; a missing
  parent directory is created `0700`. An existing parent directory's mode is
  not changed. SQLite's `-wal`/`-shm` files inherit the database file's mode.
* **What is returned:** previews return the confirmation token once; apply
  returns the undo token once; `get_change_status` never returns tokens.
  Payloads, diffs, summaries and `apply` results are returned to the agent and
  stored in clear text.
* **Logging:** mcp-changeset does not log. The MCP SDK may log tool errors
  (for example at INFO); those messages contain no tokens. Tool *results*,
  including tokens, are visible to the client and anything recording the
  session.
* **SQL:** all values are bound parameters; the only formatted SQL is a
  column list built from constants in `ledger.py`.

## Non-goals and known limits

* **Human approval.** On its own the confirmation token goes back to the agent
  in the same response as the diff. It limits *what* an approval can
  authorize; it does not prove a person gave it. Use MRTR elicitation (see the
  README) or a client that confirms destructive tools.
* **Token holders.** Whoever holds an unexpired token can apply that one
  stored change, once.
* **Authentication / authorization.** Not provided.
* **Local attackers.** Anyone who can write the ledger or run code as the
  server user can forge rows or call `apply` directly. The ledger is not
  tamper-evident.
* **TOCTOU.** The `observe` re-check shrinks the window to the gap between one
  read and the write; it does not close it unless `apply` writes
  conditionally. Only what `observe` returns is bound.
* **Ambiguous outcomes.** `apply_failed` does not always mean nothing changed.
  Rows stuck in `applying` after a crash need manual reconciliation.
* **Undo is compensation**, not rollback, and can itself fail.
* **Clocks.** Expiry uses the wall clock (`time.time()`), because the ledger
  is shared across processes. Claims are refused if the clock runs more than
  `max_clock_skew` behind a row's creation; forward jumps expire tokens early.
* **Retention.** Rows are never deleted automatically.

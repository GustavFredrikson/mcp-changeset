# Recipes

Short, copy-pasteable patterns. The `python` blocks on this page run in order
as part of the test suite (on mcp 2.x), so they stay correct.

## Multi-tenant servers: one ledger, one scope per tenant

Run one server process per tenant (or per account / API key) and give each
`Guard` a `scope`. Every change records a hash of the scope it was previewed
under, and a claim under any other scope is refused, so a confirmation token
minted for one tenant is useless to another even though they share a ledger
file.

```python
import asyncio
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from mcp_changeset import ChangeError, Guard, Proposal

ledger = Path(tempfile.mkdtemp()) / "ledger.db"
balances = {"alice": 100, "bob": 100}


def tenant_server(tenant: str) -> Guard:
    server = MCPServer(f"billing-{tenant}")
    guard = Guard(server, ledger_path=ledger, scope=f"tenant:{tenant}", write_mode="guarded")

    def apply(payload):
        balances[tenant] -= payload["amount"]
        return {"refund": payload["amount"]}

    def undo(payload, result):
        balances[tenant] += result["refund"]

    @guard.guarded(apply=apply, undo=undo, observe=lambda p: balances[tenant])
    def charge(amount: int) -> Proposal:
        """Charge this tenant."""
        if not 0 < amount <= 1000:
            raise ChangeError("amount must be 1-1000")
        return Proposal(
            payload={"amount": amount},
            diff=[
                {"field": "balance", "before": balances[tenant], "after": balances[tenant] - amount}
            ],
            summary=f"Charge {tenant} {amount}",
        )

    return guard


alice, bob = tenant_server("alice"), tenant_server("bob")


async def cross_tenant() -> None:
    preview = await alice.preview("charge", amount=30)
    try:
        await bob.apply(preview["change_id"], preview["confirmation_token"])
    except ChangeError as exc:
        print("refused:", exc)  # change was previewed under a different scope
    await alice.apply(preview["change_id"], preview["confirmation_token"])  # still pending


asyncio.run(cross_tenant())
assert balances == {"alice": 70, "bob": 100}
```

Keep the scope a stable identifier you control (tenant id, account id), not
something the agent supplies.

## Asking the user, not the model (MRTR elicitation)

On mcp 2.x, register only the previews with `shared_tools=False` and write your
own `apply_change` whose extra parameter is filled by `Resolve(ask_user)`.
`ask_user` reads the diff from the ledger (never from the agent's arguments),
escapes it with `json.dumps(..., ensure_ascii=True)` and returns an
`Elicit(...)`. A decline or cancel aborts before the tool body runs, so the
token is not consumed; an accept leads to `guard.apply(...)`, which keeps the
single-use and state-check guarantees the SDK's sealed `requestState` does not
give on its own.

The complete, tested version is
[`examples/elicit_server.py`](https://github.com/GustavFredrikson/mcp-changeset/blob/main/examples/elicit_server.py).
Remember that an elicitation answer comes from the client: you are trusting it
to show the question to a person.

## Auditing with `list_changes`

The ledger is a local record of every preview and its outcome. Trusted code can
read it directly; `ChangeRecord` never contains tokens.

```python
import json
import time

from mcp_changeset import Status

records = alice.ledger.list_changes()  # every tenant's rows: the ledger is shared

# What actually ran in the last 24 hours, as JSON lines for a log pipeline:
since = time.time() - 86400
for r in records:
    if r.status in (Status.APPLIED, Status.UNDONE) and r.updated_at >= since:
        print(
            json.dumps(
                {
                    "id": r.change_id,
                    "action": r.action,
                    "status": r.status.value,
                    "summary": r.summary,
                    "payload": r.payload,
                }
            )
        )

# Counts per status:
counts: dict[str, int] = {}
for r in records:
    counts[r.status.value] = counts.get(r.status.value, 0) + 1
print(counts)

# Changes stuck mid-apply for more than five minutes need a person:
stuck = [r for r in alice.ledger.list_changes(Status.APPLYING) if time.time() - r.updated_at > 300]
assert stuck == []
```

The ledger is not tamper-evident: anyone who can write the file can rewrite
history. Ship these records somewhere append-only if you need a real audit log.

## Operating a ledger

The `mcp-changeset` command works on an existing ledger file and never creates
one. Commands that change something need `--yes`; without it they print what
they would do and exit with status 2.

```console
$ export MCP_CHANGESET_LEDGER=~/.local/state/notes/ledger.db
$ mcp-changeset list --status applying
$ mcp-changeset show chg_3f9a1c0d2b7e4a6f8c11 --json
$ mcp-changeset reconcile chg_3f9a1c0d2b7e4a6f8c11 \
    --note "checked the calendar: event was not moved" --yes
$ mcp-changeset prune --older-than-days 30 --yes
```

From Python, the same operations are `Ledger.reconcile(change_id, note)` and
`Ledger.prune(older_than_seconds, include_applied=False, dry_run=False)`.
Pruning never touches `pending`, `applying` or `undoing` rows, and only deletes
`applied` rows with `include_applied=True` once they can no longer be undone.

```python
from mcp_changeset import Ledger

ops = Ledger(ledger, create=False)  # open an existing ledger; never creates one
print(ops.prune(30 * 86400, dry_run=True), "rows would be pruned")
```

A cron entry such as
`17 3 * * * mcp-changeset --ledger /srv/app/ledger.db prune --older-than-days 30 --yes`
keeps the ledger from growing without bound.

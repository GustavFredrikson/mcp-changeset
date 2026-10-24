"""The SQLite ticket example: compare-and-set writes on top of observe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_changeset import ChangeError, StateChangedError, Status

from test_client_session import call, in_memory
from tickets_server import TicketDB, build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def db(tmp_path: Path) -> TicketDB:
    return TicketDB(tmp_path / "tickets.db")


@pytest.fixture
def tickets(db: TicketDB, tmp_path: Path, clock: Any) -> tuple[Any, Any]:
    return build_server(db, tmp_path / "state" / "ledger.db", write_mode="guarded", clock=clock)


async def test_create_update_and_undo(db: TicketDB, tickets: tuple[Any, Any]) -> None:
    _, guard = tickets
    p = await guard.preview("create_ticket", title="  Printer on fire ", assignee="ana")
    assert p["diff"] == [
        {"field": "assignee", "before": None, "after": "ana"},
        {"field": "status", "before": None, "after": "open"},
        {"field": "title", "before": None, "after": "Printer on fire"},
    ]
    created = await guard.apply(p["change_id"], p["confirmation_token"])
    ticket_id = created["result"]["ticket_id"]
    assert db.get(ticket_id) == {
        "id": ticket_id,
        "title": "Printer on fire",
        "status": "open",
        "assignee": "ana",
        "version": 1,
    }

    u = await guard.preview("update_ticket", ticket_id=ticket_id, status="closed", assignee="ana")
    assert u["diff"] == [{"field": "status", "before": "open", "after": "closed"}]
    applied = await guard.apply(u["change_id"], u["confirmation_token"])
    assert applied["result"] == {"version": 2}
    assert db.get(ticket_id)["status"] == "closed"  # type: ignore[index]

    await guard.undo(u["change_id"], applied["undo_token"])
    assert db.get(ticket_id)["status"] == "open"  # type: ignore[index]
    assert db.get(ticket_id)["version"] == 3  # type: ignore[index]


async def test_edit_between_preview_and_apply_fails_closed(
    db: TicketDB, tickets: tuple[Any, Any]
) -> None:
    _, guard = tickets
    ticket_id = db.create("Fix login")
    p = await guard.preview("update_ticket", ticket_id=ticket_id, assignee="bo")
    db.update_if(ticket_id, 1, {"assignee": "someone else"})
    with pytest.raises(StateChangedError):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert db.get(ticket_id)["assignee"] == "someone else"  # type: ignore[index]
    assert guard.record(p["change_id"]).status is Status.APPLY_FAILED


async def test_compare_and_set_closes_the_window_observe_leaves(
    db: TicketDB, tickets: tuple[Any, Any]
) -> None:
    """A write that lands after observe's re-check but before apply is still caught."""
    _, guard = tickets
    ticket_id = db.create("Upgrade db")
    p = await guard.preview("update_ticket", ticket_id=ticket_id, status="in_progress")

    op = guard._ops["update_ticket"]
    real_apply = op.apply

    def racing_apply(payload: dict[str, Any]) -> Any:
        db.update_if(ticket_id, 1, {"title": "renamed concurrently"})  # sneaks in
        return real_apply(payload)

    guard._ops["update_ticket"] = type(op)(op.action, op.preview, racing_apply, op.undo, op.observe)
    with pytest.raises(StateChangedError, match="changed concurrently"):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert db.get(ticket_id) == {
        "id": ticket_id,
        "title": "renamed concurrently",
        "status": "open",
        "assignee": None,
        "version": 2,
    }


async def test_undo_refuses_to_overwrite_a_later_edit(
    db: TicketDB, tickets: tuple[Any, Any]
) -> None:
    _, guard = tickets
    ticket_id = db.create("Docs")
    p = await guard.preview("update_ticket", ticket_id=ticket_id, title="Docs v2")
    applied = await guard.apply(p["change_id"], p["confirmation_token"])
    db.update_if(ticket_id, 2, {"status": "closed"})
    with pytest.raises(StateChangedError):
        await guard.undo(p["change_id"], applied["undo_token"])
    assert db.get(ticket_id)["title"] == "Docs v2"  # type: ignore[index]


async def test_undo_create_keeps_an_edited_ticket(db: TicketDB, tickets: tuple[Any, Any]) -> None:
    _, guard = tickets
    p = await guard.preview("create_ticket", title="Temp")
    applied = await guard.apply(p["change_id"], p["confirmation_token"])
    ticket_id = applied["result"]["ticket_id"]
    db.update_if(ticket_id, 1, {"status": "in_progress"})
    with pytest.raises(ChangeError):
        await guard.undo(p["change_id"], applied["undo_token"])
    assert db.get(ticket_id) is not None
    assert guard.record(p["change_id"]).status is Status.UNDO_FAILED


async def test_undo_create_deletes_an_untouched_ticket(
    db: TicketDB, tickets: tuple[Any, Any]
) -> None:
    _, guard = tickets
    p = await guard.preview("create_ticket", title="Temp")
    applied = await guard.apply(p["change_id"], p["confirmation_token"])
    await guard.undo(p["change_id"], applied["undo_token"])
    assert db.all() == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ticket_id": 999, "status": "closed"}, "no ticket 999"),
        ({"ticket_id": 1}, "nothing to change"),
        ({"ticket_id": 1, "status": "open"}, "nothing to change"),
        ({"ticket_id": 1, "title": "   "}, "title must be"),
        ({"ticket_id": 1, "title": "x" * 201}, "title must be"),
    ],
)
async def test_update_validation(
    db: TicketDB, tickets: tuple[Any, Any], kwargs: dict[str, Any], message: str
) -> None:
    _, guard = tickets
    db.create("Existing")
    with pytest.raises(ChangeError, match=message):
        await guard.preview("update_ticket", **kwargs)


async def test_create_validation(tickets: tuple[Any, Any]) -> None:
    _, guard = tickets
    with pytest.raises(ChangeError, match="title must be"):
        await guard.preview("create_ticket", title=" ")


def test_update_if_rejects_unknown_or_empty_fields(db: TicketDB) -> None:
    ticket_id = db.create("x")
    with pytest.raises(ChangeError, match="version"):
        db.update_if(ticket_id, 1, {"version": 99})
    with pytest.raises(ChangeError, match="none"):
        db.update_if(ticket_id, 1, {})
    with pytest.raises(StateChangedError):
        db.delete_if(ticket_id, 7)


async def test_through_a_client_session(db: TicketDB, tickets: tuple[Any, Any]) -> None:
    server, _ = tickets
    ticket_id = db.create("Via client")
    async with in_memory(server) as client:
        err, preview = await call(
            client, "preview_update_ticket", {"ticket_id": ticket_id, "status": "closed"}
        )
        assert not err, preview
        err, _ = await call(
            client, "preview_update_ticket", {"ticket_id": ticket_id, "status": "bogus"}
        )
        assert err  # the Literal type is enforced by the tool's input schema
        err, applied = await call(
            client,
            "apply_change",
            {
                "change_id": preview["change_id"],
                "confirmation_token": preview["confirmation_token"],
            },
        )
        assert not err and applied["status"] == "applied"
        err, listed = await call(client, "list_tickets", {})
        assert not err and "Via client" in str(listed)
    assert db.get(ticket_id)["status"] == "closed"  # type: ignore[index]

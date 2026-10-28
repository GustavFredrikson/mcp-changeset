"""Every registered tool carries all four MCP annotation hints as explicit booleans."""

from __future__ import annotations

from typing import Any

import pytest
from test_mcp_integration import call

pytestmark = pytest.mark.anyio

HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")

PREVIEW = {
    "read_only_hint": False,
    "destructive_hint": False,
    "idempotent_hint": False,
    "open_world_hint": True,
}
EXPECTED: dict[str, dict[str, bool]] = {
    "preview_create_todo": PREVIEW,
    "preview_update_todo": PREVIEW,
    "preview_delete_todo": PREVIEW,
    "apply_change": {
        "read_only_hint": False,
        "destructive_hint": True,
        "idempotent_hint": True,
        "open_world_hint": True,
    },
    "undo_change": {
        "read_only_hint": False,
        "destructive_hint": True,
        "idempotent_hint": True,
        "open_world_hint": True,
    },
    "get_change_status": {
        "read_only_hint": True,
        "destructive_hint": False,
        "idempotent_hint": True,
        "open_world_hint": False,
    },
}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def hints(tool: Any) -> dict[str, Any]:
    """The four hints of a listed tool, on mcp 1.x (camelCase) and 2.x (snake_case)."""
    annotations = tool.annotations
    assert annotations is not None, f"{tool.name} has no annotations"
    explicit = annotations.model_fields_set
    out: dict[str, Any] = {}
    for hint in HINTS:
        field = hint if hint in type(annotations).model_fields else _camel(hint)
        assert field in explicit, f"{tool.name}: {field} is not set explicitly"
        out[hint] = getattr(annotations, field)
    return out


async def listed(server: Any) -> dict[str, Any]:
    return {t.name: t for t in await server.list_tools()}


async def test_every_guard_tool_sets_all_four_hints(make):
    server, _ = make()
    tools = await listed(server)
    for name, expected in EXPECTED.items():
        got = hints(tools[name])
        assert all(isinstance(v, bool) for v in got.values()), (name, got)
        assert got == expected, name


async def test_apply_change_annotations(make):
    server, _ = make()
    got = hints((await listed(server))["apply_change"])
    assert got == EXPECTED["apply_change"]


async def test_prefixed_tools_keep_their_annotations(make):
    server, _ = make(prefix="todo_")
    tools = await listed(server)
    for name, expected in EXPECTED.items():
        assert hints(tools[f"todo_{name}"]) == expected, name


async def test_apply_change_repeated_has_no_further_effect(make, store):
    """Backs idempotent_hint=True: a repeated call is refused and changes nothing."""
    server, _ = make()
    _, preview = await call(server, "preview_create_todo", {"title": "once"})
    args = {"change_id": preview["change_id"], "confirmation_token": preview["confirmation_token"]}
    err, applied = await call(server, "apply_change", args)
    assert not err and applied["status"] == "applied"
    after_first = dict(store.items)

    err, again = await call(server, "apply_change", args)
    assert err and "cannot be applied" in str(again)
    assert store.items == after_first

    undo_args = {"change_id": preview["change_id"], "undo_token": applied["undo_token"]}
    err, undone = await call(server, "undo_change", undo_args)
    assert not err and undone["status"] == "undone"
    err, again = await call(server, "undo_change", undo_args)
    assert err
    assert store.items == {}

    _, status1 = await call(server, "get_change_status", {"change_id": preview["change_id"]})
    _, status2 = await call(server, "get_change_status", {"change_id": preview["change_id"]})
    assert status1 == status2


async def test_preview_records_a_change_but_leaves_the_store_alone(make, store):
    """Backs the preview hints: read_only_hint=False because each call adds a pending
    ledger row, destructive_hint=False because the target store is untouched, and
    idempotent_hint=False because a repeat mints a new change."""
    server, guard = make()
    _, first = await call(server, "preview_create_todo", {"title": "x"})
    _, second = await call(server, "preview_create_todo", {"title": "x"})
    assert store.items == {}
    assert first["change_id"] != second["change_id"]
    pending = guard.ledger.list_changes()
    assert {r.change_id for r in pending} == {first["change_id"], second["change_id"]}

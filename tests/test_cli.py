"""The mcp-changeset command-line tool."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest

from mcp_changeset import Status
from mcp_changeset.cli import main

from test_next_features import _stuck

pytestmark = pytest.mark.anyio


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


@pytest.fixture
async def populated(guard, ledger_path, clock):
    # The CLI uses the real clock; put the guard's fake clock 100 days in the past.
    clock.now = time.time() - 100 * 86400
    p = await guard.preview("create_todo", title="Köp mjölk")
    a = await guard.preview("create_todo", title="applied one")
    applied = await guard.apply(a["change_id"], a["confirmation_token"])
    stuck = await _stuck(guard)
    return {
        "ledger": str(ledger_path),
        "pending": p,
        "applied": a,
        "applied_result": applied,
        "stuck": stuck,
        "guard": guard,
    }


async def test_list_table_and_json(populated):
    code, text = run("--ledger", populated["ledger"], "list")
    assert code == 0
    assert text.splitlines()[0].startswith("CHANGE_ID")
    assert populated["pending"]["change_id"] in text and "Köp mjölk" in text
    code, text = run("--ledger", populated["ledger"], "list", "--status", "applying", "--json")
    rows = [json.loads(line) for line in text.splitlines()]
    assert code == 0 and [r["change_id"] for r in rows] == [populated["stuck"]]


async def test_show_never_prints_tokens(populated):
    tokens = [
        populated["pending"]["confirmation_token"],
        populated["applied"]["confirmation_token"],
        populated["applied_result"]["undo_token"],
    ]
    for change_id in (populated["pending"]["change_id"], populated["applied"]["change_id"]):
        for extra in ((), ("--json",)):
            code, text = run("--ledger", populated["ledger"], "show", change_id, *extra)
            assert code == 0 and change_id in text
            assert not any(t in text for t in tokens)
    code, text = run(
        "--ledger", populated["ledger"], "show", populated["applied"]["change_id"], "--json"
    )
    view = json.loads(text)
    assert view["payload"] == {"title": "applied one"} and "result" in view
    code, text = run("--ledger", populated["ledger"], "list", "--json")
    assert not any(t in text for t in tokens)


async def test_reconcile_requires_yes(populated):
    stuck = populated["stuck"]
    code, text = run("--ledger", populated["ledger"], "reconcile", stuck, "--note", "checked")
    assert code == 2 and "re-run with --yes" in text
    assert populated["guard"].status(stuck)["status"] == "applying"
    code, text = run(
        "--ledger", populated["ledger"], "reconcile", stuck, "--note", "checked", "--yes"
    )
    assert code == 0 and "applying -> apply_failed" in text
    assert populated["guard"].record(stuck).error == "reconciled by operator: checked"


async def test_reconcile_refuses_non_stuck(populated, capsys):
    pending = populated["pending"]["change_id"]
    code, _ = run("--ledger", populated["ledger"], "reconcile", pending, "--note", "x", "--yes")
    assert code == 1 and "only applying or undoing" in capsys.readouterr().err
    assert populated["guard"].status(pending)["status"] == "pending"


async def test_prune_requires_yes(populated, clock):
    guard = populated["guard"]
    clock.advance(guard.ttl)
    guard.ledger.expire_stale()  # updated ~100 days ago in real time
    code, text = run("--ledger", populated["ledger"], "prune", "--older-than-days", "30")
    assert code == 2 and "would delete 1 change(s)" in text
    assert len(guard.ledger.list_changes()) == 3
    code, text = run("--ledger", populated["ledger"], "prune", "--older-than-days", "30", "--yes")
    assert code == 0 and "deleted 1 change(s)" in text
    assert {r.status for r in guard.ledger.list_changes()} == {Status.APPLIED, Status.APPLYING}


async def test_expire_requires_yes_and_unblocks_prune(populated):
    guard, pending = populated["guard"], populated["pending"]["change_id"]
    # The pending preview was made ~100 days ago (real clock) with a 15 min TTL.
    code, text = run("--ledger", populated["ledger"], "prune", "--older-than-days", "30")
    assert code == 2 and "would delete 0 change(s)" in text  # pending rows are never pruned
    code, text = run("--ledger", populated["ledger"], "expire")
    assert code == 2 and "would expire 1 change(s)" in text
    assert guard.status(pending)["status"] == "pending"
    code, text = run("--ledger", populated["ledger"], "expire", "--yes")
    assert code == 0 and "expired 1 change(s)" in text
    assert guard.status(pending)["status"] == "expired"
    code, text = run("--ledger", populated["ledger"], "expire")
    assert code == 2 and "would expire 0 change(s)" in text


def test_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MCP_CHANGESET_LEDGER", raising=False)
    with pytest.raises(SystemExit) as info:
        main(["list"])
    assert info.value.code == 2
    code, _ = run("--ledger", str(tmp_path / "missing.db"), "list")
    assert code == 1 and "no ledger file" in capsys.readouterr().err
    assert not (tmp_path / "missing.db").exists()  # the CLI never creates a ledger


async def test_env_var_and_unknown_change(populated, monkeypatch, capsys):
    monkeypatch.setenv("MCP_CHANGESET_LEDGER", populated["ledger"])
    code, _ = run("show", "chg_nope")
    assert code == 1 and "unknown change" in capsys.readouterr().err


def test_console_script_entry_point(tmp_path):
    from mcp_changeset import Ledger

    path = tmp_path / "l.db"
    Ledger(path)
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_changeset", "--ledger", str(path), "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0 and proc.stdout.startswith("CHANGE_ID")

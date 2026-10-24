"""Helpers that run in child processes (must be importable, not defined in tests)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from typing import Any

from mcp_changeset import ChangeError, Guard, Proposal


class NullServer:
    def add_tool(self, *args: Any, **kwargs: Any) -> None:
        pass


def make_counter_guard(ledger: str, counter: str) -> Guard:
    guard = Guard(NullServer(), ledger_path=ledger, write_mode="guarded")

    def apply(payload: dict[str, Any]) -> None:
        fd = os.open(counter, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)

    @guard.guarded(apply=apply)
    def bump(n: int) -> Proposal:
        return Proposal(payload={"n": n})

    return guard


def race_apply(ledger: str, counter: str, change_id: str, token: str, barrier: Any) -> str:
    guard = make_counter_guard(ledger, counter)
    barrier.wait()
    try:
        asyncio.run(guard.apply(change_id, token))
    except ChangeError as exc:
        return f"refused: {exc}"
    return "ok"


def hold_lock(path: str) -> None:
    """Hold an exclusive write lock on ``path`` until stdin closes."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    print("locked", flush=True)
    sys.stdin.read()
    conn.execute("ROLLBACK")
    conn.close()


if __name__ == "__main__":
    hold_lock(sys.argv[1])

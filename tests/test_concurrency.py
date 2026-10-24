from __future__ import annotations

import asyncio
import threading

from mcp_changeset import ChangeError, Guard, Proposal


def test_concurrent_apply_runs_once(tmp_path):
    """Many workers, each with its own Guard and connection, race on one token."""
    ledger = tmp_path / "ledger.db"
    applied: list[int] = []
    lock = threading.Lock()

    class NullServer:
        def add_tool(self, *args, **kwargs):
            pass

    def make_guard() -> Guard:
        g = Guard(NullServer(), ledger_path=ledger, write_mode="guarded")

        def apply(payload):
            with lock:
                applied.append(payload["n"])
            return None

        @g.guarded(apply=apply)
        def bump(n: int) -> Proposal:
            return Proposal(payload={"n": n})

        return g

    preview = asyncio.run(make_guard().preview("bump", n=7))
    workers = 16
    guards = [make_guard() for _ in range(workers)]
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []

    def worker(g: Guard) -> None:
        barrier.wait()
        try:
            asyncio.run(g.apply(preview["change_id"], preview["confirmation_token"]))
            outcomes.append("ok")
        except ChangeError:
            outcomes.append("refused")

    threads = [threading.Thread(target=worker, args=(g,)) for g in guards]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert applied == [7]
    assert sorted(outcomes) == ["ok"] + ["refused"] * (workers - 1)
    assert guards[0].status(preview["change_id"])["status"] == "applied"

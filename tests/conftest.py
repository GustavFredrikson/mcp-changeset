from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from todo_server import TodoStore, build_server


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> TodoStore:
    return TodoStore()


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "ledger.db"


@pytest.fixture
def make(store: TodoStore, ledger_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MCP_CHANGESET_WRITE_MODE", raising=False)

    def _make(**kwargs: Any):
        kwargs.setdefault("write_mode", "guarded")
        kwargs.setdefault("clock", clock)
        return build_server(store, ledger_path, **kwargs)

    return _make


@pytest.fixture
def guard(make):
    return make()[1]

"""Register state-changing operations as preview -> apply -> undo MCP tools."""

from __future__ import annotations

import functools
import inspect
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, Union

import anyio.to_thread

from .errors import ChangeError, StateChangedError, WritesDisabledError
from .ledger import ChangeRecord, Ledger, Status, canonical_hash, canonical_json

__all__ = [
    "DEFAULT_ENV_VAR",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_MAX_RESULT_BYTES",
    "Guard",
    "Proposal",
    "diff_fields",
]

DEFAULT_ENV_VAR = "MCP_CHANGESET_WRITE_MODE"
"""Environment variable that enables writes when set to ``guarded``."""

DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
"""Default limit on the UTF-8 size of a proposal's payload + diff + summary."""

DEFAULT_MAX_RESULT_BYTES = 64 * 1024
"""Default limit on the UTF-8 size of the JSON result an ``apply`` function returns."""

GUARDED = "guarded"
DISABLED = "disabled"
MIN_TTL = 60.0
"""Default lower bound for ``ttl`` (seconds); override with ``min_ttl``."""
MAX_TTL = 3600.0
"""Default upper bound for ``ttl`` (seconds); override with ``max_ttl``."""

# MCP tool annotations, all four hints set explicitly on every tool. They are
# hints for clients, not guarantees. apply/undo/preview/observe are user code,
# so the open-world hints assume they may call a remote system (as the GitHub
# example does).
#
# preview_<op>: runs the preview and observe functions, which must not change
#   the target system, then stores a pending change (row + confirmation token)
#   in the ledger that apply_change later consumes. That ledger write is a real
#   state change, so the tool is not read-only; it only adds a row, so it is
#   not destructive. Each call mints a new change_id and token, so it is not
#   idempotent.
_PREVIEW_ANNOTATIONS: dict[str, bool] = {
    "read_only_hint": False,
    "destructive_hint": False,
    "idempotent_hint": False,
    "open_world_hint": True,
}
# apply_change: runs the user's apply function, which may overwrite or delete
# data. The confirmation token is claimed at most once, so repeating a call with
# the same arguments is refused and has no further effect.
_APPLY_ANNOTATIONS: dict[str, bool] = {
    "read_only_hint": False,
    "destructive_hint": True,
    "idempotent_hint": True,
    "open_world_hint": True,
}
# undo_change: runs the user's undo function, which reverses an apply (for
# example deletes what was created or restores overwritten values). The undo
# token is claimed at most once, like apply's.
_UNDO_ANNOTATIONS: dict[str, bool] = {
    "read_only_hint": False,
    "destructive_hint": True,
    "idempotent_hint": True,
    "open_world_hint": True,
}
# get_change_status: one read of the local SQLite ledger; calls no user code.
_STATUS_ANNOTATIONS: dict[str, bool] = {
    "read_only_hint": True,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": False,
}

T = TypeVar("T")
MaybeAwaitable = Union[T, Awaitable[T]]  # noqa: UP007 - runtime alias must work on 3.10

PreviewFn = Callable[..., "MaybeAwaitable[Proposal]"]
ApplyFn = Callable[[Any], MaybeAwaitable[Any]]
UndoFn = Callable[[Any, Any], MaybeAwaitable[None]]
ObserveFn = Callable[[Any], MaybeAwaitable[Any]]


@dataclass(frozen=True)
class Proposal:
    """What a preview function returns.

    Attributes:
        payload: The exact, JSON-serializable input ``apply`` will receive.
            It is normalized through canonical JSON (tuples become lists,
            keys become strings) before it is stored.
        diff: What a human is shown, typically built with :func:`diff_fields`.
        summary: One line describing the change.
    """

    payload: Any
    diff: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    summary: str = ""


def diff_fields(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Field-level diff of two flat dicts, sorted by field name.

    ``before=None`` describes a create and ``after=None`` a delete. Fields whose
    canonical JSON is equal are omitted.
    """
    before = before or {}
    after = after or {}
    out: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if canonical_json(b) != canonical_json(a):
            out.append({"field": key, "before": b, "after": a})
    return out


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    out = fn(*args)
    if inspect.isawaitable(out):
        out = await out
    return out


def _annotations(**hints: bool) -> Any:
    """Build ``ToolAnnotations`` for mcp 1.x (camelCase fields) and 2.x (snake_case)."""
    from mcp.types import ToolAnnotations

    cls: Any = ToolAnnotations
    if "destructive_hint" in cls.model_fields:
        return cls(**hints)

    def camel(name: str) -> str:  # pragma: no cover - exercised on mcp 1.x only
        head, *rest = name.split("_")
        return head + "".join(part.title() for part in rest)

    return cls(**{camel(k): v for k, v in hints.items()})  # pragma: no cover


@dataclass(frozen=True)
class _Operation:
    action: str
    preview: PreviewFn
    apply: ApplyFn
    undo: UndoFn | None
    observe: ObserveFn | None


class Guard:
    """Adds guarded write tools to a FastMCP (mcp 1.x) or MCPServer (mcp 2.x).

    With ``shared_tools=True`` (the default) three tools are registered, named
    with ``prefix``:

    * ``apply_change(change_id, confirmation_token)``
    * ``undo_change(change_id, undo_token)``
    * ``get_change_status(change_id)``

    plus one ``preview_<name>`` tool per :meth:`guarded` operation. With
    ``shared_tools=False`` only previews are registered and you expose
    :meth:`apply`, :meth:`undo` and :meth:`status` through your own tools
    (for example behind an elicitation prompt).

    Writes are off unless ``write_mode="guarded"`` is passed or the
    environment variable ``env_var`` is ``guarded`` (case-insensitive).
    ``write_mode="disabled"`` forces them off regardless of the environment.

    Args:
        server: Any object with an ``add_tool(fn, name=, description=,
            annotations=)`` method.
        ledger_path: SQLite ledger file, shared by every process using it.
        ttl: Seconds a confirmation token stays valid, within
            ``min_ttl``..``max_ttl``.
        undo_ttl: Seconds an undo token stays valid, or ``None`` for no expiry.
        min_ttl: Smallest allowed ``ttl`` (default 60 s). Must be positive.
        max_ttl: Largest allowed ``ttl`` (default 3600 s).
        max_undo_ttl: Optional upper bound for ``undo_ttl``. When set,
            ``undo_ttl`` must be a number no larger than it (``None`` is
            rejected).
        write_mode: ``"guarded"``, ``"disabled"`` or ``None`` (use ``env_var``).
        env_var: Environment variable consulted when ``write_mode`` is ``None``.
        scope: Optional identity hashed into each change; see :class:`Ledger`.
        prefix: Prepended to every registered tool name.
        shared_tools: Register the apply/undo/status tools.
        max_payload_bytes: Refuse proposals whose payload, diff and summary
            exceed this many UTF-8 bytes of canonical JSON.
        max_result_bytes: Limit on the canonical JSON of an ``apply`` result.
            The write has already happened when this is checked, so an
            oversized result marks the change ``apply_failed`` (not undoable)
            and the agent is told the remote state needs review.
        busy_timeout: Seconds to wait for the ledger's write lock.
        max_clock_skew: Seconds the clock may run behind a change's creation
            time before claims are refused.
        clock: Wall-clock source in Unix seconds (for tests).
    """

    def __init__(
        self,
        server: Any,
        *,
        ledger_path: str | os.PathLike[str],
        ttl: float = 900.0,
        undo_ttl: float | None = 86400.0,
        min_ttl: float = MIN_TTL,
        max_ttl: float = MAX_TTL,
        max_undo_ttl: float | None = None,
        write_mode: Literal["guarded", "disabled"] | None = None,
        env_var: str = DEFAULT_ENV_VAR,
        scope: str | None = None,
        prefix: str = "",
        shared_tools: bool = True,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        busy_timeout: float = 5.0,
        max_clock_skew: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0 < min_ttl <= max_ttl:
            raise ValueError("ttl bounds must satisfy 0 < min_ttl <= max_ttl")
        if not min_ttl <= ttl <= max_ttl:
            raise ValueError(f"ttl must be between {min_ttl:g} and {max_ttl:g} seconds")
        if undo_ttl is not None and undo_ttl <= 0:
            raise ValueError("undo_ttl must be positive or None")
        if max_undo_ttl is not None:
            if max_undo_ttl <= 0:
                raise ValueError("max_undo_ttl must be positive or None")
            if undo_ttl is None or undo_ttl > max_undo_ttl:
                raise ValueError(f"undo_ttl must be at most max_undo_ttl ({max_undo_ttl:g} s)")
        if write_mode not in (None, GUARDED, DISABLED):
            raise ValueError(f"write_mode must be {GUARDED!r}, {DISABLED!r} or None")
        if max_payload_bytes <= 0 or max_result_bytes <= 0:
            raise ValueError("max_payload_bytes and max_result_bytes must be positive")
        self.server = server
        self.ledger = Ledger(
            ledger_path,
            scope=scope,
            clock=clock,
            busy_timeout=busy_timeout,
            max_clock_skew=max_clock_skew,
        )
        self.ttl = ttl
        self.undo_ttl = undo_ttl
        self.max_payload_bytes = max_payload_bytes
        self.max_result_bytes = max_result_bytes
        self._write_mode = write_mode
        self._env_var = env_var
        self._prefix = prefix
        self._ops: dict[str, _Operation] = {}
        if shared_tools:
            self._register_shared_tools()

    # -- configuration ----------------------------------------------------

    @property
    def writes_enabled(self) -> bool:
        """Whether apply and undo may run right now (re-read from the environment each time)."""
        mode = self._write_mode or os.environ.get(self._env_var, DISABLED)
        return mode.strip().lower() == GUARDED

    def _require_writes(self) -> None:
        if not self.writes_enabled:
            raise WritesDisabledError(
                f"writes are disabled; the operator must set {self._env_var}={GUARDED}"
            )

    def tool_name(self, base: str) -> str:
        """The registered name for ``base`` (``"apply_change"`` -> ``prefix + "apply_change"``)."""
        return f"{self._prefix}{base}"

    # -- registration -----------------------------------------------------

    def guarded(
        self,
        *,
        apply: ApplyFn,
        undo: UndoFn | None = None,
        observe: ObserveFn | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorate a *preview* function and register it as ``preview_<name>``.

        Args:
            apply: ``apply(payload) -> result`` performs the change and returns
                a JSON-serializable result. Never exposed as a tool.
            undo: ``undo(payload, result)`` compensates for a successful apply.
                Without it, applied changes cannot be undone.
            observe: ``observe(payload) -> snapshot`` returns JSON describing
                the state the change depends on. Called at preview, after the
                apply claim (stale approvals fail closed), after apply, and
                before undo.
            name: Operation name; defaults to the function name.
            description: Tool description; defaults to the docstring.

        The decorated function's parameters become the preview tool's input
        schema. It must validate, must not change external state, and returns
        a :class:`Proposal`; raise :class:`ChangeError` to reject. The function
        is returned unchanged so trusted code can still call it directly.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            action = name or fn.__name__
            if action in self._ops:
                raise ValueError(f"operation {action!r} already registered")
            self._ops[action] = _Operation(action, fn, apply, undo, observe)

            async def preview_tool(**kwargs: Any) -> dict[str, Any]:
                return await self.preview(action, **kwargs)

            sig = inspect.signature(fn, eval_str=True)
            preview_tool.__signature__ = sig.replace(  # type: ignore[attr-defined]
                return_annotation=dict[str, Any]
            )
            preview_tool.__name__ = f"preview_{action}"
            doc = description or inspect.getdoc(fn) or f"Propose a {action} change."
            self.server.add_tool(
                preview_tool,
                name=self.tool_name(f"preview_{action}"),
                description=(
                    f"{doc}\n\nPreview only: the target is not changed; a pending change is "
                    f"recorded. Returns a diff, a change_id "
                    f"and a single-use confirmation_token. Show the diff to the user and call "
                    f"{self.tool_name('apply_change')} only after they explicitly approve it."
                ),
                annotations=_annotations(**_PREVIEW_ANNOTATIONS),
            )
            return fn

        return decorator

    def _register_shared_tools(self) -> None:
        async def apply_change(change_id: str, confirmation_token: str) -> dict[str, Any]:
            return await self.apply(change_id, confirmation_token)

        async def undo_change(change_id: str, undo_token: str) -> dict[str, Any]:
            return await self.undo(change_id, undo_token)

        async def get_change_status(change_id: str) -> dict[str, Any]:
            return await self._db(self.status, change_id)

        self.server.add_tool(
            apply_change,
            name=self.tool_name("apply_change"),
            description=(
                "Execute a previously previewed change exactly as it was previewed. Takes no "
                "change data: only the change_id and confirmation_token from the preview. "
                "Only call this after the user has explicitly approved the diff. Each token "
                "works once and expires."
            ),
            annotations=_annotations(**_APPLY_ANNOTATIONS),
        )
        self.server.add_tool(
            undo_change,
            name=self.tool_name("undo_change"),
            description=(
                "Reverse an applied change using the undo_token returned by apply_change. "
                "Refuses if the affected state changed after the apply."
            ),
            annotations=_annotations(**_UNDO_ANNOTATIONS),
        )
        self.server.add_tool(
            get_change_status,
            name=self.tool_name("get_change_status"),
            description="Report the state of a change. Never returns tokens.",
            annotations=_annotations(**_STATUS_ANNOTATIONS),
        )

    # -- the workflow (also callable from trusted Python) ------------------

    async def _db(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking ledger call in a worker thread.

        Not abandoned on cancellation: if the caller is cancelled, the SQLite
        transaction still runs to completion (commit or rollback) before the
        cancellation is delivered, so a claim is never half-made. The
        at-most-once guarantee comes from SQLite's lock, not from the thread.
        """
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    def _op(self, action: str) -> _Operation:
        try:
            return self._ops[action]
        except KeyError:
            raise ChangeError(f"unknown operation {action!r}") from None

    async def _observe(self, op: _Operation, payload: Any) -> str | None:
        if op.observe is None:
            return None
        return canonical_hash(await _call(op.observe, payload))

    async def preview(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Run the preview function for ``action`` and store a pending change.

        Returns the agent-facing preview: ``change_id``, ``diff``,
        ``confirmation_token``, ``expires_at``, ``writes_enabled`` and
        ``next_step``. Previews work even when writes are disabled.
        """
        op = self._op(action)
        proposal = await _call(functools.partial(op.preview, **kwargs))
        if not isinstance(proposal, Proposal):
            raise TypeError(f"preview for {action!r} must return a Proposal")
        try:
            payload = _roundtrip(proposal.payload)
            diff = json.loads(json.dumps(proposal.diff, ensure_ascii=False, allow_nan=False))
            size = len(canonical_json([payload, diff, proposal.summary]).encode("utf-8"))
        except (TypeError, ValueError) as exc:  # UnicodeError is a ValueError
            raise ChangeError(f"payload and diff must be plain JSON: {exc}") from None
        if size > self.max_payload_bytes:
            raise ChangeError(
                f"proposal is {size} bytes; the limit is {self.max_payload_bytes} bytes"
            )
        observed = await self._observe(op, payload)
        record, token = await self._db(
            self.ledger.create,
            action=action,
            payload=payload,
            diff=diff,
            summary=proposal.summary,
            ttl=self.ttl,
            observed_hash=observed,
            undoable=op.undo is not None,
        )
        return {
            "status": "preview",
            "change_id": record.change_id,
            "action": action,
            "summary": record.summary,
            "diff": record.diff,
            "confirmation_token": token,
            "expires_at": record.public()["expires_at"],
            "writes_enabled": self.writes_enabled,
            "next_step": (
                f"Show this diff to the user. Only after explicit approval, call "
                f"{self.tool_name('apply_change')}(change_id, confirmation_token)."
            ),
        }

    async def apply(self, change_id: str, confirmation_token: str) -> dict[str, Any]:
        """Execute the stored payload of ``change_id`` at most once.

        Returns ``status``, ``change_id``, ``result`` and, for undoable
        operations, ``undo_token`` and ``undo_expires_at``. Raises
        :class:`WritesDisabledError`, :class:`StateChangedError` or
        :class:`ChangeError`.
        """
        # Checked before the claim so turning writes off never burns a preview.
        self._require_writes()
        op = self._op((await self._db(self.ledger.get, change_id)).action)
        record = await self._db(self.ledger.claim_apply, change_id, confirmation_token)
        try:
            if await self._observe(op, record.payload) != record.observed_hash:
                raise StateChangedError(
                    f"state changed after preview of {change_id}; create a fresh preview"
                )
            result = _roundtrip(await _call(op.apply, record.payload))
            result_size = len(canonical_json(result).encode("utf-8"))
            if result_size > self.max_result_bytes:
                raise ChangeError(
                    f"apply of {change_id} ran, but its result is {result_size} bytes (limit "
                    f"{self.max_result_bytes}); recorded as apply_failed without undo. The "
                    f"remote state needs manual review."
                )
            after = await self._observe(op, record.payload)
        except Exception as exc:
            public = _public(exc, change_id, "apply")
            await self._db(
                self.ledger.fail, change_id, Status.APPLYING, f"{type(exc).__name__}: {exc}", public
            )
            if isinstance(exc, ChangeError):
                raise
            raise ChangeError(public) from exc
        undo_token = await self._db(
            self.ledger.finish_apply,
            change_id,
            result=result,
            after_hash=after,
            undo_ttl=self.undo_ttl,
        )
        out: dict[str, Any] = {"status": "applied", "change_id": change_id, "result": result}
        if undo_token is not None:
            out["undo_token"] = undo_token
            record = await self._db(self.ledger.get, change_id)
            out["undo_expires_at"] = record.public()["undo_expires_at"]
        return out

    async def undo(self, change_id: str, undo_token: str) -> dict[str, Any]:
        """Compensate for an applied change at most once. Refuses if state changed since."""
        self._require_writes()
        op = self._op((await self._db(self.ledger.get, change_id)).action)
        if op.undo is None:
            raise ChangeError(f"change {change_id} has no undo")
        record = await self._db(self.ledger.claim_undo, change_id, undo_token)
        try:
            if await self._observe(op, record.payload) != record.after_hash:
                raise StateChangedError(
                    f"state changed after {change_id} was applied; refusing to overwrite it"
                )
            await _call(op.undo, record.payload, record.result)
        except Exception as exc:
            public = _public(exc, change_id, "undo")
            await self._db(
                self.ledger.fail, change_id, Status.UNDOING, f"{type(exc).__name__}: {exc}", public
            )
            if isinstance(exc, ChangeError):
                raise
            raise ChangeError(public) from exc
        await self._db(self.ledger.finish_undo, change_id)
        return {"status": "undone", "change_id": change_id}

    def status(self, change_id: str) -> dict[str, Any]:
        """Agent-facing state of a change (what ``get_change_status`` returns). No tokens."""
        return self.ledger.get(change_id).public()

    def record(self, change_id: str) -> ChangeRecord:
        """Full ledger row for trusted code: payload, result and raw error, but no tokens."""
        return self.ledger.get(change_id)


def _roundtrip(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _public(exc: BaseException, change_id: str, step: str) -> str:
    """Agent-safe error text: ``ChangeError`` messages pass through, anything else is
    reduced to its type; the full text stays in the ledger."""
    if isinstance(exc, ChangeError):
        return str(exc)
    return (
        f"{step} of {change_id} failed with {type(exc).__name__}; the remote state may "
        f"need manual review (details are in the server's ledger)"
    )

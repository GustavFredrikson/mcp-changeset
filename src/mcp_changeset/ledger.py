"""SQLite ledger: the state machine behind preview -> apply -> undo.

Every transition is a conditional ``UPDATE ... WHERE status = ?``. The claims
that matter (``pending -> applying`` and ``applied -> undoing``) run inside
``BEGIN IMMEDIATE`` so SQLite takes its write lock *before* the row is read,
which makes them atomic across threads and processes sharing the file.
Nothing ever returns to ``pending``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import ChangeError, LedgerError

SCHEMA_VERSION = 1
TOKEN_BYTES = 24  # 192 bits of entropy per token

__all__ = [
    "ChangeRecord",
    "Ledger",
    "Status",
    "canonical_hash",
    "canonical_json",
]


class Status(str, Enum):
    """Lifecycle states of a change. See the README for the transition diagram."""

    PENDING = "pending"
    EXPIRED = "expired"
    APPLYING = "applying"
    APPLIED = "applied"
    APPLY_FAILED = "apply_failed"
    UNDOING = "undoing"
    UNDONE = "undone"
    UNDO_FAILED = "undo_failed"


def canonical_json(value: Any) -> str:
    """Deterministic JSON (sorted keys, no whitespace, no NaN) used for storage and hashing.

    Raises ``TypeError`` or ``ValueError`` for values that are not plain JSON.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_hash(value: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_json` of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _token_hash(kind: str, change_id: str, binding: str, token: str) -> str:
    # Binds the token to its purpose, its change and the stored payload's hash:
    # editing the payload (and its hash) in the database invalidates the token.
    material = f"{kind}\x00{change_id}\x00{binding}\x00{token}".encode()
    return hashlib.sha256(material).hexdigest()


def _scope_hash(scope: str | None) -> str | None:
    return None if scope is None else hashlib.sha256(scope.encode()).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ChangeRecord:
    """A ledger row as seen by trusted server code. Never contains plaintext tokens.

    ``error`` holds the full failure text for operators; ``public_error`` is
    the sanitized version returned to agents.
    """

    change_id: str
    action: str
    status: Status
    summary: str
    payload: Any
    diff: list[dict[str, Any]]
    created_at: float
    expires_at: float
    observed_hash: str | None
    result: Any
    after_hash: str | None
    undoable: bool
    undo_expires_at: float | None
    error: str | None
    public_error: str | None
    updated_at: float

    def public(self) -> dict[str, Any]:
        """The agent-facing view returned by ``get_change_status``: state, never tokens."""
        return {
            "change_id": self.change_id,
            "action": self.action,
            "status": self.status.value,
            "summary": self.summary,
            "diff": self.diff,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "undoable": self.undoable and self.status is Status.APPLIED,
            "undo_expires_at": _iso(self.undo_expires_at) if self.undo_expires_at else None,
            "error": self.public_error,
            "updated_at": _iso(self.updated_at),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    change_id        TEXT PRIMARY KEY,
    action           TEXT NOT NULL,
    status           TEXT NOT NULL,
    summary          TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    diff_json        TEXT NOT NULL,
    scope_hash       TEXT,
    observed_hash    TEXT,
    apply_token_hash TEXT NOT NULL,
    created_at       REAL NOT NULL,
    expires_at       REAL NOT NULL,
    result_json      TEXT,
    after_hash       TEXT,
    undoable         INTEGER NOT NULL,
    undo_token_hash  TEXT,
    undo_expires_at  REAL,
    error            TEXT,
    public_error     TEXT,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS changes_status ON changes(status);
"""


class Ledger:
    """Durable, multi-process record of proposed and executed changes.

    Plaintext tokens are returned exactly once (from :meth:`create` and
    :meth:`finish_apply`); only a SHA-256 of each is stored.

    Args:
        path: SQLite file. Created ``0600``; a missing parent directory is
            created ``0700``.
        scope: Optional identity (for example an account id) hashed into
            every row. Claims made under a different scope are refused.
        clock: Wall-clock source in Unix seconds. Injectable for tests.
        busy_timeout: Seconds to wait for another connection's write lock
            before raising :class:`LedgerError`.
        create: Create the file and schema if missing (default). With
            ``create=False`` the file must already be an mcp-changeset ledger
            of the current schema version; used by the CLI.
        max_clock_skew: A claim is refused when the clock reads more than
            this many seconds *before* the row was created (the clock moved
            backwards, which would otherwise extend a token's life).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        scope: str | None = None,
        clock: Callable[[], float] = time.time,
        busy_timeout: float = 5.0,
        max_clock_skew: float = 60.0,
        create: bool = True,
    ) -> None:
        self.path = Path(path).expanduser()
        self._scope_hash = _scope_hash(scope)
        self._clock = clock
        self._busy_timeout = busy_timeout
        self._max_clock_skew = max_clock_skew
        if create:
            self._init_db()
        else:
            self._open_existing()

    # -- plumbing ---------------------------------------------------------

    def _init_db(self) -> None:
        try:
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, mode=0o700)
            if self.path.is_symlink():
                raise LedgerError("ledger path is a symlink; refusing")
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            try:
                # O_EXCL | O_NOFOLLOW: never create through, or follow, a symlink.
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow, 0o600)
                os.close(fd)
            except FileExistsError:
                if self.path.is_symlink():  # e.g. a dangling symlink planted at the path
                    raise LedgerError("ledger path is a symlink; refusing") from None
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise LedgerError(f"ledger file cannot be created ({type(exc).__name__})") from exc
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise LedgerError(f"unsupported ledger schema version {version}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            if version == 0:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _open_existing(self) -> None:
        if self.path.is_symlink():
            raise LedgerError("ledger path is a symlink; refusing")
        if not self.path.is_file():
            raise LedgerError("no ledger file at the given path")
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'changes'"
            ).fetchone()
        if version != SCHEMA_VERSION or has_table is None:
            raise LedgerError("file is not an mcp-changeset ledger of a supported version")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            conn = sqlite3.connect(self.path, timeout=self._busy_timeout, isolation_level=None)
        except sqlite3.Error as exc:
            raise LedgerError(_db_message(exc)) from exc
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        except sqlite3.Error as exc:
            raise LedgerError(_db_message(exc)) from exc
        finally:
            conn.close()

    @contextmanager
    def _immediate(self) -> Generator[sqlite3.Connection, None, None]:
        """A write transaction whose lock is taken before the first read."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _row(self, conn: sqlite3.Connection, change_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM changes WHERE change_id = ?", (change_id,)
        ).fetchone()
        if row is None:
            raise ChangeError(f"unknown change {change_id!r}")
        return row

    def _transition(
        self, conn: sqlite3.Connection, change_id: str, frm: Status, to: Status, **fields: Any
    ) -> None:
        # Column names come from this module only, never from callers' data.
        sets = ", ".join(f"{k} = ?" for k in ("status", "updated_at", *fields))
        cur = conn.execute(
            f"UPDATE changes SET {sets} WHERE change_id = ? AND status = ?",  # noqa: S608
            (to.value, self._clock(), *fields.values(), change_id, frm.value),
        )
        if cur.rowcount != 1:  # pragma: no cover - guarded by the claim's lock
            raise ChangeError(f"change {change_id} is no longer {frm.value}")

    def _check_clock(self, row: sqlite3.Row) -> None:
        if self._clock() + self._max_clock_skew < row["created_at"]:
            raise ChangeError(
                "the server clock is earlier than this change's creation time; refusing "
                "(clock moved backwards). Create a fresh preview."
            )

    def _check_integrity(self, row: sqlite3.Row) -> None:
        if row["scope_hash"] != self._scope_hash:
            raise ChangeError("change was previewed under a different scope")
        try:
            intact = canonical_hash(json.loads(row["payload_json"])) == row["payload_hash"]
        except ValueError:
            intact = False
        if not intact:
            raise ChangeError("stored payload does not match its hash")

    # -- reads ------------------------------------------------------------

    def get(self, change_id: str) -> ChangeRecord:
        """Return one change. Raises :class:`ChangeError` if it does not exist."""
        with self._connect() as conn:
            row = self._row(conn, change_id)
        return self._record(row)

    def list_changes(self, status: Status | None = None) -> list[ChangeRecord]:
        """All changes (optionally filtered by status), oldest first."""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM changes ORDER BY created_at").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM changes WHERE status = ? ORDER BY created_at", (status.value,)
                ).fetchall()
        return [self._record(r) for r in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> ChangeRecord:
        try:
            return Ledger._decode(row)
        except (ValueError, KeyError) as exc:
            raise LedgerError(f"ledger row {row['change_id']} is corrupt") from exc

    @staticmethod
    def _decode(row: sqlite3.Row) -> ChangeRecord:
        return ChangeRecord(
            change_id=row["change_id"],
            action=row["action"],
            status=Status(row["status"]),
            summary=row["summary"],
            payload=json.loads(row["payload_json"]),
            diff=json.loads(row["diff_json"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            observed_hash=row["observed_hash"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            after_hash=row["after_hash"],
            undoable=bool(row["undoable"]),
            undo_expires_at=row["undo_expires_at"],
            error=row["error"],
            public_error=row["public_error"],
            updated_at=row["updated_at"],
        )

    # -- preview ----------------------------------------------------------

    def create(
        self,
        *,
        action: str,
        payload: Any,
        diff: list[dict[str, Any]],
        summary: str,
        ttl: float,
        observed_hash: str | None,
        undoable: bool,
    ) -> tuple[ChangeRecord, str]:
        """Store a ``pending`` change and return it with its plaintext confirmation token."""
        change_id = "chg_" + secrets.token_hex(10)
        token = "confirm_" + secrets.token_urlsafe(TOKEN_BYTES)
        payload_json = canonical_json(payload)
        payload_hash = canonical_hash(payload)
        now = self._clock()
        with self._immediate() as conn:
            conn.execute(
                """INSERT INTO changes (change_id, action, status, summary, payload_json,
                       payload_hash, diff_json, scope_hash, observed_hash, apply_token_hash,
                       created_at, expires_at, undoable, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    change_id,
                    action,
                    Status.PENDING.value,
                    summary,
                    payload_json,
                    payload_hash,
                    json.dumps(diff, ensure_ascii=False),
                    self._scope_hash,
                    observed_hash,
                    _token_hash("apply", change_id, payload_hash, token),
                    now,
                    now + ttl,
                    int(undoable),
                    now,
                ),
            )
        return self.get(change_id), token

    # -- apply ------------------------------------------------------------

    def claim_apply(self, change_id: str, token: str) -> ChangeRecord:
        """``pending -> applying``, committed before any external call is made.

        A wrong token leaves the change ``pending``. The right token consumes
        it, whatever happens next. Raises :class:`ChangeError` on refusal.
        """
        expired = False
        with self._immediate() as conn:
            row = self._row(conn, change_id)
            status = Status(row["status"])
            if status is not Status.PENDING:
                raise ChangeError(f"change {change_id} cannot be applied (status: {status.value})")
            if self._clock() >= row["expires_at"]:
                self._transition(conn, change_id, Status.PENDING, Status.EXPIRED)
                expired = True
            else:
                self._check_clock(row)
                self._check_integrity(row)
                expected = _token_hash("apply", change_id, row["payload_hash"], token)
                if not hmac.compare_digest(row["apply_token_hash"], expected):
                    raise ChangeError("invalid confirmation token")
                self._transition(conn, change_id, Status.PENDING, Status.APPLYING)
        if expired:
            raise ChangeError(f"change {change_id} expired; create a fresh preview")
        return self.get(change_id)

    def finish_apply(
        self, change_id: str, *, result: Any, after_hash: str | None, undo_ttl: float | None
    ) -> str | None:
        """``applying -> applied``. Returns a plaintext undo token if the change is undoable."""
        with self._immediate() as conn:
            row = self._row(conn, change_id)
            undo_token = undo_hash = None
            undo_expires = None
            if row["undoable"]:
                undo_token = "undo_" + secrets.token_urlsafe(TOKEN_BYTES)
                undo_hash = _token_hash("undo", change_id, row["payload_hash"], undo_token)
                undo_expires = self._clock() + undo_ttl if undo_ttl is not None else None
            self._transition(
                conn,
                change_id,
                Status.APPLYING,
                Status.APPLIED,
                result_json=canonical_json(result),
                after_hash=after_hash,
                undo_token_hash=undo_hash,
                undo_expires_at=undo_expires,
            )
        return undo_token

    def fail(self, change_id: str, frm: Status, error: str, public_error: str) -> None:
        """``applying -> apply_failed`` or ``undoing -> undo_failed``.

        ``error`` is kept for operators; ``public_error`` is what agents see.
        """
        to = Status.APPLY_FAILED if frm is Status.APPLYING else Status.UNDO_FAILED
        with self._immediate() as conn:
            self._transition(
                conn, change_id, frm, to, error=error[:2000], public_error=public_error[:500]
            )

    # -- undo -------------------------------------------------------------

    def claim_undo(self, change_id: str, token: str) -> ChangeRecord:
        """``applied -> undoing``, with the same rules as :meth:`claim_apply`."""
        with self._immediate() as conn:
            row = self._row(conn, change_id)
            status = Status(row["status"])
            if not row["undoable"]:
                raise ChangeError(f"change {change_id} has no undo")
            if status is not Status.APPLIED:
                raise ChangeError(f"change {change_id} cannot be undone (status: {status.value})")
            if row["undo_expires_at"] is not None and self._clock() >= row["undo_expires_at"]:
                raise ChangeError(f"undo window for change {change_id} has closed")
            self._check_clock(row)
            self._check_integrity(row)
            expected = _token_hash("undo", change_id, row["payload_hash"], token)
            if not hmac.compare_digest(row["undo_token_hash"] or "", expected):
                raise ChangeError("invalid undo token")
            self._transition(conn, change_id, Status.APPLIED, Status.UNDOING)
        return self.get(change_id)

    def finish_undo(self, change_id: str) -> None:
        """``undoing -> undone``."""
        with self._immediate() as conn:
            self._transition(conn, change_id, Status.UNDOING, Status.UNDONE)

    # -- operator actions -------------------------------------------------

    def reconcile(self, change_id: str, note: str) -> ChangeRecord:
        """Close a change stuck in ``applying`` or ``undoing`` after a crash or cancellation.

        ``applying -> apply_failed`` or ``undoing -> undo_failed``. Call this only
        after checking the remote state by hand: the ledger cannot know whether
        the interrupted write happened. ``note`` is recorded for operators. The
        change is never made retryable; preview again if it still needs doing.
        """
        note = note.strip()
        if not note:
            raise ValueError("a reconciliation note is required")
        with self._immediate() as conn:
            status = Status(self._row(conn, change_id)["status"])
            if status not in (Status.APPLYING, Status.UNDOING):
                raise ChangeError(
                    f"change {change_id} is {status.value}; only applying or undoing "
                    "changes can be reconciled"
                )
            to = Status.APPLY_FAILED if status is Status.APPLYING else Status.UNDO_FAILED
            self._transition(
                conn,
                change_id,
                status,
                to,
                error=f"reconciled by operator: {note}"[:2000],
                public_error="interrupted; reconciled by the operator",
            )
        return self.get(change_id)

    def prune(
        self, older_than: float, *, include_applied: bool = False, dry_run: bool = False
    ) -> int:
        """Delete finished changes last updated more than ``older_than`` seconds ago.

        Always eligible: ``expired``, ``apply_failed``, ``undone``,
        ``undo_failed``. With ``include_applied=True``, ``applied`` changes are
        eligible too, but only once they can no longer be undone (no undo, or
        the undo window has closed); rows with a never-expiring undo window are
        kept. ``pending``, ``applying`` and ``undoing`` rows are never deleted.
        Returns the number of deleted rows (with ``dry_run=True``, the number
        that would be deleted; nothing is changed).
        """
        if older_than < 0:
            raise ValueError("older_than must be >= 0")
        now = self._clock()
        cutoff = now - older_than
        finished = (
            Status.EXPIRED.value,
            Status.APPLY_FAILED.value,
            Status.UNDONE.value,
            Status.UNDO_FAILED.value,
        )
        where = "updated_at < ? AND status IN (?, ?, ?, ?)"
        params: tuple[Any, ...] = (cutoff, *finished)
        if include_applied:
            where = (
                f"({where}) OR (updated_at < ? AND status = ? AND "
                "(undoable = 0 OR (undo_expires_at IS NOT NULL AND undo_expires_at <= ?)))"
            )
            params += (cutoff, Status.APPLIED.value, now)
        verb = "SELECT COUNT(*) FROM" if dry_run else "DELETE FROM"
        with self._immediate() as conn:
            # `where` is assembled from constants above; values are bound.
            cur = conn.execute(f"{verb} changes WHERE {where}", params)
            return int(cur.fetchone()[0]) if dry_run else cur.rowcount

    # -- housekeeping -----------------------------------------------------

    def expire_stale(self) -> int:
        """Mark every ``pending`` change past its TTL as ``expired``. Returns the count."""
        now = self._clock()
        with self._immediate() as conn:
            cur = conn.execute(
                "UPDATE changes SET status = ?, updated_at = ? "
                "WHERE status = ? AND expires_at <= ?",
                (Status.EXPIRED.value, now, Status.PENDING.value, now),
            )
            return cur.rowcount


def _db_message(exc: sqlite3.Error) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "ledger is busy (locked by another process); try again"
    if "not a database" in text or "malformed" in text or "corrupt" in text:
        return "ledger file is corrupt or not a SQLite database"
    if "unable to open" in text or "readonly" in text or "read-only" in text:
        return "ledger file cannot be opened for writing"
    return f"ledger error ({type(exc).__name__})"

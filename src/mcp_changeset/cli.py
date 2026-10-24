"""``mcp-changeset``: inspect a ledger and resolve what software can't.

    mcp-changeset --ledger PATH list [--status applying] [--json]
    mcp-changeset --ledger PATH show CHANGE_ID [--json]
    mcp-changeset --ledger PATH reconcile CHANGE_ID --note "checked remote: not written" --yes
    mcp-changeset --ledger PATH prune --older-than-days 30 [--include-applied] --yes
    mcp-changeset --ledger PATH expire --yes

Commands that change the ledger do nothing without ``--yes``; they print what
they would do and exit with status 2. The CLI never creates a ledger and never
shows tokens (the ledger stores only their hashes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, TextIO, cast

from . import __version__
from .errors import ChangeError
from .ledger import ChangeRecord, Ledger, Status

LEDGER_ENV_VAR = "MCP_CHANGESET_LEDGER"
EXIT_OK, EXIT_ERROR, EXIT_NEEDS_CONFIRMATION = 0, 1, 2


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _operator_view(record: ChangeRecord) -> dict[str, Any]:
    """Everything an operator needs; still no tokens (they are not stored)."""
    view = record.public()
    view.update(
        payload=record.payload,
        result=record.result,
        error=record.error,
        public_error=record.public_error,
        undoable=record.undoable,
        undo_expires_at=_iso(record.undo_expires_at),
    )
    return view


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-changeset",
        description="Inspect and maintain an mcp-changeset ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--ledger",
        default=os.environ.get(LEDGER_ENV_VAR),
        help=f"path to the ledger file (default: ${LEDGER_ENV_VAR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ls = sub.add_parser("list", help="list changes, oldest first")
    ls.add_argument("--status", choices=[s.value for s in Status])
    ls.add_argument("--json", action="store_true", help="one JSON object per line")

    show = sub.add_parser("show", help="show one change, including payload and raw error")
    show.add_argument("change_id")
    show.add_argument("--json", action="store_true")

    rec = sub.add_parser(
        "reconcile",
        help="mark a change stuck in applying/undoing as apply_failed/undo_failed",
    )
    rec.add_argument("change_id")
    rec.add_argument("--note", required=True, help="what you checked and found (recorded)")
    rec.add_argument("--yes", action="store_true", help="actually change the ledger")

    prune = sub.add_parser("prune", help="delete old finished changes")
    prune.add_argument("--older-than-days", type=float, required=True)
    prune.add_argument(
        "--include-applied",
        action="store_true",
        help="also delete applied changes whose undo window has closed",
    )
    prune.add_argument("--yes", action="store_true", help="actually delete")

    expire = sub.add_parser(
        "expire",
        help="mark pending changes past their TTL as expired (so prune can delete them)",
    )
    expire.add_argument("--yes", action="store_true", help="actually change the ledger")
    return parser


def _print_table(records: Sequence[ChangeRecord], out: TextIO) -> None:
    out.write(f"{'CHANGE_ID':<26} {'STATUS':<12} {'UPDATED (UTC)':<25} {'ACTION':<20} SUMMARY\n")
    for r in records:
        summary = r.summary if len(r.summary) <= 60 else r.summary[:57] + "..."
        out.write(
            f"{r.change_id:<26} {r.status.value:<12} {_iso(r.updated_at) or '':<25} "
            f"{r.action:<20} {summary}\n"
        )


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Entry point. Returns the process exit status."""
    out = cast(TextIO, out or sys.stdout)
    err = cast(TextIO, sys.stderr)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.ledger:
        parser.error(f"--ledger is required (or set ${LEDGER_ENV_VAR})")
    try:
        ledger = Ledger(args.ledger, create=False)
        if args.command == "list":
            status = Status(args.status) if args.status else None
            records = ledger.list_changes(status)
            if args.json:
                for r in records:
                    out.write(json.dumps(_operator_view(r), ensure_ascii=False) + "\n")
            else:
                _print_table(records, out)
            return EXIT_OK
        if args.command == "show":
            view = _operator_view(ledger.get(args.change_id))
            if args.json:
                out.write(json.dumps(view, ensure_ascii=False, indent=2) + "\n")
            else:
                for key, value in view.items():
                    shown = value if isinstance(value, str) else json.dumps(value)
                    out.write(f"{key:>16}: {shown}\n")
            return EXIT_OK
        if args.command == "reconcile":
            record = ledger.get(args.change_id)
            target = {Status.APPLYING: "apply_failed", Status.UNDOING: "undo_failed"}.get(
                record.status
            )
            if target is None:
                err.write(
                    f"change {record.change_id} is {record.status.value}; only applying or "
                    "undoing changes can be reconciled\n"
                )
                return EXIT_ERROR
            if not args.yes:
                out.write(
                    f"would mark {record.change_id} ({record.action}, {record.status.value}) "
                    f"as {target} with note {args.note!r}.\nCheck the remote state first, "
                    "then re-run with --yes.\n"
                )
                return EXIT_NEEDS_CONFIRMATION
            done = ledger.reconcile(record.change_id, args.note)
            out.write(f"{done.change_id}: {record.status.value} -> {done.status.value}\n")
            return EXIT_OK
        if args.command == "expire":
            # Pending rows are never pruned, and a stale one only turns expired
            # when someone tries to apply it. This sweeps them all.
            if not args.yes:
                now = time.time()
                count = sum(1 for r in ledger.list_changes(Status.PENDING) if r.expires_at <= now)
                out.write(f"would expire {count} change(s); re-run with --yes to expire.\n")
                return EXIT_NEEDS_CONFIRMATION
            out.write(f"expired {ledger.expire_stale()} change(s).\n")
            return EXIT_OK
        # prune
        seconds = args.older_than_days * 86400
        count = ledger.prune(seconds, include_applied=args.include_applied, dry_run=not args.yes)
        if not args.yes:
            out.write(f"would delete {count} change(s); re-run with --yes to delete.\n")
            return EXIT_NEEDS_CONFIRMATION
        out.write(f"deleted {count} change(s).\n")
        return EXIT_OK
    except (ChangeError, ValueError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

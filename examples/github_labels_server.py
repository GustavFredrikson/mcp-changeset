"""Guarded edits to GitHub issue labels: preview -> apply -> undo against a real API.

    GITHUB_TOKEN=... GITHUB_REPOS=owner/repo,owner/other \\
    MCP_CHANGESET_WRITE_MODE=guarded python examples/github_labels_server.py

The token needs "Issues: read and write" on the listed repositories. Only
existing labels can be added (GitHub would otherwise create new labels
silently). Uses only the standard library for HTTP; the test suite replaces
the transport, so it never touches the network.

Limits worth knowing: GitHub has no conditional write for labels, so the
observe re-check narrows but does not close the race with a concurrent
edit; and repository labels are read from the first page (100) only.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

import anyio.to_thread

from mcp_changeset import ChangeError, Guard, Proposal

from _compat import new_server

API = "https://api.github.com"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
MAX_LABEL_LEN = 50
MAX_LABELS = 100

Send = Callable[[str, str, dict[str, str], bytes | None], Awaitable[tuple[int, bytes]]]


class GitHubError(Exception):
    """An HTTP error from GitHub. Its text never reaches the agent from apply/undo."""

    def __init__(self, status: int, method: str, path: str) -> None:
        super().__init__(f"GitHub returned HTTP {status} for {method} {path}")
        self.status = status


async def urllib_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """Default transport: urllib in a worker thread."""

    def send() -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                return int(response.status), bytes(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), bytes(exc.read() or b"")

    return await anyio.to_thread.run_sync(send)


class GitHubLabels:
    """The three REST calls this server needs."""

    def __init__(self, token: str, *, api: str = API, send: Send = urllib_send) -> None:
        if not token:
            raise ValueError("a GitHub token is required")
        self._token = token
        self._api = api.rstrip("/")
        self._send = send

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mcp-changeset-example",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, raw = await self._send(method, self._api + path, headers, data)
        if status >= 400:
            raise GitHubError(status, method, path)
        return json.loads(raw) if raw else None

    async def repo_labels(self, repo: str) -> set[str]:
        labels = await self._call("GET", f"/repos/{repo}/labels?per_page=100")
        return {label["name"] for label in labels}

    async def issue_labels(self, repo: str, number: int) -> list[str]:
        labels = await self._call("GET", f"/repos/{repo}/issues/{number}/labels?per_page=100")
        return sorted(label["name"] for label in labels)

    async def set_issue_labels(self, repo: str, number: int, labels: list[str]) -> list[str]:
        result = await self._call(
            "PUT", f"/repos/{repo}/issues/{number}/labels", {"labels": labels}
        )
        return sorted(label["name"] for label in result)


def _clean(labels: Iterable[str]) -> set[str]:
    out = {label.strip() for label in labels}
    if any(not label or len(label) > MAX_LABEL_LEN for label in out):
        raise ChangeError(f"labels must be 1-{MAX_LABEL_LEN} characters")
    return out


def build_server(
    github: GitHubLabels, allowed_repos: Iterable[str], ledger_path: str | Path, **guard_kwargs: Any
) -> tuple[Any, Guard]:
    """Build the labels server. ``allowed_repos`` is fixed by the operator, never the agent."""
    allowed = frozenset(allowed_repos)
    if not allowed or any(not REPO_RE.fullmatch(r) for r in allowed):
        raise ValueError("allowed_repos must be a non-empty set of 'owner/name' strings")
    server = new_server("github-labels")
    guard = Guard(server, ledger_path=ledger_path, **guard_kwargs)

    def check_target(repo: str, number: int) -> None:
        if repo not in allowed:
            raise ChangeError(f"repository {repo!r} is not enabled on this server")
        if number < 1:
            raise ChangeError("issue_number must be a positive integer")

    async def read_preview(repo: str, number: int) -> list[str]:
        try:
            return await github.issue_labels(repo, number)
        except GitHubError as exc:
            raise ChangeError(f"could not read issue {repo}#{number} (HTTP {exc.status})") from None

    async def get_issue_labels(repo: str, issue_number: int) -> list[str]:
        """Current labels on an issue or pull request."""
        check_target(repo, issue_number)
        return await read_preview(repo, issue_number)

    server.add_tool(get_issue_labels)

    async def observe(payload: dict[str, Any]) -> list[str]:
        return await github.issue_labels(payload["repo"], payload["number"])

    async def apply(payload: dict[str, Any]) -> dict[str, Any]:
        now = await github.set_issue_labels(payload["repo"], payload["number"], payload["labels"])
        return {"previous": payload["before"], "labels": now}

    async def undo(payload: dict[str, Any], result: dict[str, Any]) -> None:
        await github.set_issue_labels(payload["repo"], payload["number"], result["previous"])

    @guard.guarded(apply=apply, undo=undo, observe=observe)
    async def set_issue_labels(
        repo: str,
        issue_number: int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> Proposal:
        """Add and/or remove labels on an issue or pull request (existing labels only)."""
        check_target(repo, issue_number)
        to_add, to_remove = _clean(add or []), _clean(remove or [])
        if to_add & to_remove:
            raise ChangeError("a label cannot be both added and removed")
        before = await read_preview(repo, issue_number)
        try:
            known = await github.repo_labels(repo)
        except GitHubError as exc:
            raise ChangeError(f"could not read labels of {repo} (HTTP {exc.status})") from None
        unknown = sorted(to_add - known)
        if unknown:
            raise ChangeError(f"unknown labels in {repo}: {', '.join(unknown)}")
        after = sorted((set(before) | to_add) - to_remove)
        if after == before:
            raise ChangeError("nothing to change")
        if len(after) > MAX_LABELS:
            raise ChangeError(f"at most {MAX_LABELS} labels per issue")
        return Proposal(
            payload={"repo": repo, "number": issue_number, "labels": after, "before": before},
            diff=[{"field": "labels", "before": before, "after": after}],
            summary=(
                f"{repo}#{issue_number}: +{sorted(to_add - set(before))} "
                f"-{sorted(to_remove & set(before))}"
            ),
        )

    return server, guard


if __name__ == "__main__":
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    repos = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]
    client = GitHubLabels(os.environ.get("GITHUB_TOKEN", ""))
    server, _ = build_server(
        client, repos, state / "mcp-changeset-github" / "ledger.db", scope=",".join(sorted(repos))
    )
    server.run()

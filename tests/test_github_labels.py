"""The GitHub labels example against an in-memory fake of the REST API (no network)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from mcp_changeset import ChangeError, StateChangedError

from github_labels_server import GitHubError, GitHubLabels, build_server, urllib_send

pytestmark = pytest.mark.anyio
TOKEN = "ghp_exampletokenexampletoken0000"  # fake, never sent anywhere


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class FakeGitHub:
    """Just enough of api.github.com for labels, with the same request shapes."""

    def __init__(self) -> None:
        self.repo_labels = {"acme/app": {"bug", "docs", "needs-triage", "good first issue"}}
        self.issues: dict[tuple[str, int], set[str]] = {("acme/app", 7): {"needs-triage"}}
        self.requests: list[tuple[str, str]] = []

    async def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        path = url.removeprefix("https://api.github.com")
        self.requests.append((method, path.split("?")[0]))
        parts = path.split("?")[0].strip("/").split("/")
        repo = f"{parts[1]}/{parts[2]}"
        if parts[3] == "labels" and method == "GET":
            return 200, self._labels(self.repo_labels.get(repo, set()))
        number = int(parts[4])
        if (repo, number) not in self.issues:
            return 404, b'{"message": "Not Found"}'
        if method == "GET":
            return 200, self._labels(self.issues[(repo, number)])
        assert method == "PUT" and headers["Content-Type"] == "application/json"
        self.issues[(repo, number)] = set(json.loads(body or b"{}")["labels"])
        return 200, self._labels(self.issues[(repo, number)])

    @staticmethod
    def _labels(names: set[str]) -> bytes:
        return json.dumps([{"name": n, "color": "ededed"} for n in sorted(names)]).encode()


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def labels_server(fake, tmp_path, clock):
    client = GitHubLabels(TOKEN, send=fake.send)
    return build_server(
        client, ["acme/app"], tmp_path / "ledger.db", write_mode="guarded", clock=clock
    )


async def test_preview_apply_undo(fake, labels_server):
    _, guard = labels_server
    p = await guard.preview(
        "set_issue_labels", repo="acme/app", issue_number=7, add=["bug"], remove=["needs-triage"]
    )
    assert p["diff"] == [{"field": "labels", "before": ["needs-triage"], "after": ["bug"]}]
    assert ("PUT", "/repos/acme/app/issues/7/labels") not in fake.requests  # preview never writes
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    assert fake.issues[("acme/app", 7)] == {"bug"}
    assert a["result"] == {"previous": ["needs-triage"], "labels": ["bug"]}
    await guard.undo(p["change_id"], a["undo_token"])
    assert fake.issues[("acme/app", 7)] == {"needs-triage"}


async def test_concurrent_edit_on_github_fails_closed(fake, labels_server):
    _, guard = labels_server
    p = await guard.preview("set_issue_labels", repo="acme/app", issue_number=7, add=["docs"])
    fake.issues[("acme/app", 7)].add("bug")  # someone labels the issue in the web UI
    with pytest.raises(StateChangedError):
        await guard.apply(p["change_id"], p["confirmation_token"])
    assert fake.issues[("acme/app", 7)] == {"needs-triage", "bug"}
    assert ("PUT", "/repos/acme/app/issues/7/labels") not in fake.requests


async def test_undo_refuses_after_later_edit(fake, labels_server):
    _, guard = labels_server
    p = await guard.preview("set_issue_labels", repo="acme/app", issue_number=7, add=["docs"])
    a = await guard.apply(p["change_id"], p["confirmation_token"])
    fake.issues[("acme/app", 7)].discard("needs-triage")
    with pytest.raises(StateChangedError):
        await guard.undo(p["change_id"], a["undo_token"])
    assert fake.issues[("acme/app", 7)] == {"docs"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repo": "evil/repo", "issue_number": 7, "add": ["bug"]}, "not enabled"),
        ({"repo": "acme/app", "issue_number": 0, "add": ["bug"]}, "positive integer"),
        ({"repo": "acme/app", "issue_number": 7, "add": ["wontfix"]}, "unknown labels"),
        ({"repo": "acme/app", "issue_number": 7, "add": ["bug"], "remove": ["bug"]}, "both"),
        ({"repo": "acme/app", "issue_number": 7, "add": ["needs-triage"]}, "nothing to change"),
        ({"repo": "acme/app", "issue_number": 7, "add": ["x" * 51]}, "1-50 characters"),
        ({"repo": "acme/app", "issue_number": 99, "add": ["bug"]}, r"HTTP 404"),
    ],
)
async def test_preview_rejections(fake, labels_server, kwargs, message):
    _, guard = labels_server
    with pytest.raises(ChangeError, match=message):
        await guard.preview("set_issue_labels", **kwargs)
    assert guard.ledger.list_changes() == []


async def test_repo_labels_unreadable(fake, tmp_path):
    original = fake.send
    calls = 0

    async def flaky(method, url, headers, body):
        nonlocal calls
        calls += 1
        if calls == 2:  # the repo-labels call
            return 403, b"{}"
        return await original(method, url, headers, body)

    client = GitHubLabels(TOKEN, send=flaky)
    _, guard = build_server(client, ["acme/app"], tmp_path / "l.db", write_mode="guarded")
    with pytest.raises(ChangeError, match="could not read labels of acme/app \\(HTTP 403\\)"):
        await guard.preview("set_issue_labels", repo="acme/app", issue_number=7, add=["bug"])


async def test_github_error_in_apply_is_not_leaked(fake, labels_server, clock):
    _, guard = labels_server
    p = await guard.preview("set_issue_labels", repo="acme/app", issue_number=7, add=["bug"])
    original = fake.send

    async def fail_put(method, url, headers, body):
        if method == "PUT":
            return 502, b'{"message": "Bad gateway"}'
        return await original(method, url, headers, body)

    # A second server process on the same ledger whose GitHub calls fail on write.
    client = GitHubLabels(TOKEN, send=fail_put)
    _, guard2 = build_server(
        client, ["acme/app"], guard.ledger.path, write_mode="guarded", clock=clock
    )
    with pytest.raises(ChangeError, match="failed with GitHubError") as info:
        await guard2.apply(p["change_id"], p["confirmation_token"])
    assert TOKEN not in str(info.value) and "502" not in str(info.value)
    assert "HTTP 502" in (guard2.record(p["change_id"]).error or "")
    assert guard2.status(p["change_id"])["status"] == "apply_failed"


async def test_read_tool_over_mcp(labels_server):
    from test_client_session import call, in_memory

    server, _ = labels_server
    async with in_memory(server) as client:
        err, labels = await call(
            client, "get_issue_labels", {"repo": "acme/app", "issue_number": 7}
        )
        assert not err and "needs-triage" in str(labels)
        err, msg = await call(client, "get_issue_labels", {"repo": "x/y", "issue_number": 7})
        assert err and "not enabled" in str(msg)


def test_configuration_errors(tmp_path):
    with pytest.raises(ValueError, match="token"):
        GitHubLabels("")
    client = GitHubLabels(TOKEN)
    for repos in ([], ["not a repo"], ["a/b/c"]):
        with pytest.raises(ValueError, match="allowed_repos"):
            build_server(client, repos, tmp_path / "l.db")
    assert GitHubError(500, "GET", "/x").status == 500


async def test_urllib_transport_without_network(monkeypatch):
    sent: list[urllib.request.Request] = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

    def fake_urlopen(request, timeout):
        sent.append(request)
        if request.get_method() == "PUT":
            raise urllib.error.HTTPError(request.full_url, 422, "nope", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
        return Response(b'[{"name": "bug"}]')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GitHubLabels(TOKEN, api="https://example.invalid")
    assert await client.issue_labels("acme/app", 1) == ["bug"]
    with pytest.raises(GitHubError, match="HTTP 422"):
        await client.set_issue_labels("acme/app", 1, ["bug"])
    assert sent[0].full_url == "https://example.invalid/repos/acme/app/issues/1/labels?per_page=100"
    assert sent[0].get_header("Authorization") == f"Bearer {TOKEN}"
    status, body = await urllib_send("GET", "https://example.invalid/x", {}, None)
    assert (status, body) == (200, b'[{"name": "bug"}]')

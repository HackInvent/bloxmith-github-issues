"""FB9/FB10: wallet precedence, action scopes, revisions, pagination and uncertain outcomes."""

import json
import os
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).parent)]
from blocs.github_issues.block import GitHubIssuesBlock
from blocs.github_issues.transport import GitHubIssuesBlockError, revision, validate_url
from bloxsmith_app.block_api import BlockRuntimeContext
from ticket_fixture import github, TOKEN


def execute(server, payload, config=None, services=None):
    settings = {"repo": "fixture/project", "api_base_url": server.base_url, "token": TOKEN,
                "dry_run": False, "error_mode": "result", **(config or {})}
    context = BlockRuntimeContext(run_id="unit", node_id="tickets", kind="github_issues", title="Tickets",
        config=MappingProxyType(settings), inputs={"actions": json.dumps(payload)}, input_message="",
        input_content_types={}, input_ports=(SimpleNamespace(id=1, name="actions"),),
        output_ports=(SimpleNamespace(id=1, name="result"), SimpleNamespace(id=2, name="summary")),
        root_dir=ROOT, run_dir=ROOT, services=services or {})
    result = GitHubIssuesBlock().execute_runtime(context)
    assert result.status == "success", (result.error, result.logs)
    assert TOKEN not in "\n".join(result.logs)
    decoded = json.loads(result.outputs[0].value)
    assert TOKEN not in json.dumps(decoded)
    return decoded


def main():
    with github() as server:
        request = {"request_id": "read-1", "action": "list_issues", "page": 2, "per_page": 5,
                   "labels": "support,triage", "assignee": "tester", "since": "2026-01-01T00:00:00Z"}
        result = execute(server, request)
        assert result["ok"] and result["request_id"] == "read-1" and result["has_next_page"]
        assert result["page"] == 2 and result["per_page"] == 5
        assert server.calls[-1]["query"]["labels"] == ["support,triage"]
        assert server.calls[-1]["query"]["page"] == ["2"]
        seen = []
        def resolver(reference):
            seen.append(reference)
            return TOKEN
        result = execute(server, {"action": "view_issue", "issue_number": 7},
            {"token_ref": "secret://workspace/github", "token": "wrong-legacy-token"}, {"resolve_secret": resolver})
        assert seen == ["secret://workspace/github"] and result["ok"]
        assert server.calls[-1]["authorization"] == "Bearer " + TOKEN
        snapshot = result["revision"]
        assert snapshot == revision(server.issue)
        before = len(server.calls)
        mismatch = execute(server, {**request, "expected_target": {"repo": "wrong/repository", "api_base_url": server.base_url}})
        assert mismatch["code"] == "scope" and len(server.calls) == before
        missing = execute(server, request, {"token_ref": "secret://workspace/github"})
        assert not missing["ok"] and missing["code"] == "secret" and len(server.calls) == before
        def unavailable(reference):
            raise RuntimeError("Do not leak " + TOKEN)
        assert execute(server, request, {"token_ref": "secret://workspace/github"},
                       {"resolve_secret": unavailable})["code"] == "secret"
        assert len(server.calls) == before
        write = {"request_id": "approval-1", "action": "comment_issue", "issue_number": 7,
                 "body": "Reviewed reply", "expected_revision": snapshot}
        refused = execute(server, write, {"allowed_actions": "list_issues,view_issue"})
        assert refused["code"] == "scope" and not refused["uncertain"] and len(server.calls) == before
        assert execute(server, {**write, "expected_revision": "bad"})["code"] == "validation"
        assert len(server.calls) == before
        result = execute(server, write, {"require_revision": True})
        assert result["ok"] and result["data"]["id"] and result["http"]["request_id"] == "fixture-request-123"
        assert [item["method"] for item in server.calls[before:]] == ["GET", "POST"]
        server.issue["title"] = "Changed concurrently"
        before = len(server.writes)
        result = execute(server, write, {"require_revision": True})
        assert result["code"] == "conflict" and not result["uncertain"]
        assert result["current"]["data"]["title"] == "Changed concurrently"
        assert len(server.writes) == before
        unguarded = {key: value for key, value in write.items() if key != "expected_revision"}
        assert execute(server, unguarded, {"require_revision": True})["code"] == "validation"
        before = len(server.calls)
        assert execute(server, write, {"dry_run": True, "require_revision": True})["dry_run"]
        assert len(server.calls) == before
        for mode, uncertain in (("400", False), ("429", False), ("503", True), ("drop", True),
                                ("invalid", True), ("oversize", True), ("redirect", False)):
            server.mode = mode
            before = len(server.calls)
            result = execute(server, unguarded)
            assert not result["ok"] and result["uncertain"] is uncertain, (mode, result)
            assert result["request_id"] == "approval-1"
            assert len(server.calls) == before + 1, "A mutation was retried or a redirect was followed"
        server.fail_reads = True
        assert execute(server, request)["uncertain"] is False
        server.mode = "ok"
        server.issue["body"] = "Credential echo: " + TOKEN
        assert "[REDACTED]" in execute(server, {"action": "view_issue", "issue_number": 7})["data"]["body"]
        for bad in ({"action": []}, {"request_id": "bad\nidentifier"}, {"body": "x" * 262145}):
            before = len(server.calls)
            assert not execute(server, bad)["ok"]
            assert len(server.calls) == before
        for value in ("http://github.com", "https://name:token@github.com", "https://[bad", "file:///forbidden"):
            try:
                validate_url(value)
            except GitHubIssuesBlockError:
                pass
            else:
                raise AssertionError(value)
        validate_url("https://api.github.com")
        validate_url(server.base_url)
    print("[ok] GitHub wallet, scopes, correlation, pagination, conflict preflight and no-retry uncertain writes")


if __name__ == "__main__":
    main()

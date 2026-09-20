#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies GitHub Issues block behavior.
# File Name: F5.24_github_issues_block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-23
# -----------------------------------------------------------------------------

"""F5.24 - GitHub Issues block.

The test runs against a local GitHub-compatible Issues API endpoint and verifies
read actions, write dry-runs, token handling, UI rendering, and both centralized
and zeromq_active runtime modes.
"""

# Test cases:
# - FB1/FB2/FB4/FB6 - Run text payload -> GitHub Issues -> display in centralized and active runtime, verify issue listing and output propagation.
# - FB3/FB4 - Verify write dry-runs, real authenticated comments, missing token guard, invalid repo guard, and token masking.
# - FB7 - Verify standard multi-actions payload execution order, dry-run behavior, failure reporting, and skipped actions.
# - FB8 - Verify optional comment-content enrichment on list_issues and view_issue.
# - FB5 - Render modal, inspector, and node-card with GitHub settings and without leaking token values.

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any
from urllib import parse as urlparse
import json
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blocs.github_issues.block import GitHubIssuesBlock
from bloxsmith_app.block_runtime import BlockRuntimeContext
from bloxsmith_app.block_ui import render_block_inspector_panel, render_block_modal, render_block_node_card
from ui_smoke_common import (
    create_run_api,
    data_edge,
    display_node,
    expect,
    graph_payload,
    isolated_server,
    text_node,
    wait_for_run_terminal,
)
from urllib.parse import quote
from block_test_packages import install_test_package, release_key, surface_payload


SECRET = "ghp_test_github_issues_secret"
OWNER = "acme"
REPO = "project"
FULL_REPO = f"{OWNER}/{REPO}"


class FakeGitHubIssuesHttpServer(ThreadingHTTPServer):
    """Small GitHub Issues-compatible HTTP server used by block tests."""

    requests_log: list[dict[str, Any]]

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeGitHubIssuesHandler)
        self.requests_log = []

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class FakeGitHubIssuesHandler(BaseHTTPRequestHandler):
    server: FakeGitHubIssuesHttpServer

    def do_GET(self) -> None:
        parsed = urlparse.urlparse(self.path)
        self._record(parsed=parsed)
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues":
            self._send_json(
                200,
                [
                    {"number": 7, "title": "Real issue", "state": "open"},
                    {"number": 8, "title": "Pull request", "pull_request": {"url": "https://api.github.test/pr/8"}},
                ],
                headers={"X-RateLimit-Remaining": "99", "X-RateLimit-Reset": "123"},
            )
            return
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7":
            self._send_json(200, {"number": 7, "title": "Real issue", "state": "open", "comments": 2})
            return
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7/comments":
            self._send_json(
                200,
                [
                    {"id": 201, "body": "Premier commentaire", "user": {"login": "reviewer"}},
                    {"id": 202, "body": "Deuxieme commentaire", "user": {"login": "maintainer"}},
                ],
            )
            return
        self._send_json(404, {"message": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse.urlparse(self.path)
        payload = self._read_payload()
        self._record(parsed=parsed, payload=payload)
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues":
            self._send_json(201, {"number": 9, "title": payload.get("title"), "body": payload.get("body")})
            return
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7/comments":
            self._send_json(201, {"id": 101, "body": payload.get("body")})
            return
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7/labels":
            self._send_json(200, [{"name": label} for label in payload.get("labels", [])])
            return
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7/assignees":
            self._send_json(201, {"assignees": payload.get("assignees", [])})
            return
        self._send_json(404, {"message": "not found"})

    def do_PATCH(self) -> None:
        parsed = urlparse.urlparse(self.path)
        payload = self._read_payload()
        self._record(parsed=parsed, payload=payload)
        if parsed.path == f"/repos/{OWNER}/{REPO}/issues/7":
            self._send_json(200, {"number": 7, "state": payload.get("state"), "state_reason": payload.get("state_reason")})
            return
        self._send_json(404, {"message": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse.urlparse(self.path)
        self._record(parsed=parsed)
        if parsed.path in {f"/repos/{OWNER}/{REPO}/issues/7/labels/bug", f"/repos/{OWNER}/{REPO}/issues/7/labels/todo"}:
            self._send_json(200, [{"name": "triage"}])
            return
        self._send_json(404, {"message": "not found"})

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _record(self, *, parsed: urlparse.ParseResult, payload: dict[str, Any] | None = None) -> None:
        self.server.requests_log.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": urlparse.parse_qs(parsed.query),
                "authorization": self.headers.get("Authorization") or "",
                "api_version": self.headers.get("X-GitHub-Api-Version") or "",
                "payload": payload or {},
            }
        )

    def _send_json(self, status: int, payload: Any, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class FakeGitHubServer:
    """Context manager for the local fake GitHub Issues API server."""

    def __enter__(self) -> FakeGitHubIssuesHttpServer:
        self.server = FakeGitHubIssuesHttpServer()
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def github_issues_node(api_base_url: str, *, action: str = "list_issues") -> dict[str, Any]:
    """Build a graph node payload for the GitHub Issues block."""

    return {
        "id": "github-issues-1",
        "kind": "github_issues",
        "title": "GitHub Issues test",
        "position": {"x": 360, "y": 120},
        "inputs": [
            {
                "id": 1,
                "name": "actions",
                "title": "Actions",
                "accepts": ["application/json", "text/plain", "message/*"],
                "multiplicity": "many",
                "required": False,
            }
        ],
        "outputs": [
            {"id": 1, "name": "result", "title": "Result", "emits": ["application/json", "message/*"], "multiplicity": "many"},
            {"id": 2, "name": "summary", "title": "Summary", "emits": ["text/plain", "message/*"], "multiplicity": "many"},
        ],
        "config": {
            "api_base_url": api_base_url,
            "token": SECRET,
            "repo": FULL_REPO,
            "action": action,
            "state": "open",
            "dry_run": True,
            "per_page": 30,
            "timeout_sec": 10,
            "include_comments": False,
        },
    }


def runtime_graph(api_base_url: str) -> dict[str, Any]:
    """Build text payload -> GitHub Issues -> display graph used by runtime tests."""

    payload = json.dumps({"state": "all"})
    return graph_payload(
        "F5 GitHub Issues",
        [
            text_node("text-1", "Actions", payload, 80, 120),
            github_issues_node(api_base_url),
            display_node("display-1", "Affichage", 720, 120),
        ],
        [
            data_edge("edge-text-github", "text-1", 1, "github-issues-1", 1),
            data_edge("edge-github-display", "github-issues-1", 1, "display-1", 1),
        ],
    )


def multi_actions_payload() -> str:
    """Return the standard multi-actions input payload used by runtime tests."""

    return json.dumps(
        {
            "issue_number": 7,
            "actions": [
                {"action": "add_labels", "labels": ["status:ready-for-dev", "priority:p2"]},
                {"action": "remove_labels", "labels": ["todo"]},
                {"action": "add_comment", "body": "Commentaire de triage."},
                {"action": "close_issue", "state_reason": "not_planned"},
            ],
        },
        ensure_ascii=False,
    )


def multi_actions_runtime_graph(api_base_url: str) -> dict[str, Any]:
    """Build actions payload -> GitHub Issues -> display graph for runtime multi-action tests."""

    node = github_issues_node(api_base_url)
    node["config"] = {**node["config"], "dry_run": True}
    return graph_payload(
        "F5 GitHub Issues multi actions",
        [
            text_node("text-1", "Actions", multi_actions_payload(), 80, 120),
            node,
            display_node("display-1", "Affichage", 720, 120),
        ],
        [
            data_edge("edge-text-github", "text-1", 1, "github-issues-1", 1),
            data_edge("edge-github-display", "github-issues-1", 1, "display-1", 1),
        ],
    )


def run_github_issues_case(runtime_mode: str, fake_server: FakeGitHubIssuesHttpServer) -> dict[str, Any]:
    """Run the block through the public run API in one runtime mode."""

    with isolated_server() as server:
        # Les surfaces sont des assets de release : le bundled kind n'en sert aucun.
        model = install_test_package(server, "github_issues")
        key = quote(release_key(model), safe="")
        served = lambda payload, suffix: next(
            asset["path"] for asset in payload["assets"] if asset["path"].endswith(suffix))
        created = create_run_api(server, runtime_graph(fake_server.base_url), runtime_mode=runtime_mode)
        run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

    logs = "\n".join(run.get("logs", []))
    node_logs = "\n".join(run.get("node_logs", {}).get("github-issues-1", []))
    raw_result = run.get("output_values", {}).get("github-issues-1:1", {}).get("value") or "{}"
    result = json.loads(raw_result)
    expect(run.get("status") == "success", f"Le run GitHub Issues {runtime_mode} doit reussir.")
    expect(result.get("action") == "list_issues", "Le bloc doit executer list_issues.")
    expect(result.get("count") == 1, "list_issues doit filtrer les pull requests par defaut.")
    expect("pull_request" not in raw_result, "La sortie filtree ne doit pas contenir de pull_request.")
    expect(SECRET not in logs and SECRET not in node_logs, "Le token GitHub ne doit pas apparaitre dans les logs.")
    expect("fallback centralized" not in logs, "Le run ne doit pas fallback centralise.")
    if runtime_mode == "zeromq_active":
        expect(
            run.get("results", {}).get("github-issues-1", {}).get("transport") == "zeromq_active",
            "github_issues doit etre execute via zeromq_active.",
        )
    return run


def run_github_issues_multi_actions_case(runtime_mode: str, fake_server: FakeGitHubIssuesHttpServer) -> dict[str, Any]:
    """Run a standard multi-actions payload through the public run API in one runtime mode."""

    before_requests = len(fake_server.requests_log)
    with isolated_server() as server:
        created = create_run_api(server, multi_actions_runtime_graph(fake_server.base_url), runtime_mode=runtime_mode)
        run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

    logs = "\n".join(run.get("logs", []))
    raw_result = run.get("output_values", {}).get("github-issues-1:1", {}).get("value") or "{}"
    result = json.loads(raw_result)
    expect(run.get("status") == "success", f"Le run GitHub Issues multi-actions {runtime_mode} doit reussir.")
    expect(result.get("action") == "multi_actions", "Le payload standard doit declencher multi_actions.")
    expect(result.get("succeeded_count") == 4, "Les quatre actions dry-run doivent reussir.")
    expect([item.get("status") for item in result.get("actions", [])] == ["success", "success", "success", "success"], "Chaque action doit avoir un statut success.")
    expect(result.get("actions", [])[1].get("unit_action") == "remove_label", "remove_labels doit reutiliser l'action unitaire remove_label.")
    expect(len(fake_server.requests_log) == before_requests, "Le multi-actions dry-run ne doit pas contacter GitHub.")
    expect("fallback centralized" not in logs, "Le run multi-actions ne doit pas fallback centralise.")
    if runtime_mode == "zeromq_active":
        expect(
            run.get("results", {}).get("github-issues-1", {}).get("transport") == "zeromq_active",
            "github_issues multi-actions doit etre execute via zeromq_active.",
        )
    return run


def unit_context(
    *,
    config: dict[str, Any],
    inputs: dict[str, str] | None = None,
    input_message: str = "",
) -> BlockRuntimeContext:
    """Build a direct runtime context for unit-level block tests."""

    return BlockRuntimeContext(
        run_id="unit-run",
        node_id="github-issues-unit",
        kind="github_issues",
        title="GitHub Issues unit",
        config=config,
        inputs=inputs or {},
        input_content_types={"actions": "application/json"},
        input_message=input_message,
        input_ports=(SimpleNamespace(id=1, name="actions"),),
        output_ports=(
            SimpleNamespace(id=1, name="result", emits=("application/json", "message/*")),
            SimpleNamespace(id=2, name="summary", emits=("text/plain", "message/*")),
        ),
        root_dir=ROOT,
        run_dir=ROOT,
    )


def test_http_requests(fake_server: FakeGitHubIssuesHttpServer) -> None:
    """TC1 - Verify GitHub Issues read requests and headers."""

    list_requests = [request for request in fake_server.requests_log if request["method"] == "GET" and request["path"].endswith("/issues")]
    expect(len(list_requests) >= 2, "Le faux endpoint Issues doit recevoir une requete de liste par run.")
    for request in list_requests:
        expect(request["authorization"] == f"Bearer {SECRET}", "Le token doit etre envoye en bearer token.")
        expect(request["api_version"] == "2022-11-28", "La version REST GitHub doit etre explicite.")
        expect(request["query"].get("state") == ["all"], "L input actions doit pouvoir surcharger state pour une action simple.")
        expect(request["query"].get("per_page") == ["30"], "Le bloc doit envoyer per_page.")


def test_comment_enrichment(fake_server: FakeGitHubIssuesHttpServer) -> None:
    """TC2 - Verify optional comment-content enrichment for read actions."""

    block = GitHubIssuesBlock()
    list_result = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "token": SECRET,
                "repo": FULL_REPO,
                "action": "list_issues",
                "include_comments": True,
                "include_pull_requests": False,
            }
        )
    )
    expect(list_result.status == "success", "list_issues avec include_comments doit reussir.")
    parsed_list = json.loads(list_result.outputs[0].value)
    expect(parsed_list.get("comments_included") is True, "Le resultat doit signaler que les commentaires sont inclus.")
    expect(parsed_list.get("comments_count") == 2, "Le resultat doit compter les commentaires attaches.")
    expect(parsed_list.get("data", [])[0].get("comments_data", [])[0].get("body") == "Premier commentaire", "Chaque issue doit contenir comments_data.")
    comment_requests = [request for request in fake_server.requests_log if request["method"] == "GET" and request["path"].endswith("/issues/7/comments")]
    expect(comment_requests, "Le bloc doit appeler le endpoint GitHub des commentaires quand include_comments=true.")
    expect(comment_requests[-1]["query"].get("per_page") == ["30"], "La recuperation des commentaires doit respecter per_page.")

    view_result = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "token": SECRET,
                "repo": FULL_REPO,
                "action": "view_issue",
                "issue_number": 7,
                "include_comments": True,
            }
        )
    )
    expect(view_result.status == "success", "view_issue avec include_comments doit reussir.")
    parsed_view = json.loads(view_result.outputs[0].value)
    expect(parsed_view.get("data", {}).get("comments_data", [])[1].get("body") == "Deuxieme commentaire", "view_issue doit aussi enrichir l issue avec comments_data.")


def test_write_actions_and_guards(fake_server: FakeGitHubIssuesHttpServer) -> None:
    """TC2 - Verify dry-runs, authenticated writes, local guards, and token masking."""

    block = GitHubIssuesBlock()
    before = len(fake_server.requests_log)
    dry_create = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "repo": FULL_REPO,
                "action": "create_issue",
                "title": "Dry issue",
                "body": "Dry body",
                "dry_run": True,
            }
        )
    )
    expect(dry_create.status == "success", "Une creation dry-run doit reussir sans token.")
    expect(len(fake_server.requests_log) == before, "Le dry-run ne doit pas contacter GitHub.")
    expect("Dry run" in (dry_create.outputs[0].value or ""), "Le resultat dry-run doit expliquer qu'aucune requete n'est envoyee.")

    view_issue = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "token": SECRET,
                "repo": FULL_REPO,
                "action": "view_issue",
                "issue_number": 7,
            }
        )
    )
    expect(view_issue.status == "success", "La lecture d'une issue precise doit reussir.")
    expect("Real issue" in view_issue.outputs[0].value, "La sortie view_issue doit contenir l'issue lue.")

    ignored_payload_input = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "token": SECRET,
                "repo": FULL_REPO,
                "action": "view_issue",
                "issue_number": 7,
            },
            inputs={"payload": json.dumps({"action": "list_issues", "state": "all"})},
        )
    )
    ignored_payload_result = json.loads(ignored_payload_input.outputs[0].value)
    expect(ignored_payload_result.get("action") == "view_issue", "Un input nomme payload ne doit plus etre consomme.")

    real_comment = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "token": SECRET,
                "repo": FULL_REPO,
                "action": "comment_issue",
                "issue_number": 7,
                "dry_run": False,
            },
            inputs={"actions": "commentaire depuis actions"},
            input_message="commentaire depuis actions",
        )
    )
    expect(real_comment.status == "success", "Un commentaire authentifie doit reussir.")
    last_request = fake_server.requests_log[-1]
    expect(last_request["method"] == "POST" and last_request["path"].endswith("/issues/7/comments"), "Le commentaire doit appeler le bon endpoint.")
    expect(last_request["payload"].get("body") == "commentaire depuis actions", "Le texte actions doit alimenter le commentaire.")
    expect(SECRET not in "\n".join(real_comment.logs), "Le token ne doit pas etre loggue.")

    multi_dry_run = block.execute_runtime(
        unit_context(
            config={"api_base_url": fake_server.base_url, "repo": FULL_REPO, "dry_run": True},
            inputs={"actions": multi_actions_payload()},
        )
    )
    expect(multi_dry_run.status == "success", "Un payload multi-actions dry-run doit reussir sans token.")
    multi_result = json.loads(multi_dry_run.outputs[0].value)
    expect(multi_result.get("action") == "multi_actions", "Le resultat doit identifier la sequence multi_actions.")
    expect(multi_result.get("issue_number") == 7, "issue_number doit etre lu une seule fois a la racine.")
    expect(multi_result.get("succeeded_count") == 4, "Les quatre actions standard doivent etre executees en dry-run.")
    expect([item.get("action") for item in multi_result.get("actions", [])] == ["add_labels", "remove_labels", "add_comment", "close_issue"], "Les actions doivent rester dans l'ordre du payload.")
    expect(multi_result["actions"][1].get("unit_action") == "remove_label", "remove_labels doit reutiliser l'implementation remove_label existante.")
    expect(multi_result["actions"][2].get("unit_action") == "comment_issue", "add_comment doit reutiliser l'implementation comment_issue existante.")
    expect("multi_actions dry-run" in multi_dry_run.outputs[1].value, "Le summary doit decrire la sequence multi-actions.")

    before_multi_failure = len(fake_server.requests_log)
    multi_failure_payload = json.dumps(
        {
            "issue_number": 7,
            "actions": [
                {"action": "add_labels", "labels": ["triage"]},
                {"action": "remove_labels", "labels": ["missing"]},
                {"action": "add_comment", "body": "Ne doit pas etre envoye."},
            ],
        },
        ensure_ascii=False,
    )
    multi_failure = block.execute_runtime(
        unit_context(
            config={"api_base_url": fake_server.base_url, "token": SECRET, "repo": FULL_REPO, "dry_run": False},
            inputs={"actions": multi_failure_payload},
        )
    )
    expect(multi_failure.status == "failed", "Une erreur dans la sequence doit marquer le resultat runtime en echec.")
    failed_result = json.loads(multi_failure.outputs[0].value)
    expect(failed_result.get("ok") is False, "Le resultat structure doit indiquer ok=false.")
    expect([item.get("status") for item in failed_result.get("actions", [])] == ["success", "failed", "skipped"], "Les actions deja executees, echouees et ignorees doivent etre conservees.")
    expect("GitHub HTTP 404" in failed_result.get("actions", [])[1].get("error", ""), "L'action echouee doit porter son erreur.")
    new_requests = fake_server.requests_log[before_multi_failure:]
    expect([request["method"] for request in new_requests] == ["POST", "DELETE"], "La sequence doit s'arreter apres l'erreur et ne pas commenter.")

    missing_token = block.execute_runtime(
        unit_context(
            config={
                "api_base_url": fake_server.base_url,
                "repo": FULL_REPO,
                "action": "add_labels",
                "issue_number": 7,
                "labels": "bug",
                "dry_run": False,
            }
        )
    )
    expect(missing_token.status == "failed", "Une ecriture non dry-run doit refuser un token manquant.")
    expect("token GitHub requis" in missing_token.error, "L'erreur doit expliquer le token manquant.")

    invalid_repo = block.execute_runtime(
        unit_context(
            config={"api_base_url": fake_server.base_url, "repo": "invalid", "action": "list_issues"}
        )
    )
    expect(invalid_repo.status == "failed", "Un repo invalide doit echouer proprement.")
    expect("repo GitHub invalide" in invalid_repo.error, "L'erreur doit expliquer le format owner/repo.")

    dry_run_actions = [
        ("add_labels", {"issue_number": 7, "labels": "bug"}),
        ("remove_label", {"issue_number": 7, "labels": "bug"}),
        ("assign_issue", {"issue_number": 7, "assignees": "octocat"}),
        ("close_issue", {"issue_number": 7}),
        ("reopen_issue", {"issue_number": 7}),
    ]
    before_plans = len(fake_server.requests_log)
    for action, extra_config in dry_run_actions:
        planned = block.execute_runtime(
            unit_context(
                config={
                    "api_base_url": fake_server.base_url,
                    "repo": FULL_REPO,
                    "action": action,
                    "dry_run": True,
                    **extra_config,
                }
            )
        )
        expect(planned.status == "success", f"{action} dry-run doit reussir.")
        expect(action in planned.outputs[0].value, f"Le plan {action} doit etre visible dans le JSON resultat.")
    expect(len(fake_server.requests_log) == before_plans, "Les plans dry-run ne doivent pas appeler GitHub.")


def test_github_issues_ui_contract(fake_server: FakeGitHubIssuesHttpServer) -> None:
    """TC3 - Render GitHub Issues block-owned modal, inspector, and node-card."""

    node = github_issues_node(fake_server.base_url)
    rendered = render_block_modal("github_issues", {"node": node, "runtime": {}})
    html = str(rendered.get("html") or "")
    assets = rendered.get("assets") or []
    css = (ROOT / "blocs/github_issues/assets/css/block_modal.css").read_text(encoding="utf-8")
    js = (ROOT / "blocs/github_issues/assets/js/block_modal.js").read_text(encoding="utf-8")
    expect('data-node-kind="github_issues"' in html, "Le modal GitHub Issues doit venir du bloc.")
    expect("cw-github-issues-modal" in html, "Le modal GitHub Issues doit utiliser le layout large du bloc.")
    expect('data-block-runtime-refresh="autonomous"' in html, "Le modal GitHub Issues doit gerer son refresh runtime.")
    expect('data-github-issues-tab-id="action"' in html, "Le modal doit exposer l'onglet Action.")
    expect('data-github-issues-tab-id="payload"' in html, "Le modal doit exposer l'onglet Actions input.")
    expect('data-github-issues-tab-id="status"' in html, "Le modal doit exposer l'onglet Ports & etat.")
    expect('data-block-config-field="repo"' in html, "Le repo doit etre editable dans le modal.")
    expect('data-block-config-field="token"' in html, "Le token doit etre editable dans le modal.")
    expect('data-block-config-field="dry_run"' in html, "Le dry-run doit etre editable dans le modal.")
    expect('data-block-config-field="include_comments"' in html, "include_comments doit etre editable dans le modal.")
    expect(SECRET not in html, "Le token ne doit pas etre rendu en clair dans le modal.")
    expect("width: min(1180px" in css, "Le CSS doit agrandir le modal GitHub Issues.")
    expect("export function mount" in js, "Le JS doit monter le modal GitHub Issues via le registre block UI.")

    inspector = render_block_inspector_panel("github_issues", {"node": node})
    inspector_html = str(inspector.get("html") or "")
    expect("cw-github-issues-inspector" in inspector_html, "L'inspector GitHub Issues doit venir du bloc.")
    expect("GitHub Issues" in inspector_html and "Integration" in inspector_html, "La metadata inspector doit etre renseignee.")
    expect('class="field-grid"' not in inspector_html, "L'inspector GitHub Issues doit afficher un attribut par ligne.")
    expect('data-block-config-field="action"' in inspector_html, "L'action doit etre editable dans l'inspector.")
    expect('data-block-config-field="token"' in inspector_html, "Le token doit etre editable dans l'inspector.")
    expect('data-block-config-field="include_comments"' in inspector_html, "include_comments doit etre editable dans l'inspector.")
    expect(SECRET not in inspector_html, "Le token ne doit pas etre rendu en clair dans l'inspector.")

    card = render_block_node_card("github_issues", {"node": node})
    card_html = str(card.get("html") or "")
    expect("data-github-issues-node-card" in card_html, "La node-card GitHub Issues doit venir du bloc.")
    expect("list_issues" in card_html and FULL_REPO in card_html, "La node-card doit resumer action et repo.")


def main() -> None:
    with FakeGitHubServer() as fake_server:
        test_github_issues_ui_contract(fake_server)
        run_github_issues_case("centralized", fake_server)
        run_github_issues_case("zeromq_active", fake_server)
        run_github_issues_multi_actions_case("centralized", fake_server)
        run_github_issues_multi_actions_case("zeromq_active", fake_server)
        test_http_requests(fake_server)
        test_comment_enrichment(fake_server)
        test_write_actions_and_guards(fake_server)
    print("[ok] F5.24_github_issues_block")


if __name__ == "__main__":
    main()

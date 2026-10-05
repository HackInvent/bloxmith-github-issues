"""FB6/FB9/FB10: actual wallet and recoverable outcomes in both runtimes/all origins."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).parent)]
from blocs.github_issues.block import GitHubIssuesBlock
from block_test_packages import install_test_package
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload,
                            http_json, isolated_server, text_node, wait_for_run_terminal)
from ticket_fixture import github, TOKEN


def main():
    with github() as api:
        for mode in ("centralized", "zeromq_active"):
            for origin in (None, "managed", "linked"):
                with isolated_server() as server:
                    block = GitHubIssuesBlock()
                    node = block.build_node_payload(node_id="tickets", position={"x": 360, "y": 140})
                    node["config"] = {**block.default_config(), "repo": "fixture/project", "api_base_url": api.base_url,
                        "token_ref": "secret://workspace/ticket_fixture", "error_mode": "result",
                        "allowed_actions": "list_issues,view_issue", "dry_run": True}
                    if origin:
                        node["block_version"] = install_test_package(server, "github_issues", origin=origin)["version"]
                    http_json(server.base_url, "/api/application/secrets/init", method="POST",
                              payload={"password": "disposable-ticket-fixture"})
                    http_json(server.base_url, "/api/application/secrets", method="POST",
                              payload={"name": "ticket_fixture", "value": TOKEN})
                    graph = graph_payload("Ticket connector", [
                        text_node("source", "Read request", '{"request_id":"read-runtime","action":"view_issue","issue_number":7}', 60, 140),
                        node, display_node("result", "Ticket", 700, 140)], [
                        data_edge("input", "source", 1, "tickets", 1), data_edge("result", "tickets", 1, "result", 1)])
                    project = create_project_api(server, document=graph)["project"]
                    def run():
                        pending = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                        result = wait_for_run_terminal(server, pending["run_id"], timeout_sec=30)
                        assert result["status"] == "success", result.get("logs")
                        assert TOKEN not in json.dumps(result)
                        return json.loads(result["output_values"]["tickets:1"]["value"])
                    result = run()
                    assert result["ok"] and len(result["revision"]) == 64 and result["request_id"] == "read-runtime"
                    assert api.calls[-1]["authorization"] == "Bearer " + TOKEN
                    previous = len(api.calls)
                    http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                    denied = run()
                    assert denied["code"] == "secret" and denied["request_id"] == "read-runtime"
                    assert not denied["uncertain"] and len(api.calls) == previous
                print("[ok] GitHub wallet/outcome runtime " + mode + " " + str(origin), flush=True)


if __name__ == "__main__":
    main()

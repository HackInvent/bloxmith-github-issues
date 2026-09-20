# -----------------------------------------------------------------------------
# Role: Implements the GitHub Issues block runtime and UI contract.
# File Name: block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-23
# -----------------------------------------------------------------------------

from __future__ import annotations

from html import escape
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
import json
import os
import time

from bloxsmith_app.block_api import (
    APPLICATION_JSON,
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeOutput,
    BlockRuntimeResult,
    render_inspector_template,
    render_node_card_template,
    TEXT_PLAIN,
)


DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SEC = 30
MAX_TIMEOUT_SEC = 300
DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100
GITHUB_API_VERSION = "2022-11-28"

READ_ACTIONS = {"list_issues", "view_issue"}
WRITE_ACTIONS = {
    "create_issue",
    "comment_issue",
    "add_labels",
    "remove_label",
    "assign_issue",
    "close_issue",
    "reopen_issue",
}
MULTI_ACTION_ALIASES = {
    "add_labels": "add_labels",
    "remove_labels": "remove_label",
    "remove_label": "remove_label",
    "add_comment": "comment_issue",
    "comment_issue": "comment_issue",
    "close_issue": "close_issue",
}
SUPPORTED_ACTIONS = tuple(sorted(READ_ACTIONS | WRITE_ACTIONS))
ISSUE_NUMBER_ACTIONS = {
    "view_issue",
    "comment_issue",
    "add_labels",
    "remove_label",
    "assign_issue",
    "close_issue",
    "reopen_issue",
}
STATE_VALUES = ("open", "closed", "all")
STATE_REASON_VALUES = ("completed", "not_planned", "reopened")


# Functional behavior:
# FB1 - Normalize a GitHub repository, action, token, issue fields, and optional JSON/text actions input.
# FB2 - Execute read actions against GitHub Issues REST endpoints and filter pull requests from list_issues by default.
# FB3 - Execute write actions only when dry_run is disabled, using the configured token without leaking it in logs or outputs.
# FB4 - Emit a normalized JSON result and a human-readable summary on separate output ports.
# FB5 - Render the GitHub Issues inspector, modal, and node card from block-owned files.
# FB6 - Run through the generic block runtime path used by both centralized and zeromq_active execution modes.
# FB7 - Execute a standard multi-action payload from the actions input while keeping repo/token/runtime settings in config.
# FB8 - Optionally enrich read issues with comment contents when include_comments is enabled.
class GitHubIssuesBlockError(ValueError):
    """Raised when the GitHub Issues block cannot complete an action."""


class GitHubIssuesBlock(BlockDefinition):
    """Autonomous block implementation for GitHub Issues maintainer actions."""

    kind = "github_issues"

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the GitHub Issues canvas card from the block-owned template."""

        config = self._ui_config(node)
        return render_node_card_template(
            block=self,
            node=node,
            node_classes=["github-issues-node"],
            replacements={
                "title": node.get("title") or self.default_title(),
                "action": config["action"],
                "repo": config["repo"] or "owner/repo",
                "mode": "dry-run" if config["dry_run"] else "write",
            },
        )

    def render_modal(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render a wide GitHub Issues modal with grouped editable settings."""

        payload = payload or {}
        title = str(node.get("title") or self.default_title())
        config = self._ui_config(node)
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        replacements = {
            "node_id": escape(str(node.get("id") or ""), quote=True),
            "node_title": escape(title),
            "node_kind": escape(self.kind, quote=True),
            "node_kind_title": escape(str(self.model.get("title") or self.default_title())),
            "modal_body_html": self._render_modal_body(node=node, title=title, config=config, payload=payload),
        }
        html = template
        for key, value in replacements.items():
            html = html.replace(f"{{{{ {key} }}}}", str(value))
        return {"html": html, "context": {"node_id": str(node.get("id") or ""), "node_kind": self.kind}}

    def _render_modal_body(
        self,
        *,
        node: dict[str, Any],
        title: str,
        config: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Render the tabbed body used by the GitHub Issues modal."""

        node_dom_id = self._modal_dom_id(node)
        tabs = [
            self._render_modal_tab(node_dom_id=node_dom_id, tab_id="action", label="Action", summary="Repo, token, endpoint", selected=True),
            self._render_modal_tab(node_dom_id=node_dom_id, tab_id="payload", label="Actions input", summary="Titre, body, labels", selected=False),
            self._render_modal_tab(node_dom_id=node_dom_id, tab_id="status", label="Ports & etat", summary="Runtime et sorties", selected=False),
        ]
        panels = [
            self._render_action_panel(node_dom_id=node_dom_id, title=title, config=config, selected=True),
            self._render_payload_panel(node_dom_id=node_dom_id, config=config, selected=False),
            self._render_status_panel(node_dom_id=node_dom_id, node=node, payload=payload, selected=False),
        ]
        return (
            '<div class="github-issues-modal-body" data-github-issues-modal-tabs>'
            '<nav class="github-issues-modal-tablist" role="tablist" aria-label="Configuration GitHub Issues">'
            + "".join(tabs)
            + '</nav>'
            + '<div class="github-issues-modal-panels">'
            + "".join(panels)
            + '</div>'
            + '</div>'
        )

    def _render_modal_tab(self, *, node_dom_id: str, tab_id: str, label: str, summary: str, selected: bool) -> str:
        """Render one GitHub Issues modal tab button."""

        tab_dom_id = f"github-issues-{node_dom_id}-tab-{tab_id}"
        panel_dom_id = f"github-issues-{node_dom_id}-panel-{tab_id}"
        return (
            '<button class="github-issues-modal-tab" type="button" role="tab" '
            f'id="{escape(tab_dom_id, quote=True)}" aria-controls="{escape(panel_dom_id, quote=True)}" '
            f'aria-selected="{str(selected).lower()}" tabindex="{0 if selected else -1}" '
            f'data-github-issues-modal-tab data-github-issues-tab-id="{escape(tab_id, quote=True)}">'
            f'<span>{escape(label)}</span><small>{escape(summary)}</small>'
            '</button>'
        )

    def _render_action_panel(self, *, node_dom_id: str, title: str, config: dict[str, Any], selected: bool) -> str:
        """Render identity, connection, and issue action settings."""

        panel_id = f"github-issues-{node_dom_id}-panel-action"
        tab_id = f"github-issues-{node_dom_id}-tab-action"
        return (
            '<section class="github-issues-modal-panel" data-github-issues-modal-panel '
            f'data-github-issues-tab-id="action" id="{escape(panel_id, quote=True)}" role="tabpanel" '
            f'aria-labelledby="{escape(tab_id, quote=True)}"{ "" if selected else " hidden" }>'
            '<div class="github-issues-modal-layout">'
            '<div class="github-issues-modal-stack">'
            '<section class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Identite</span></div>'
            f'{self._render_title_field(title)}'
            '</section>'
            '<section class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Connexion GitHub</span></div>'
            '<div class="github-issues-config-grid">'
            '<div class="field-group github-issues-span-2">'
            '<label>Repository</label>'
            f'<input data-block-config-field="repo" type="text" autocomplete="off" spellcheck="false" placeholder="owner/repo" value="{escape(config["repo"], quote=True)}" />'
            '</div>'
            '<div class="field-group github-issues-span-2">'
            '<label>API base URL</label>'
            f'<input data-block-config-field="api_base_url" type="text" autocomplete="off" spellcheck="false" value="{escape(config["api_base_url"], quote=True)}" />'
            '</div>'
            '<div class="field-group github-issues-span-2">'
            '<label>Token GitHub</label>'
            f'<input data-block-config-field="token" data-block-skip-empty="true" type="password" autocomplete="off" spellcheck="false" placeholder="{escape("Token configure" if config["token"] else "github_pat_...", quote=True)}" />'
            '</div>'
            '</div>'
            '<p class="github-issues-modal-help">Le token peut rester vide si <code>GITHUB_TOKEN</code> est defini cote serveur. Repo, token, dry-run, API base URL et timeout restent dans la configuration du bloc.</p>'
            '</section>'
            '</div>'
            '<aside class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Action</span></div>'
            f'{self._render_action_fields(config)}'
            '</aside>'
            '</div>'
            '</section>'
        )

    def _render_payload_panel(self, *, node_dom_id: str, config: dict[str, Any], selected: bool) -> str:
        """Render write payload fields for issue creation and maintenance actions."""

        panel_id = f"github-issues-{node_dom_id}-panel-payload"
        tab_id = f"github-issues-{node_dom_id}-tab-payload"
        return (
            '<section class="github-issues-modal-panel" data-github-issues-modal-panel '
            f'data-github-issues-tab-id="payload" id="{escape(panel_id, quote=True)}" role="tabpanel" '
            f'aria-labelledby="{escape(tab_id, quote=True)}"{ "" if selected else " hidden" }>'
            '<div class="github-issues-modal-layout">'
            '<section class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Contenu issue / commentaire</span></div>'
            '<div class="field-group">'
            '<label>Titre</label>'
            f'<input data-block-config-field="title" type="text" autocomplete="off" spellcheck="false" placeholder="Titre pour create_issue" value="{escape(config["title"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Body / commentaire</label>'
            '<textarea data-block-config-field="body" rows="12" spellcheck="false" placeholder="Corps issue ou commentaire. Peut aussi venir de l input actions.">'
            f'{escape(config["body"])}'
            '</textarea>'
            '</div>'
            '<p class="github-issues-modal-help">Un input <code>actions</code> JSON peut fournir un payload multi-actions ou des champs d action. Un texte brut alimente <code>body</code> si le body est vide.</p>'
            '</section>'
            '<aside class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Meta actions</span></div>'
            '<div class="field-group">'
            '<label>Labels</label>'
            f'<input data-block-config-field="labels" type="text" autocomplete="off" spellcheck="false" placeholder="bug,triage" value="{escape(config["labels"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Assignees</label>'
            f'<input data-block-config-field="assignees" type="text" autocomplete="off" spellcheck="false" placeholder="octocat,maintainer" value="{escape(config["assignees"], quote=True)}" />'
            '</div>'
            '<div class="field-group">'
            '<label>State reason</label>'
            f'<select data-block-config-field="state_reason">{self._select_options(STATE_REASON_VALUES, config["state_reason"])}</select>'
            '</div>'
            '</aside>'
            '</div>'
            '</section>'
        )

    def _render_status_panel(self, *, node_dom_id: str, node: dict[str, Any], payload: dict[str, Any], selected: bool) -> str:
        """Render port and latest runtime information in the modal."""

        panel_id = f"github-issues-{node_dom_id}-panel-status"
        tab_id = f"github-issues-{node_dom_id}-tab-status"
        return (
            '<section class="github-issues-modal-panel" data-github-issues-modal-panel '
            f'data-github-issues-tab-id="status" id="{escape(panel_id, quote=True)}" role="tabpanel" '
            f'aria-labelledby="{escape(tab_id, quote=True)}"{ "" if selected else " hidden" }>'
            '<div class="github-issues-modal-layout">'
            '<section class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Ports</span></div>'
            f'{self._render_generic_modal_ports(node)}'
            '</section>'
            '<section class="github-issues-modal-section">'
            '<div class="ports-editor-header"><span class="group-label">Dernier etat</span></div>'
            f'{self._render_generic_modal_runtime(payload)}'
            '</section>'
            '</div>'
            '</section>'
        )

    def _render_action_fields(self, config: dict[str, Any]) -> str:
        """Render the action-specific controls used by the GitHub Issues modal."""

        dry_run_checked = "checked" if config["dry_run"] else ""
        include_pr_checked = "checked" if config["include_pull_requests"] else ""
        include_comments_checked = "checked" if config["include_comments"] else ""
        return (
            '<div class="field-group">'
            '<label>Action</label>'
            f'<select data-block-config-field="action">{self._select_options(SUPPORTED_ACTIONS, config["action"])}</select>'
            '</div>'
            '<div class="github-issues-config-grid">'
            '<div class="field-group">'
            '<label>Issue number</label>'
            f'<input data-block-config-field="issue_number" data-block-value-type="integer" type="number" min="0" step="1" value="{config["issue_number"]}" />'
            '</div>'
            '<div class="field-group">'
            '<label>State</label>'
            f'<select data-block-config-field="state">{self._select_options(STATE_VALUES, config["state"])}</select>'
            '</div>'
            '<div class="field-group">'
            '<label>Per page</label>'
            f'<input data-block-config-field="per_page" data-block-value-type="integer" type="number" min="1" max="100" step="1" value="{config["per_page"]}" />'
            '</div>'
            '<div class="field-group">'
            '<label>Timeout secondes</label>'
            f'<input data-block-config-field="timeout_sec" data-block-value-type="integer" type="number" min="1" max="{MAX_TIMEOUT_SEC}" step="1" value="{config["timeout_sec"]}" />'
            '</div>'
            '</div>'
            '<label class="checkbox-line">'
            f'<input data-block-config-field="dry_run" data-block-value-type="boolean" type="checkbox" {dry_run_checked} />'
            '<span>Dry run pour les actions d ecriture</span>'
            '</label>'
            '<label class="checkbox-line">'
            f'<input data-block-config-field="include_pull_requests" data-block-value-type="boolean" type="checkbox" {include_pr_checked} />'
            '<span>Inclure les pull requests dans list_issues</span>'
            '</label>'
            '<label class="checkbox-line">'
            f'<input data-block-config-field="include_comments" data-block-value-type="boolean" type="checkbox" {include_comments_checked} />'
            '<span>Recuperer le contenu des commentaires</span>'
            '</label>'
        )

    def _render_title_field(self, title: str) -> str:
        """Render the editable node title field without duplicating the modal Apply button."""

        return (
            '<div class="field-group">'
            '<label>Nom du bloc</label>'
            f'<input data-block-title-field type="text" autocomplete="off" value="{escape(title, quote=True)}" />'
            '</div>'
        )

    def _modal_dom_id(self, node: dict[str, Any]) -> str:
        """Return a stable DOM-safe id fragment for the GitHub Issues modal."""

        raw = str(node.get("id") or "github-issues")
        return "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-") or "github-issues"

    def render_inspector_panel(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the GitHub Issues inspector with generic field bindings."""

        config = self._ui_config(node)
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = render_inspector_template(
            template=(
                template
                .replace("{{ repo }}", escape(config["repo"], quote=True))
                .replace("{{ api_base_url }}", escape(config["api_base_url"], quote=True))
                .replace("{{ token_placeholder }}", "Token configure" if config["token"] else "github_pat_...")
                .replace("{{ action_options }}", self._select_options(SUPPORTED_ACTIONS, config["action"]))
                .replace("{{ state_options }}", self._select_options(STATE_VALUES, config["state"]))
                .replace("{{ state_reason_options }}", self._select_options(STATE_REASON_VALUES, config["state_reason"]))
                .replace("{{ issue_number }}", str(config["issue_number"]))
                .replace("{{ title }}", escape(config["title"], quote=True))
                .replace("{{ body }}", escape(config["body"]))
                .replace("{{ labels }}", escape(config["labels"], quote=True))
                .replace("{{ assignees }}", escape(config["assignees"], quote=True))
                .replace("{{ dry_run_checked }}", "checked" if config["dry_run"] else "")
                .replace("{{ include_pull_requests_checked }}", "checked" if config["include_pull_requests"] else "")
                .replace("{{ include_comments_checked }}", "checked" if config["include_comments"] else "")
                .replace("{{ per_page }}", str(config["per_page"]))
                .replace("{{ timeout_sec }}", str(config["timeout_sec"]))
            ),
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
        )
        return {
            "html": html,
            "context": {
                "node_id": str(node.get("id") or ""),
                "token_configured": bool(config["token"]),
                "full_panel": True,
            },
        }

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Execute one configured action or an ordered multi-action payload.

        Args:
            context: Generic runtime context injected by centralized or active execution.
        """

        logs: list[str] = []
        started = time.perf_counter()
        config = self.normalize_config(context.config)
        payload = self._input_payload(context)
        try:
            self._validate_base_config(config)
            if self._is_multi_actions_payload(payload):
                result = self._dispatch_multi_actions(config, payload)
                result["duration_sec"] = round(time.perf_counter() - started, 3)
                summary = self._summary(result)
                result_json = json.dumps(result, ensure_ascii=False, indent=2)
                outputs = self._runtime_outputs(context, result_json=result_json, summary=summary)
                logs.append(
                    f"[github-issues] {context.node_id}: multi_actions repo={config['repo']} "
                    f"issue={result.get('issue_number')} dry_run={str(config['dry_run']).lower()}."
                )
                logs.append(f"[done] GitHub Issues {context.node_id}: {summary}")
                return BlockRuntimeResult(
                    status="success" if result.get("ok") else "failed",
                    outputs=outputs,
                    logs=logs,
                    error="" if result.get("ok") else str(result.get("error") or summary),
                    exit_code=0 if result.get("ok") else 1,
                    last_message=summary,
                    content_type=APPLICATION_JSON,
                    worker_received=summary,
                    metadata={"github_issues": self._metadata(result)},
                )

            single_config = self._single_action_config(config, payload)
            action = single_config["action"]
            self._validate_action_config(single_config, action)
            logs.append(
                f"[github-issues] {context.node_id}: action={action} repo={single_config['repo']} "
                f"dry_run={str(single_config['dry_run']).lower()}."
            )
            result = self._dispatch_action(single_config)
            result["duration_sec"] = round(time.perf_counter() - started, 3)
            summary = self._summary(result)
            result_json = json.dumps(result, ensure_ascii=False, indent=2)
            outputs = self._runtime_outputs(context, result_json=result_json, summary=summary)
            logs.append(f"[done] GitHub Issues {context.node_id}: {summary}")
            return BlockRuntimeResult(
                status="success",
                outputs=outputs,
                logs=logs,
                last_message=summary,
                content_type=APPLICATION_JSON,
                worker_received=summary,
                metadata={"github_issues": self._metadata(result)},
            )
        except GitHubIssuesBlockError as exc:
            token = config.get("token", "")
            message = self._mask_secret(str(exc), token)
            logs.append(f"[github-issues-error] {context.node_id}: {message}")
            return BlockRuntimeResult(
                status="failed",
                outputs=[],
                logs=logs,
                error=message,
                exit_code=1,
                last_message=message,
                content_type=TEXT_PLAIN,
                worker_received="-",
            )

    def normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """Return safe runtime configuration from raw node config."""

        raw = config if isinstance(config, dict) else {}
        return {
            "api_base_url": self._normalize_base_url(raw.get("api_base_url")),
            "token": str(raw.get("token") or os.getenv("GITHUB_TOKEN") or "").strip(),
            "repo": self._normalize_repo(raw.get("repo")),
            "action": self._normalize_action(raw.get("action")),
            "issue_number": self._normalize_int(raw.get("issue_number"), default=0, minimum=0, maximum=999999999),
            "state": self._normalize_choice(raw.get("state"), STATE_VALUES, default="open"),
            "title": str(raw.get("title") or "").strip(),
            "body": str(raw.get("body") or "").strip(),
            "labels": self._normalize_csv_text(raw.get("labels")),
            "assignees": self._normalize_csv_text(raw.get("assignees")),
            "state_reason": self._normalize_choice(raw.get("state_reason"), STATE_REASON_VALUES, default="completed"),
            "include_pull_requests": self._bool(raw.get("include_pull_requests", False)),
            "include_comments": self._bool(raw.get("include_comments", False)),
            "dry_run": self._bool(raw.get("dry_run", True)),
            "per_page": self._normalize_int(raw.get("per_page"), default=DEFAULT_PER_PAGE, minimum=1, maximum=MAX_PER_PAGE),
            "timeout_sec": self._normalize_int(raw.get("timeout_sec"), default=DEFAULT_TIMEOUT_SEC, minimum=1, maximum=MAX_TIMEOUT_SEC),
        }

    def _single_action_config(self, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Merge action-level payload fields into config without overriding connection settings.

        Args:
            config: Normalized durable block configuration.
            payload: Optional parsed input payload from the actions port.
        """

        if not payload:
            return config
        allowed_payload_keys = {
            "action",
            "issue_number",
            "state",
            "title",
            "body",
            "labels",
            "assignees",
            "state_reason",
            "include_pull_requests",
            "per_page",
        }
        action_payload = {key: value for key, value in payload.items() if key in allowed_payload_keys}
        merged = {**config, **action_payload}
        if "body" not in action_payload and payload.get("_plain_text"):
            merged["body"] = str(payload["_plain_text"])
        normalized = self.normalize_config(merged)
        if "_plain_text" in payload and not normalized["body"]:
            normalized["body"] = str(payload["_plain_text"])
        return normalized

    def _input_payload(self, context: BlockRuntimeContext) -> dict[str, Any]:
        """Parse the optional actions input as JSON object or plain text.

        The canonical input name is actions. Port id 1 remains accepted because
        it is the stable runtime id of the canonical input.
        """

        text = ""
        for key in ("actions", "1"):
            value = context.input_value(key)
            if value:
                text = str(value)
                break
        if not text:
            text = str(context.input_message or "")
        text = text.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_plain_text": text}
        return parsed if isinstance(parsed, dict) else {"_plain_text": text}

    def _validate_base_config(self, config: dict[str, Any]) -> None:
        """Validate repository settings shared by single and multi-action execution."""

        if not config["repo"]:
            raise GitHubIssuesBlockError("repo GitHub manquant. Format attendu: owner/repo.")
        if not self._is_valid_repo(config["repo"]):
            raise GitHubIssuesBlockError(f"repo GitHub invalide: {config['repo']}. Format attendu: owner/repo.")

    def _validate_action_config(self, config: dict[str, Any], action: str) -> None:
        """Validate one action and its token requirements before dispatch."""

        if action not in SUPPORTED_ACTIONS:
            raise GitHubIssuesBlockError(f"Action GitHub Issues non supportee: {action}.")
        if action in WRITE_ACTIONS and not config["dry_run"] and not config["token"]:
            raise GitHubIssuesBlockError("token GitHub requis pour une action d'ecriture.")

    def _is_multi_actions_payload(self, payload: dict[str, Any]) -> bool:
        """Return whether the parsed input uses the standard multi-actions shape."""

        return isinstance(payload, dict) and isinstance(payload.get("actions"), list)

    def _dispatch_multi_actions(self, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a root issue_number plus ordered action list payload.

        Args:
            config: Normalized durable block configuration containing repo, token, dry_run and HTTP settings.
            payload: Parsed standard multi-action payload received on the actions input.
        """

        issue_number = self._normalize_int(payload.get("issue_number"), default=0, minimum=0, maximum=999999999)
        if issue_number <= 0:
            raise GitHubIssuesBlockError("issue_number requis a la racine du payload multi-actions.")
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions:
            raise GitHubIssuesBlockError("actions doit etre une liste non vide dans le payload multi-actions.")
        if not config["dry_run"] and not config["token"]:
            raise GitHubIssuesBlockError("token GitHub requis pour une sequence d'actions d'ecriture.")

        action_results: list[dict[str, Any]] = []
        failed = False
        error_message = ""
        for index, action_payload in enumerate(actions):
            if failed:
                action_results.append(self._skipped_multi_action(index, action_payload))
                continue
            try:
                action_config, requested_action = self._multi_action_config(config, issue_number, action_payload)
                unit_result = self._dispatch_action(action_config)
                action_results.append(
                    {
                        "index": index,
                        "action": requested_action,
                        "unit_action": action_config["action"],
                        "status": "success",
                        "result": unit_result,
                    }
                )
            except GitHubIssuesBlockError as exc:
                failed = True
                error_message = self._mask_secret(str(exc), config["token"])
                action_results.append(
                    {
                        "index": index,
                        "action": self._raw_action_name(action_payload),
                        "status": "failed",
                        "error": error_message,
                    }
                )

        succeeded_count = sum(1 for item in action_results if item.get("status") == "success")
        failed_count = sum(1 for item in action_results if item.get("status") == "failed")
        skipped_count = sum(1 for item in action_results if item.get("status") == "skipped")
        return {
            "ok": not failed,
            "action": "multi_actions",
            "repo": config["repo"],
            "issue_number": issue_number,
            "dry_run": config["dry_run"],
            "count": len(actions),
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "actions": action_results,
            "error": error_message if failed else "",
        }

    def _multi_action_config(self, config: dict[str, Any], issue_number: int, action_payload: Any) -> tuple[dict[str, Any], str]:
        """Build a single-action config from one multi-action entry."""

        if not isinstance(action_payload, dict):
            raise GitHubIssuesBlockError("Chaque entree actions doit etre un objet JSON.")
        requested_action = self._raw_action_name(action_payload)
        unit_action = self._normalize_multi_action_name(requested_action)
        action_config: dict[str, Any] = {**config, "action": unit_action, "issue_number": issue_number}
        if unit_action in {"add_labels", "remove_label"}:
            action_config["labels"] = self._normalize_csv_text(action_payload.get("labels"))
        elif unit_action == "comment_issue":
            action_config["body"] = str(action_payload.get("body") or action_payload.get("comment") or "").strip()
        elif unit_action == "close_issue":
            action_config["state_reason"] = self._normalize_choice(action_payload.get("state_reason"), STATE_REASON_VALUES, default=config["state_reason"])
        self._validate_action_config(action_config, unit_action)
        return action_config, requested_action

    def _normalize_multi_action_name(self, action: str) -> str:
        """Map standard multi-action names to existing block unit actions."""

        normalized = str(action or "").strip().lower().replace("-", "_")
        unit_action = MULTI_ACTION_ALIASES.get(normalized)
        if not unit_action:
            raise GitHubIssuesBlockError(f"Action multi-actions non supportee: {action}.")
        return unit_action

    def _raw_action_name(self, action_payload: Any) -> str:
        """Return the user-provided action name for result reporting."""

        if isinstance(action_payload, dict):
            return str(action_payload.get("action") or "").strip()
        return ""

    def _skipped_multi_action(self, index: int, action_payload: Any) -> dict[str, Any]:
        """Return a result entry for actions skipped after a prior failure."""

        return {
            "index": index,
            "action": self._raw_action_name(action_payload),
            "status": "skipped",
            "reason": "Action ignoree apres une erreur precedente.",
        }

    def _dispatch_action(self, config: dict[str, Any]) -> dict[str, Any]:
        """Route the normalized action to one GitHub REST operation."""

        action = config["action"]
        if action == "list_issues":
            return self._list_issues(config)
        if action == "view_issue":
            return self._view_issue(config)
        if action == "create_issue":
            return self._create_issue(config)
        if action == "comment_issue":
            return self._comment_issue(config)
        if action == "add_labels":
            return self._add_labels(config)
        if action == "remove_label":
            return self._remove_label(config)
        if action == "assign_issue":
            return self._assign_issue(config)
        if action == "close_issue":
            return self._set_issue_state(config, state="closed")
        if action == "reopen_issue":
            return self._set_issue_state(config, state="open")
        raise GitHubIssuesBlockError(f"Action GitHub Issues non supportee: {action}.")

    def _list_issues(self, config: dict[str, Any]) -> dict[str, Any]:
        """List repository issues and optionally exclude pull requests."""

        response = self._api_request(
            config,
            method="GET",
            path=f"/repos/{self._repo_path(config['repo'])}/issues",
            query={"state": config["state"], "per_page": str(config["per_page"])},
        )
        items = response["data"] if isinstance(response["data"], list) else []
        if not config["include_pull_requests"]:
            items = [item for item in items if not (isinstance(item, dict) and item.get("pull_request"))]
        comments_count = 0
        if config["include_comments"]:
            items, comments_count = self._attach_comments_to_issues(config, items)
        return {
            "ok": True,
            "action": "list_issues",
            "repo": config["repo"],
            "dry_run": False,
            "count": len(items),
            "comments_included": bool(config["include_comments"]),
            "comments_count": comments_count,
            "data": items,
            "http": response["http"],
        }

    def _view_issue(self, config: dict[str, Any]) -> dict[str, Any]:
        """Fetch one GitHub issue by number."""

        issue_number = self._required_issue_number(config)
        response = self._api_request(
            config,
            method="GET",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}",
        )
        issue = response["data"]
        comments_count = 0
        if config["include_comments"] and isinstance(issue, dict):
            issue, comments_count = self._attach_comments_to_issue(config, issue)
        return {
            "ok": True,
            "action": "view_issue",
            "repo": config["repo"],
            "issue_number": issue_number,
            "dry_run": False,
            "comments_included": bool(config["include_comments"]),
            "comments_count": comments_count,
            "data": issue,
            "http": response["http"],
        }

    def _attach_comments_to_issues(self, config: dict[str, Any], issues: list[Any]) -> tuple[list[Any], int]:
        """Return list issues enriched with GitHub comment contents.

        Args:
            config: Normalized GitHub Issues block configuration.
            issues: Issue payloads returned by GitHub list_issues after local filtering.
        """

        enriched: list[Any] = []
        total_comments = 0
        for issue in issues:
            if not isinstance(issue, dict):
                enriched.append(issue)
                continue
            issue_with_comments, comments_count = self._attach_comments_to_issue(config, issue)
            enriched.append(issue_with_comments)
            total_comments += comments_count
        return enriched, total_comments

    def _attach_comments_to_issue(self, config: dict[str, Any], issue: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Attach comment contents to one issue under `comments_data`.

        Args:
            config: Normalized GitHub Issues block configuration.
            issue: GitHub issue object containing a numeric `number`.
        """

        issue_number = self._normalize_int(issue.get("number"), default=0, minimum=0, maximum=999999999)
        enriched = dict(issue)
        if issue_number <= 0:
            enriched["comments_data"] = []
            return enriched, 0
        comments = self._get_issue_comments(config, issue_number)
        enriched["comments_data"] = comments
        return enriched, len(comments)

    def _get_issue_comments(self, config: dict[str, Any], issue_number: int) -> list[Any]:
        """Fetch the configured first page of comments for one GitHub issue.

        Args:
            config: Normalized GitHub Issues block configuration.
            issue_number: GitHub issue number used by the comments endpoint.
        """

        response = self._api_request(
            config,
            method="GET",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/comments",
            query={"per_page": str(config["per_page"])},
        )
        return response["data"] if isinstance(response["data"], list) else []

    def _create_issue(self, config: dict[str, Any]) -> dict[str, Any]:
        """Create one issue or return the planned request in dry-run mode."""

        if not config["title"]:
            raise GitHubIssuesBlockError("title requis pour create_issue.")
        payload = {
            "title": config["title"],
            "body": config["body"],
            "labels": self._csv_values(config["labels"]),
            "assignees": self._csv_values(config["assignees"]),
        }
        return self._write_or_dry_run(
            config,
            action="create_issue",
            method="POST",
            path=f"/repos/{self._repo_path(config['repo'])}/issues",
            payload=self._compact_payload(payload),
        )

    def _comment_issue(self, config: dict[str, Any]) -> dict[str, Any]:
        """Comment on one issue or return the planned request in dry-run mode."""

        issue_number = self._required_issue_number(config)
        if not config["body"]:
            raise GitHubIssuesBlockError("body requis pour comment_issue.")
        return self._write_or_dry_run(
            config,
            action="comment_issue",
            method="POST",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/comments",
            payload={"body": config["body"]},
            issue_number=issue_number,
        )

    def _add_labels(self, config: dict[str, Any]) -> dict[str, Any]:
        """Add labels to one issue or return the planned request in dry-run mode."""

        issue_number = self._required_issue_number(config)
        labels = self._csv_values(config["labels"])
        if not labels:
            raise GitHubIssuesBlockError("labels requis pour add_labels.")
        return self._write_or_dry_run(
            config,
            action="add_labels",
            method="POST",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/labels",
            payload={"labels": labels},
            issue_number=issue_number,
        )

    def _remove_label(self, config: dict[str, Any]) -> dict[str, Any]:
        """Remove one or more labels from one issue."""

        issue_number = self._required_issue_number(config)
        labels = self._csv_values(config["labels"])
        if not labels:
            raise GitHubIssuesBlockError("labels requis pour remove_label.")
        if config["dry_run"]:
            return self._dry_run_result(
                config,
                action="remove_label",
                method="DELETE",
                path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/labels/<label>",
                payload={"labels": labels},
                issue_number=issue_number,
            )
        responses = [
            self._api_request(
                config,
                method="DELETE",
                path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/labels/{urlparse.quote(label, safe='')}",
            )
            for label in labels
        ]
        return {
            "ok": True,
            "action": "remove_label",
            "repo": config["repo"],
            "issue_number": issue_number,
            "dry_run": False,
            "data": [response["data"] for response in responses],
            "http": [response["http"] for response in responses],
        }

    def _assign_issue(self, config: dict[str, Any]) -> dict[str, Any]:
        """Assign users to one issue or return the planned request in dry-run mode."""

        issue_number = self._required_issue_number(config)
        assignees = self._csv_values(config["assignees"])
        if not assignees:
            raise GitHubIssuesBlockError("assignees requis pour assign_issue.")
        return self._write_or_dry_run(
            config,
            action="assign_issue",
            method="POST",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}/assignees",
            payload={"assignees": assignees},
            issue_number=issue_number,
        )

    def _set_issue_state(self, config: dict[str, Any], *, state: str) -> dict[str, Any]:
        """Close or reopen one issue through GitHub's issue update endpoint."""

        issue_number = self._required_issue_number(config)
        payload: dict[str, Any] = {"state": state}
        if state == "closed":
            payload["state_reason"] = config["state_reason"]
        return self._write_or_dry_run(
            config,
            action="close_issue" if state == "closed" else "reopen_issue",
            method="PATCH",
            path=f"/repos/{self._repo_path(config['repo'])}/issues/{issue_number}",
            payload=payload,
            issue_number=issue_number,
        )

    def _write_or_dry_run(
        self,
        config: dict[str, Any],
        *,
        action: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        issue_number: int = 0,
    ) -> dict[str, Any]:
        """Execute a write request or return a safe dry-run plan."""

        if config["dry_run"]:
            return self._dry_run_result(
                config,
                action=action,
                method=method,
                path=path,
                payload=payload,
                issue_number=issue_number,
            )
        response = self._api_request(config, method=method, path=path, payload=payload)
        return {
            "ok": True,
            "action": action,
            "repo": config["repo"],
            "issue_number": issue_number or None,
            "dry_run": False,
            "data": response["data"],
            "http": response["http"],
        }

    def _dry_run_result(
        self,
        config: dict[str, Any],
        *,
        action: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        issue_number: int = 0,
    ) -> dict[str, Any]:
        """Return a normalized dry-run result without contacting GitHub."""

        return {
            "ok": True,
            "action": action,
            "repo": config["repo"],
            "issue_number": issue_number or None,
            "dry_run": True,
            "data": {
                "method": method,
                "path": path,
                "payload": payload,
                "message": "Dry run: aucune requete d'ecriture envoyee a GitHub.",
            },
        }

    def _api_request(
        self,
        config: dict[str, Any],
        *,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one GitHub REST request and parse the JSON response."""

        url = self._api_url(config["api_base_url"], path, query=query)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "bloxsmith-github-issues-block",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if config["token"]:
            headers["Authorization"] = f"Bearer {config['token']}"
        request = urlrequest.Request(url, data=body, method=method, headers=headers)
        try:
            with urlrequest.urlopen(request, timeout=int(config["timeout_sec"])) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                status = int(response.status)
                response_headers = dict(response.headers.items())
        except urlerror.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            safe_body = self._mask_secret(response_body, config["token"])
            raise GitHubIssuesBlockError(f"GitHub HTTP {exc.code}: {safe_body[:800]}") from exc
        except urlerror.URLError as exc:
            raise GitHubIssuesBlockError(f"GitHub API inaccessible: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitHubIssuesBlockError(f"Timeout GitHub apres {config['timeout_sec']}s.") from exc

        data: Any = {}
        if response_body.strip():
            try:
                data = json.loads(response_body)
            except json.JSONDecodeError as exc:
                raise GitHubIssuesBlockError(f"Reponse GitHub non JSON: {response_body[:600]}") from exc
        return {
            "data": data,
            "http": {
                "status": status,
                "rate_limit_remaining": response_headers.get("X-RateLimit-Remaining", ""),
                "rate_limit_reset": response_headers.get("X-RateLimit-Reset", ""),
            },
        }

    def _runtime_outputs(self, context: BlockRuntimeContext, *, result_json: str, summary: str) -> list[BlockRuntimeOutput]:
        """Map JSON and summary values to declared output ports."""

        outputs: list[BlockRuntimeOutput] = []
        for output_port in context.output_ports:
            port_id = int(getattr(output_port, "id", 0) or 0)
            port_name = str(getattr(output_port, "name", "") or "")
            is_summary = port_name == "summary" or port_id == 2
            outputs.append(
                BlockRuntimeOutput(
                    port_id=port_id,
                    port_name=port_name,
                    value=summary if is_summary else result_json,
                    content_type=TEXT_PLAIN if is_summary else APPLICATION_JSON,
                )
            )
        return outputs

    def _summary(self, result: dict[str, Any]) -> str:
        """Return a concise human-readable execution summary."""

        action = str(result.get("action") or "")
        repo = str(result.get("repo") or "")
        if action == "multi_actions":
            issue_number = result.get("issue_number")
            total = int(result.get("count") or 0)
            succeeded = int(result.get("succeeded_count") or 0)
            failed = int(result.get("failed_count") or 0)
            skipped = int(result.get("skipped_count") or 0)
            if result.get("ok"):
                mode = "dry-run" if result.get("dry_run") else "OK"
                return f"multi_actions {mode}: {succeeded}/{total} action(s) sur {repo}#{issue_number}."
            return (
                f"multi_actions erreur: {succeeded}/{total} action(s) executee(s), "
                f"{failed} echec, {skipped} ignoree(s) sur {repo}#{issue_number}."
            )
        if result.get("dry_run"):
            return f"{action} dry-run pret pour {repo}."
        if action == "list_issues":
            if result.get("comments_included"):
                return f"{result.get('count', 0)} issue(s) lue(s), {result.get('comments_count', 0)} commentaire(s) depuis {repo}."
            return f"{result.get('count', 0)} issue(s) lue(s) depuis {repo}."
        issue_number = result.get("issue_number")
        if issue_number:
            return f"{action} OK sur {repo}#{issue_number}."
        data = result.get("data")
        if isinstance(data, dict) and data.get("number"):
            return f"{action} OK sur {repo}#{data.get('number')}."
        return f"{action} OK sur {repo}."

    def _metadata(self, result: dict[str, Any]) -> dict[str, Any]:
        """Return safe structured metadata without request secrets."""

        return {
            "action": result.get("action"),
            "repo": result.get("repo"),
            "issue_number": result.get("issue_number"),
            "dry_run": result.get("dry_run"),
            "count": result.get("count"),
            "comments_included": result.get("comments_included"),
            "comments_count": result.get("comments_count"),
            "succeeded_count": result.get("succeeded_count"),
            "failed_count": result.get("failed_count"),
            "skipped_count": result.get("skipped_count"),
            "http": result.get("http"),
            "duration_sec": result.get("duration_sec"),
        }

    def _ui_config(self, node: dict[str, Any]) -> dict[str, Any]:
        """Return normalized UI values for persisted node config."""

        return self.normalize_config(node.get("config") if isinstance(node.get("config"), dict) else {})

    def _select_options(self, values: tuple[str, ...], selected: str) -> str:
        """Render option tags for a select field."""

        return "\n".join(
            f'<option value="{escape(value, quote=True)}"{" selected" if value == selected else ""}>{escape(value)}</option>'
            for value in values
        )

    def _required_issue_number(self, config: dict[str, Any]) -> int:
        """Return the required positive issue number for issue-scoped actions."""

        issue_number = int(config.get("issue_number") or 0)
        if issue_number <= 0:
            raise GitHubIssuesBlockError(f"issue_number requis pour {config.get('action')}.")
        return issue_number

    def _repo_path(self, repo: str) -> str:
        """Return URL-encoded owner/repo path segments."""

        owner, name = repo.split("/", 1)
        return f"{urlparse.quote(owner, safe='')}/{urlparse.quote(name, safe='')}"

    def _is_valid_repo(self, repo: str) -> bool:
        """Return whether a repository reference uses the supported owner/repo shape."""

        parts = str(repo or "").split("/")
        return len(parts) == 2 and all(part.strip() for part in parts)

    def _api_url(self, base_url: str, path: str, *, query: dict[str, str] | None = None) -> str:
        """Build a GitHub API URL from base, path, and query values."""

        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlparse.urlencode(query)}"
        return url

    def _normalize_base_url(self, value: Any) -> str:
        """Normalize the GitHub API base URL."""

        raw = str(value or DEFAULT_GITHUB_API_BASE_URL).strip().rstrip("/")
        return raw or DEFAULT_GITHUB_API_BASE_URL

    def _normalize_repo(self, value: Any) -> str:
        """Normalize an owner/repo repository reference."""

        raw = str(value or "").strip().strip("/")
        if raw.count("/") != 1:
            return raw
        owner, name = (part.strip() for part in raw.split("/", 1))
        return f"{owner}/{name}" if owner and name else raw

    def _normalize_action(self, value: Any) -> str:
        """Normalize and validate a GitHub Issues action name."""

        raw = str(value or "list_issues").strip().lower().replace("-", "_")
        return raw if raw in SUPPORTED_ACTIONS else "list_issues"

    def _normalize_choice(self, value: Any, allowed: tuple[str, ...], *, default: str) -> str:
        """Normalize a string enum with a fallback value."""

        raw = str(value or default).strip().lower()
        return raw if raw in allowed else default

    def _normalize_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        """Normalize an integer setting within inclusive bounds."""

        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        return max(int(minimum), min(int(maximum), parsed))

    def _normalize_csv_text(self, value: Any) -> str:
        """Normalize strings or lists into a comma-separated UI value."""

        if isinstance(value, list):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    def _csv_values(self, value: Any) -> list[str]:
        """Return comma-separated config values as a clean string list."""

        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    def _compact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove empty optional values from a GitHub request payload."""

        return {
            key: value
            for key, value in payload.items()
            if value not in ("", None, [], {})
        }

    def _bool(self, value: Any) -> bool:
        """Normalize user-facing checkbox values."""

        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _mask_secret(self, value: str, secret: str) -> str:
        """Remove token values from errors or logs."""

        text = str(value or "")
        if secret:
            text = text.replace(secret, "***")
        return text

"""FB5/FB9/FB10: real managed/linked forms, draft semantics and narrow-screen geometry."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright, expect
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package
from ui_smoke_common import isolated_server, graph_payload, create_project_api, project_editor_url
from blocs.github_issues.block import GitHubIssuesBlock


def geometry(modal):
    measured = modal.evaluate("""el => {
        const r=el.getBoundingClientRect(), panel=el.querySelector('[role=tabpanel]:not([hidden])');
        const actions=[...el.querySelectorAll('[data-close-block-modal], [data-block-apply]')];
        return {inside:r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1,
          overflow:el.scrollWidth>el.clientWidth+2||panel.scrollWidth>panel.clientWidth+2,
          controls:actions.every(a=>{const b=a.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}),
          unlabeled:[...el.querySelectorAll('input,select,textarea')].filter(c=>!c.labels?.length&&!c.getAttribute('aria-label')&&!c.getAttribute('aria-labelledby')).length};
    }""")
    assert measured["inside"] and measured["controls"] and not measured["overflow"], measured
    assert not measured["unlabeled"], measured


def main():
    for origin in ("managed", "linked"):
        with isolated_server() as server, sync_playwright() as playwright:
            model = install_test_package(server, "github_issues", origin=origin)
            node = GitHubIssuesBlock().build_node_payload(node_id="tickets", position={"x": 160, "y": 160})
            node["block_version"] = model["version"]
            project = create_project_api(server, document=graph_payload("Ticket settings", [node], []))["project"]
            url = project_editor_url(server.base_url, project["graph_id"], workspace_project_id=project["workspace_project_id"])
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.add_init_script("window.localStorage.setItem('bloxsmith.inspectorPinned','true')")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                card = page.locator('.canvas-node[data-node-id="tickets"]')
                card.locator('.block-node-card-title').dblclick()
                modal = page.locator('.cw-github-issues-modal')
                expect(modal).to_be_visible()
                field = modal.locator('[data-block-config-field="token_ref"]')
                field.fill('secret://workspace/fixture_github')
                expect(modal.locator('[data-block-apply]')).to_be_enabled()
                modal.locator('[data-close-block-modal]').last.click()
                card.locator('.block-node-card-title').dblclick()
                expect(field).to_have_value('')
                field.fill('secret://workspace/fixture_github')
                modal.locator('.github-issues-safety summary').click()
                modal.locator('[data-block-config-field="allowed_actions"]').fill('list_issues,view_issue')
                modal.locator('[data-block-config-field="error_mode"]').select_option('result')
                modal.locator('[data-block-config-field="page"]').fill('2')
                with page.expect_response(lambda r: r.url.endswith('/ui-action') and r.request.method == 'POST') as saved:
                    modal.locator('[data-block-apply]').click()
                assert not saved.value.json().get('error'), saved.value.json()
                if modal.is_visible():
                    modal.locator('[data-close-block-modal]').first.click()
                page.reload()
                card.locator('.block-node-card-title').dblclick()
                expect(field).to_have_value('secret://workspace/fixture_github')
                modal.locator('.github-issues-safety summary').click()
                expect(modal.locator('[data-block-config-field="page"]')).to_have_value('2')
                expect(modal.locator('[data-block-config-field="error_mode"]')).to_have_value('result')
                for width, height in ((1440, 900), (800, 700), (390, 740), (320, 568)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(100)
                    page.screenshot(path=artifact_path('github-ticket-' + origin + '-' + str(width) + '.png'))
                    geometry(modal)
                page.set_viewport_size({"width":1440,"height":900})
                modal.locator('[data-close-block-modal]').first.click()
                card.click(position={"x":20,"y":20})
                inspector = page.locator('[data-properties-surface="inspector"][data-node-id="tickets"]:visible')
                expect(inspector).to_be_visible()
                inspector.locator('.github-issues-safety summary').click()
                expect(inspector.locator('[data-block-config-field="page"]')).to_have_value('2')
                assert inspector.evaluate('el => el.scrollWidth <= el.clientWidth+2')
                page.screenshot(path=artifact_path('github-ticket-' + origin + '-inspector.png'))
                assert not errors, errors
            finally:
                browser.close()
        print('[ok] GitHub controlled settings UI ' + origin, flush=True)


if __name__ == '__main__':
    main()

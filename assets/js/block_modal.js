/**
 * Role: Mounts the GitHub Issues block modal frontend.
 * File Name: block_modal.js
 * Author: Alexandre EL
 * Email: alex@hackinvent.com
 * Created Date: 2026-05-24
 */

(function () {
  "use strict";

  const registry = (window.CWBlockUiBlocks = window.CWBlockUiBlocks || {});
  const previous = registry.github_issues || {};

  /**
   * Return GitHub Issues modal tabs in DOM order.
   *
   * @param {HTMLElement} root - Mounted GitHub Issues modal root.
   * @returns {HTMLElement[]} Tab buttons controlled by this asset.
   */
  function tabElements(root) {
    return Array.from(root.querySelectorAll("[data-github-issues-modal-tab]"));
  }

  /**
   * Return GitHub Issues modal panels in DOM order.
   *
   * @param {HTMLElement} root - Mounted GitHub Issues modal root.
   * @returns {HTMLElement[]} Panels associated with modal tabs.
   */
  function panelElements(root) {
    return Array.from(root.querySelectorAll("[data-github-issues-modal-panel]"));
  }

  /**
   * Activate one tab and hide inactive GitHub Issues modal panels.
   *
   * @param {HTMLElement} root - Mounted GitHub Issues modal root.
   * @param {HTMLElement} tab - Tab element to activate.
   * @param {object} options - Activation options.
   * @param {boolean} options.focus - Whether keyboard focus should move to the tab.
   * @returns {void}
   */
  function activateTab(root, tab, { focus = false } = {}) {
    if (!(tab instanceof HTMLElement)) {
      return;
    }
    const tabId = String(tab.dataset.githubIssuesTabId || "");
    for (const candidate of tabElements(root)) {
      const selected = candidate === tab;
      candidate.setAttribute("aria-selected", selected ? "true" : "false");
      candidate.tabIndex = selected ? 0 : -1;
    }
    for (const panel of panelElements(root)) {
      panel.hidden = String(panel.dataset.githubIssuesTabId || "") !== tabId;
    }
    if (focus) {
      tab.focus();
    }
  }

  /**
   * Move keyboard selection to a neighboring GitHub Issues modal tab.
   *
   * @param {HTMLElement} root - Mounted GitHub Issues modal root.
   * @param {HTMLElement} current - Currently focused tab.
   * @param {number} direction - Relative movement, usually -1 or 1.
   * @returns {void}
   */
  function moveTab(root, current, direction) {
    const tabs = tabElements(root);
    const index = tabs.indexOf(current);
    if (index < 0 || !tabs.length) {
      return;
    }
    const nextIndex = (index + direction + tabs.length) % tabs.length;
    activateTab(root, tabs[nextIndex], { focus: true });
  }

  registry.github_issues = {
    ...previous,

    /**
     * Bind GitHub Issues modal tabs while persistence stays on generic block UI fields.
     *
     * @param {HTMLElement} root - Mounted GitHub Issues modal root.
     * @param {object} api - Generic block UI API passed by the framework.
     * @param {object} context - Render context returned by block.py.
     * @returns {void}
     */
    mount(root, api, context) {
      previous.mount?.(root, api, context);
      const selected = root.querySelector('[data-github-issues-modal-tab][aria-selected="true"]')
        || root.querySelector("[data-github-issues-modal-tab]");
      activateTab(root, selected);

      root.addEventListener("click", (event) => {
        const tab = event.target.closest("[data-github-issues-modal-tab]");
        if (!tab || !root.contains(tab)) {
          return;
        }
        event.preventDefault();
        activateTab(root, tab, { focus: true });
      });

      root.addEventListener("keydown", (event) => {
        const tab = event.target.closest("[data-github-issues-modal-tab]");
        if (!tab || !root.contains(tab)) {
          return;
        }
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          event.preventDefault();
          moveTab(root, tab, 1);
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          event.preventDefault();
          moveTab(root, tab, -1);
        } else if (event.key === "Home") {
          event.preventDefault();
          activateTab(root, tabElements(root)[0], { focus: true });
        } else if (event.key === "End") {
          event.preventDefault();
          const tabs = tabElements(root);
          activateTab(root, tabs[tabs.length - 1], { focus: true });
        }
      });
    },
  };
})();

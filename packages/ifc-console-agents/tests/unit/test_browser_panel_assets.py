"""Static browser-panel checks for the optional agents distribution.

These assertions cover the agent panel itself and its integration contract with
the viewer bundled by IFC Console.
"""

from __future__ import annotations

import re

import pytest
from ifc_console.viewer import assets as viewer_assets

from ifc_console_agents import assets as agent_assets

VIEWER_STATIC = viewer_assets.require_static_dir()
AGENT_STATIC = agent_assets.require_static_dir()
# The end of a top-level function body in the panel modules.
SPLIT_BLOCK_END = chr(10) + "  }"


def _agent_asset(name: str) -> str:
    return (AGENT_STATIC / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (VIEWER_STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (VIEWER_STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles() -> str:
    return (VIEWER_STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def measure_math() -> str:
    return (VIEWER_STATIC / "measure_math.js").read_text(encoding="utf-8")


def test_ifc_tabs_keep_parsed_revisions_and_model_scoped_selections(
    script: str, styles: str, chat_js: str
) -> None:
    load = script.split("async function loadModel()", 1)[1].split(
        "/**\n * Yield to the browser", 1
    )[0]
    assert "cachedParsedModel(targetModelId, targetEtag)" in load
    assert "buildScene(null, cached.parsed" in load
    build = script.split("async function buildScene(buffer)", 1)[1].split(
        "// ---------------------------------------------------------------- spatial tree", 1
    )[0]
    assert "residentParsed || await parseBuffer(buffer)" in build
    assert "cacheParsedModel(targetModelId, targetEtag, parsed)" in build
    assert build.index("await parseBuffer(buffer)") < build.index("disposeModel();")
    assert "PARSED_CACHE_MAX_ENTRIES = 2" in script
    assert "PARSED_CACHE_BUDGET" in script

    context = script.split("function viewerContext(", 1)[1].split(
        "function scheduleViewerContext", 1
    )[0]
    assert "selections: modelSelectionRows()" in context
    wire = script.split("function sendSelection()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "selections = modelSelectionRows()" in wire
    assert "model_id: item.model_id" in wire
    assert "selections });" in wire
    assert 'class="model-tab-selection-count"' not in script
    assert '"model-tab-selection-count"' in script
    assert ".model-tab-selection-count" in styles

    assert "function viewerSelections()" in chat_js
    assert "sessionStatus.selections" in chat_js
    assert 'action: "clear-model-selection"' in chat_js
    assert "selectedModel.model_id" in chat_js

def test_agent_workspace_can_hide_and_reopen_the_ifc_surface(
    html: str, script: str, styles: str, chat_css: str
) -> None:
    assert '<span id="brand">' in html
    assert '$("brand").addEventListener' not in script
    assert '$("brand").setAttribute("aria-pressed"' not in script
    assert 'id="viewer-empty"' in html
    close = script.split("function closeViewerSurface", 1)[1].split(
        "function openActiveViewerModel", 1
    )[0]
    assert "viewerDocumentOpen = false" in close
    assert "setSelection([], false)" in close
    assert 'scheduleViewerContext("closed")' in close
    assert 'body.viewer-closed #canvas-wrap' in styles
    assert 'body.viewer-closed #viewer-toolbar' in styles
    assert 'body.viewer-closed #chat-dock:not([hidden])' in chat_css

def test_chat_splitter_updates_aria_and_primary_agent_has_no_close_callback(
    script: str, chat_css: str
) -> None:
    handler = script.split('chatResize.addEventListener("keydown"', 1)[1].split(
        "\n});", 1
    )[0]
    for key in ("Home", "ArrowLeft", "ArrowRight"):
        assert f'event.key === "{key}"' in handler or f'event.key !== "{key}"' in handler
    aria = script.split("function syncChatResizeAria", 1)[1].split(
        "function setChatWidth", 1
    )[0]
    for attribute in (
        "aria-valuemin",
        "aria-valuemax",
        "aria-valuenow",
        "aria-valuetext",
    ):
        assert f'"{attribute}"' in aria
    mount = script.split("chatPanel ||= mountPanel", 1)[1].split("return chatPanel", 1)[0]
    assert "onClose" not in mount
    assert "extensionPanelPrimary" in script
    assert "#chat-dock-resize:focus-visible" in chat_css

def _assert_plain_object_check(script: str) -> None:
    check = script.split("function isPlainObject(value)", 1)[1].split("\n}", 1)[0]
    assert "value !== null" in check
    assert 'typeof value === "object"' in check
    assert "!Array.isArray(value)" in check
    assert "Object.getPrototypeOf(value) === Object.prototype" in check

# ------------------------------------------------------------- the chat panel
@pytest.fixture(scope="module")
def chat_js() -> str:
    return _agent_asset("chat.js")


@pytest.fixture(scope="module")
def chat_css() -> str:
    return _agent_asset("chat.css")


@pytest.fixture(scope="module")
def workflows_js() -> str:
    return _agent_asset("workflows.js")


@pytest.fixture(scope="module")
def workflows_css() -> str:
    return _agent_asset("workflows.css")


def test_workflows_separate_authoring_from_concurrent_run_history(
    workflows_js: str, workflows_css: str
) -> None:
    """Two views over one library: Library reads and runs, Runs streams and
    keeps. Authoring is a page of the Library reached from Edit or New."""
    for label in (
        'data-section="launch"',
        'data-section="runs"',
        'data-act="switch-agent"',
        'data-act="edit-workflow"',
        'data-act="run-selected"',
        'data-act="run-one"',
        'class="wf-prompt-fold"',
        'data-role="scope"',
        'class="wf-pipeline"',
    ):
        assert label in workflows_js
    # The editor is a state of the shell, not a third tab.
    assert 'shell.dataset.section = editing() ? "workflows" : section' in workflows_js
    assert 'data-section="workflows"' not in workflows_js
    assert 'started.forEach((run) => { void executeRun(run); })' in workflows_js
    assert 'runs.unshift(...started)' in workflows_js
    assert 'workflows/${isNew ? "create" : "update"}' in workflows_js
    assert 'Hidden reasoning is not displayed' in workflows_js
    assert ".wf-name-row" in workflows_css
    assert ".wf-prompt-fold" in workflows_css
    assert ".wf-run-thread" in workflows_css
    assert ".wf-setting-row" in workflows_css
    assert ".wf-card" in workflows_css
    assert ".wf-hero" in workflows_css
    assert ".wf-pipeline" in workflows_css


def test_a_streaming_run_is_patched_not_rebuilt(workflows_js: str) -> None:
    """Rebuilding the thread on every token re-parsed every entry and made a
    long run cost more the longer it went. New entries append; the growing
    paragraph is the only thing repainted; detail is bounded at receipt."""
    patch = workflows_js.split("function patchRunDetail(run", 1)[1].split(
        "function renderRunsMain", 1
    )[0]
    assert "run.entries.slice(run.painted)" in patch
    assert "if (!entry.dirty) continue;" in patch
    assert 'canvas.dataset.run !== run.id' in patch
    assert "const RUN_LIMIT = 30;" in workflows_js
    assert "const ENTRY_LIMIT = 300;" in workflows_js
    assert "const DETAIL_LIMIT = 6000;" in workflows_js
    added = workflows_js.split("function addRunEntry(run", 1)[1].split("function applyRunEvent", 1)[0]
    assert "clipText(valueText(detail), DETAIL_LIMIT)" in added
    # The full tool envelope stays on the console; the panel keeps the preview.
    result = workflows_js.split('event.type === "tool_result"', 1)[1].split(
        'event.type === "usage"', 1
    )[0]
    assert "event.preview" in result
    assert "payload = { ...event }" not in result


def test_the_library_page_can_hand_a_workflow_to_the_chat(workflows_js: str) -> None:
    assert 'data-act="attach-workflow"' in workflows_js
    assert "options.onAttach?.(flow.name, { scope: resolveRunScope(flow), note: noteFor(flow.name).trim() })" in workflows_js
    # The control only exists where a chat is listening.
    assert "options.onAttach ? `<button" in workflows_js


def test_starting_a_run_asks_for_nothing_but_a_prompt(workflows_js: str) -> None:
    """A run is a click. The only thing a user may type is one run prompt, and
    the declared-input fields survive for hand-written workflows only."""
    assert "data-run-note" in workflows_js
    assert "Prompt for this run" in workflows_js
    assert "wf-card-legacy" in workflows_js
    # No shared key/value editor on the way to a run: defaults live in Setup.
    assert 'data-owner="launcher"' not in workflows_js
    assert 'data-role="launcher-settings"' not in workflows_js


def test_a_finished_run_can_be_continued_in_place(
    workflows_js: str, workflows_css: str
) -> None:
    assert "/api/agents/workflows/continue" in workflows_js
    assert 'data-role="composer"' in workflows_js
    assert "follow_up_completed" in workflows_js
    # A tool waiting on approval has to be answerable, or the run just stalls.
    assert 'data-act="answer-approval"' in workflows_js
    assert ".wf-composer" in workflows_css


@pytest.fixture(scope="module")
def chat_history_js() -> str:
    return _agent_asset("chat_history.js")


@pytest.fixture(scope="module")
def chat_markdown_js() -> str:
    return _agent_asset("chat_markdown.js")


@pytest.fixture(scope="module")
def chat_flow_js() -> str:
    return _agent_asset("chat_flow.js")


@pytest.fixture(scope="module")
def chat_sidebar_js() -> str:
    return _agent_asset("chat_sidebar.js")


@pytest.fixture(scope="module")
def chat_workspace_js() -> str:
    return _agent_asset("chat_workspace.js")


@pytest.fixture(scope="module")
def chat_page_js() -> str:
    return _agent_asset("chat-page.js")


def test_the_chat_page_runs_no_inline_script():
    """The page's CSP is script-src 'self'; an inline module would be blocked."""
    html = _agent_asset("chat.html")
    assert "chat-page.js" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script in chat.html"


def _template(chat_js: str) -> str:
    """Only the markup, never the selector strings that look like markup.

    Scanning the whole file for data-act= counted `querySelector('[data-act=
    "history"]')` as a declaration, so a control that had been deleted from the
    template still looked present and its lookup returned null at runtime.
    """
    body = chat_js.split("const TEMPLATE = `", 1)[1]
    return body.split("\n`;", 1)[0]


def _attributes(chat_js: str, name: str) -> set[str]:
    """Attributes the panel writes, never the selectors that look for them.

    `[data-act="history"]` inside a querySelector used to count as a control,
    so a button deleted from the markup still looked present and its lookup
    returned null at runtime.
    """
    return set(re.findall(rf'(?<!\[){name}="([\w-]+)"', chat_js))


def _attributes_all(chat_js: str, name: str, value: str) -> list[str]:
    """Every place the panel writes one exact attribute, selectors excluded."""
    return re.findall(rf'(?<!\[){name}="{value}"', chat_js)


def _css_rule_with(source: str, selector: str, declaration: str) -> str:
    """Return a rule by meaning, without depending on selector formatting."""
    for written_selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", source, re.S):
        if selector in written_selector and declaration in body:
            return body
    raise AssertionError(f"no {selector!r} rule containing {declaration!r}")


def test_every_role_the_script_reads_exists_in_the_markup(chat_js: str):
    """Markup and script drifting apart is the one bug a browser-less suite
    can still catch: el("x") must have a data-role="x" behind it."""
    declared = _attributes(chat_js, "data-role")
    used = set(re.findall(r'el\("([\w-]+)"\)', chat_js))
    used |= set(re.findall(r'\[data-role="([\w-]+)"\]', chat_js))
    assert used - declared == set(), f"chat.js reads roles that do not exist: {used - declared}"


def test_every_action_the_script_handles_exists_in_the_markup(chat_js: str):
    declared = _attributes(chat_js, "data-act")
    declared |= set(re.findall(r'\.dataset\.act = "([\w-]+)"', chat_js))
    handled = set(re.findall(r'(?:if|else if) \(action === "([\w-]+)"', chat_js))
    queried = set(re.findall(r'\[data-act="([\w-]+)"\]', chat_js))
    queried |= set(re.findall(r'act\("([\w-]+)"\)', chat_js))
    assert handled <= declared, f"handlers without a button: {handled - declared}"
    assert queried <= declared, f"selectors for controls that do not exist: {queried - declared}"
    # every button must be handled somewhere: as an action, or wired directly
    for action in declared:
        assert action in handled or f'[data-act="{action}"]' in chat_js, f"dead button: {action}"


def test_the_dock_takes_its_colours_from_the_viewer(chat_css: str):
    """Its own hardcoded palette made the dock clash with the viewer chrome
    and stay dark when the console switched to the light theme."""
    block = chat_css.split(".chat-root {", 1)[1].split("}", 1)[0]
    for line in block.splitlines():
        if line.strip().startswith("--chat-") and "#" in line:
            assert "var(--" in line, f"hardcoded colour in the dock: {line.strip()}"


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_quiet_text_tokens_meet_wcag_aa(styles: str, chat_css: str) -> None:
    for source in (styles, chat_css):
        assert "--text-quiet: #83909c;" in source
        assert "--text-quiet: #596875;" in source
    for foreground, background in (
        ("#83909c", "#171e27"),
        ("#83909c", "#101720"),
        ("#596875", "#f5f8fb"),
        ("#596875", "#e7edf3"),
    ):
        assert _contrast(foreground, background) >= 4.5


def test_docked_transcript_text_is_selectable(chat_css: str) -> None:
    log = chat_css.split(".chat-log {", 1)[1].split("}", 1)[0]
    assert "user-select: text;" in log


def test_markdown_escapes_attribute_delimiters(chat_markdown_js: str) -> None:
    escaping = chat_markdown_js.split("export const esc =", 1)[1].split(
        "function mdInline", 1
    )[0]
    assert '.replace(/"/g, "&quot;")' in escaping
    assert ".replace(/'/g, \"&#39;\")" in escaping


def test_workspace_is_one_native_dialog_with_vertical_navigation(
    chat_js: str, chat_css: str
) -> None:
    template = _template(chat_js)
    assert '<dialog class="chat-workspace"' in template
    assert 'aria-labelledby="chat-workspace-label"' in template
    assert 'aria-describedby="chat-workspace-context"' in template
    assert 'role="tablist" aria-orientation="vertical"' in template
    assert set(re.findall(r'data-workspace-view="([\w-]+)"', template)) == {
        "agent",
        "capabilities",
        "tools",
        "content",
        "skills",
        "models",
        "app",
    }
    assert 'data-role="workspace-agents"' in template
    assert 'data-role="settings-models"' in template
    assert 'data-role="settings-app"' in template
    assert 'data-workspace-view="settings"' not in template
    assert 'data-role="ws-tabs"' not in template
    assert 'dialog.showModal()' in chat_js
    assert "if (dialog.open) dialog.close();" in chat_js
    assert ".chat-settings-view[hidden]" in chat_css


def test_workspace_dialog_is_bounded_and_centred(chat_css: str) -> None:
    modal = _css_rule_with(chat_css, "dialog.chat-workspace", "position: fixed")
    # inset 0 plus auto margins is the whole centring mechanism; either half
    # alone pins the sheet back to a corner.
    assert re.search(r"inset:\s*0\s*;", modal), "the sheet must be centred, not corner-pinned"
    assert re.search(r"margin:\s*auto\s*;", modal), "auto margins do the centring"
    assert re.search(r"width:\s*min\(\d+px,\s*calc\(100%\s*-\s*\d+px\)\)", modal)
    assert re.search(r"max-width:\s*\d+px", modal)
    assert re.search(r"height:\s*min\(\d+px,\s*calc\(100%\s*-\s*\d+px\)\)", modal)
    assert re.search(r"max-height:\s*\d+px", modal)
    assert "width: 100%" not in modal
    assert "dialog.chat-workspace::backdrop" in chat_css
    opened = _css_rule_with(chat_css, "dialog.chat-workspace[open]", "display: grid")
    assert "display: grid" in opened


def test_chat_dialog_contains_focus_and_restores_it(chat_js: str):
    assert "settingsReturnFocus" in chat_js
    assert 'event.key === "Tab"' in chat_js
    assert "node.tabIndex >= 0" in chat_js
    assert 'aria-controls="chat-workspace"' in chat_js
    assert "focusQuietly(overlayReturnTarget(target))" in chat_js
    close_workspace = chat_js.split("function closeWorkspace", 1)[1].split(
        "function toggleWorkspace", 1
    )[0]
    assert "settingsReturnFocus = null;" in close_workspace


def test_workspace_agent_switches_restore_the_originating_control(chat_js: str) -> None:
    assert 'switchAgent(agent.name, { workspaceFocus: "agent" })' in chat_js
    assert '.chat-workspace-agent[aria-current="true"]' in chat_js
    assert 'switchAgent(compactSelect.value, { workspaceFocus: "agent-select" })' in chat_js
    assert "requestAnimationFrame(restoreWorkspaceFocus)" in chat_js


def test_chat_status_and_send_state_are_accessible(chat_js: str):
    assert 'role="log" aria-label="Conversation"' in chat_js
    assert 'data-role="announce" role="status"' in chat_js
    assert 'send.setAttribute("aria-label", "Stop response")' in chat_js
    # Idle, the control is Send, or Run while a workflow waits to start.
    idle = chat_js.split("function syncSendIdle()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert '"Send message"' in idle
    assert "`Run ${conversationWorkflow.title}`" in idle
    assert 'send.classList.toggle("run", starting)' in idle


def test_agent_setup_uses_allowlisted_blocks_inside_the_workspace(
    chat_js: str, chat_css: str
) -> None:
    assert 'id="chat-builder"' in chat_js
    assert '<section class="chat-workspace-pane chat-studio"' in chat_js
    assert 'aria-label="Agent setup"' in chat_js
    assert 'postJSON("/api/agents/custom"' in chat_js
    assert 'api("/api/agents/blocks")' in chat_js
    assert '<details class="chat-studio-advanced">' in chat_js
    setup = _css_rule_with(chat_css, ".chat-ws-content > .chat-studio", "position: relative")
    assert "position: relative" in setup
    assert "width: 100%" in setup
    assert "height: 100%" in setup
    assert 'data-role="studio-capabilities"' in chat_js
    assert 'el("studio-capabilities").open = !studioDraft.blocks.length' in chat_js
    assert 'role="region" aria-label="Agent setup fields" tabindex="0"' in chat_js
    assert 'scroller.scrollBy({ top: event.key === "PageDown" ? page : -page' in chat_js
    assert 'scroller.scrollTo({ top: event.key === "Home" ? 0 : scroller.scrollHeight' in chat_js
    assert ".chat-block input:checked + span" in chat_css
    assert ".chat-studio [hidden] { display: none !important; }" in chat_css
    assert '<dialog class="chat-studio"' not in chat_js
    assert 'data-act="close-builder" type="button"' in chat_js
    assert 'setWorkspaceView("agent")' in chat_js


def test_agent_setup_keeps_focus_and_refreshes_an_edited_active_agent(
    chat_js: str,
) -> None:
    assert '<h2 tabindex="-1">Set up one clear IFC job.</h2>' in chat_js
    assert 'requestAnimationFrame(() => focusQuietly(el("builder-title")))' in chat_js
    assert "studioReturnFocus && studioReturnFocus.isConnected" in chat_js
    assert "const editedCurrent = editing && payload.agent.name === currentAgent;" in chat_js
    assert "startConversation(true, { focus: false });" in chat_js
    assert "workspace = null;" in chat_js
    assert "await loadWorkspace({ force: true });" in chat_js
    opener = chat_js.split("function openBuilder(", 1)[1].split(
        "function closeBuilder", 1
    )[0]
    assert opener.index("writeStudioDraft(studioDraft)") < opener.index(
        'openWorkspace(trigger, "builder")'
    )


def test_agent_workspace_connects_content_and_viewer_context(chat_js: str) -> None:
    for endpoint in (
        "/api/agents/content",
        "/api/agents/content/access",
        "/api/agents/content/upload",
        "/api/session/mode",
    ):
        assert endpoint in chat_js
    for control in (
        'data-role="content-file"',
        'data-role="ifcmodel"',
        'data-role="session-mode"',
        'data-role="plus-menu"',
        "captureViewerEvidence()",
    ):
        assert control in chat_js
    for event_name in (
        "ifc-console:viewer-context",
        "ifc-console:viewer-command",
        "ifc-console:viewer-result",
    ):
        assert event_name in chat_js
    assert 'sendViewerCommand({' in chat_js
    assert 'action: "capture-evidence"' in chat_js
    assert "options.viewer?.version === 1" in chat_js
    assert "viewer.execute(detail)" in chat_js
    assert "viewer.subscribe(applyViewerContext)" in chat_js
    assert "viewer.subscribeResults?." in chat_js
    assert "const binary = atob(" in chat_js
    assert "fetch(result.dataUrl)" not in chat_js


def test_workspace_reviews_v2_geometry_and_skills_without_writing(
    chat_js: str, chat_css: str, chat_workspace_js: str
) -> None:
    assert 'postJSON("/api/agents/geometry/review"' in chat_js
    assert 'postJSON("/api/agents/skills/dry-run"' in chat_js
    assert 'detail: "standard"' in chat_js
    assert 'model: selection.model_id' in chat_js
    assert 'global_ids: [guids[0]]' in chat_js
    assert "canonicalSelectionGuids(selection)" in chat_js
    assert "Select exactly one object" in chat_js
    assert "measurement spec v" in chat_js
    assert 'area: "m^2"' in chat_js and 'volume: "m^3"' in chat_js
    assert "Â" not in chat_js and "Ã" not in chat_js
    assert "skill.executable" in chat_js
    assert "applicability.reasons" in chat_js
    assert "No IFC property was proposed" in chat_js
    assert "Prepare a separate property proposal" in chat_js
    assert "workspaceReviewSelectionToken" in chat_js
    assert "workspaceReviewSelectionEpoch" in chat_js
    assert "requestGeneration" in chat_js
    assert "workspaceReviewRequestIsCurrent" in chat_js
    assert "skillDryRunStateKey" in chat_js
    assert "currentSkillDryRunState" in chat_js
    assert "selectionTooLarge" in chat_js
    assert "global_ids: guids" in chat_js
    assert "limit: guids.length" in chat_js
    assert ".slice(0, 200)" not in chat_js
    assert "measurementDryRunCanPropose" in chat_js
    for partial_marker in (
        "targets.has_more",
        "targets.truncated_by_max_matches",
        "envelopeMeta.truncated",
        "dataMeta.truncated",
    ):
        assert partial_marker in chat_workspace_js
    assert '["extracted", "partial"].includes' in chat_workspace_js
    assert "result.extracted.length > 0" in chat_workspace_js
    assert ".chat-ws-data-table" in chat_css
    assert ".chat-ws-review-state.bad" in chat_css


def test_geometry_review_visualizes_frames_sections_and_evidence_read_only(
    chat_js: str, chat_css: str, chat_workspace_js: str
) -> None:
    assert "appendSemanticFrame(detail, record)" in chat_js
    assert 'document.createElementNS("http://www.w3.org/2000/svg"' in chat_js
    for axis in ("longitudinal", "transverse", "vertical"):
        assert f'["{axis}",' in chat_js
        assert f".chat-ws-axis-{axis}" in chat_css
    assert "frame.source" in chat_js and "frame.confidence" in chat_js
    assert "Read-only 2D projection" in chat_js
    assert "appendSectionBrowser(detail, record)" in chat_js
    assert 'slider.type = "range"' in chat_js
    assert "representativeSectionStations(analysis)" in chat_js
    assert ".chat-ws-section-regions" in chat_css
    assert "measurementEvidenceBadge(measurement)" in chat_js
    assert "appendMeasurementEvidence(detail, record, measurement)" in chat_js
    assert "Element length tolerance +/-" in chat_js
    assert "No source was silently discarded" in chat_js
    assert ".chat-ws-evidence-badge.exact" in chat_css
    assert ".chat-ws-evidence-badge.measured" in chat_css
    assert "measurementEvidenceKind" in chat_workspace_js
    assert "measurementAlternativeRows" in chat_workspace_js


def test_content_state_is_scoped_and_permission_writes_are_serialized(
    chat_js: str,
) -> None:
    assert 'let contentLibraryAgent = ""' in chat_js
    assert "let contentLibraryRequest = 0" in chat_js
    assert "const contentAccessQueues = new Map()" in chat_js
    loader = chat_js.split("async function loadContentLibrary", 1)[1].split(
        "function updateContentAccessDraft", 1
    )[0]
    assert "const agent = currentAgent" in loader
    assert "ticket !== contentLibraryRequest || agent !== currentAgent" in loader
    saver = chat_js.split("function saveContentAccess", 1)[1].split(
        "async function uploadWorkspaceContent", 1
    )[0]
    assert "const previous = contentAccessQueues.get(agent)" in saver
    assert "contentAccessRevisions.get(agent) !== revision" in saver


def test_settings_apply_is_deduplicated_serialized_and_generation_guarded(
    chat_js: str,
) -> None:
    assert "let settingsApplyQueue = Promise.resolve()" in chat_js
    saver = chat_js.split("function saveSettings()", 1)[1].split(
        "function forkConversationForConfigurationChange", 1
    )[0]
    assert "queuedConnection === requestSignature" in saver
    assert "settingsApplyQueue = settingsApplyQueue.catch(() => {}).then" in saver
    assert saver.count("revision !== settingsApplyRevision") >= 3
    assert 'provider()?.id === id && el("key").value.trim() === key' in saver


def test_ifc_tabs_start_in_the_viewer_column_and_agent_settings_stay_in_its_header(
    html: str, script: str, styles: str, chat_js: str, chat_css: str
) -> None:
    rail = html.split('<div id="viewer-rail">', 1)[1].split("</div>", 1)[0]
    assert 'id="btn-chat"' not in rail
    assert 'id="model-tabs"' in rail
    assert 'class="agent-workspace-tab"' not in html
    assert html.index('<main id="layout">') < html.index('<div id="viewer-rail">')
    assert html.index('id="chat-dock"') < html.index('<div id="viewer-rail">')
    assert html.index('id="chat-dock"') < html.index('id="viewer-stage"')
    layout = styles.split("#layout {", 1)[1].split("}", 1)[0]
    rail_style = styles.split("#viewer-rail {", 1)[1].split("}", 1)[0]
    stage_style = styles.split("#viewer-stage {", 1)[1].split("}", 1)[0]
    assert "display: grid" in layout
    assert "grid-column: 3" in rail_style
    assert "padding: 0 7px 0 0" in rail_style
    assert "grid-column: 3" in stage_style
    dock_style = chat_css.split("#chat-dock {", 1)[1].split("}", 1)[0]
    assert "grid-column: 1" in dock_style
    assert "grid-row: 1 / 3" in dock_style
    assert 'id="btn-settings"' not in html.split('<div id="viewer-rail">', 1)[0]
    assert 'id="btn-settings"' in html.split('id="viewer-toolbar"', 1)[1]
    chat_header = chat_js.split('<header class="chat-head">', 1)[1].split(
        "</header>", 1
    )[0]
    assert 'class="chat-title" data-role="title"' in chat_header
    assert 'title="Agent settings" aria-label="Open agent settings"' in chat_header
    assert "mountPanel(chatDock, { viewer: viewerComponentHost.api })" in script
    assert "const extensionPanelPrimary" in script


def test_model_capability_controls_and_key_location_are_visible(
    chat_js: str, chat_css: str
) -> None:
    assert 'data-role="toolcap"' in chat_js
    assert 'data-role="visioncap"' in chat_js
    assert 'tools_supported: effectiveCapability("tools")' in chat_js
    assert 'vision_supported: effectiveCapability("vision")' in chat_js
    assert 'el("keyfield").hidden = !p.needs_key;' in chat_js
    assert "operating system under service ifc-console" in chat_js
    assert ".chat-composer-select select option" in chat_css


def test_stream_repaint_keeps_a_followed_answer_in_view(chat_js: str) -> None:
    run = chat_js.split("async function run({ retry = false } = {})", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert "schedule(stick);" in run
    assert "repaintShouldScroll && followingOutput" in run
    assert "if (shouldScroll) scroll();" in run
    assert "const finishStick = followingOutput;" in run
    assert "if (finishStick) scroll();" in run


def test_upward_scroll_owns_the_viewport_while_output_keeps_rendering(
    chat_js: str,
) -> None:
    scroll_control = chat_js.split("const nearBottom =", 1)[1].split(
        "/* True while the reader", 1
    )[0]
    assert "let followingOutput = true;" in scroll_control
    assert 'log.addEventListener("wheel"' in scroll_control
    assert "if (event.deltaY < 0) followingOutput = false;" in scroll_control
    assert "if (top < lastScrollTop - 0.5) followingOutput = false;" in scroll_control
    assert "top > lastScrollTop + 0.5 && nearBottom()" in scroll_control

    run = chat_js.split("async function run({ retry = false } = {})", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert "const stick = followingOutput;" in run
    assert "const heldScrollTop = followingOutput ? null : log.scrollTop;" in run
    assert "if (heldScrollTop !== null)" in run
    assert "log.scrollTop = heldScrollTop;" in run
    assert "if (stick && followingOutput && !repaint) scroll();" in run


def test_tool_results_render_lazily_and_progress_is_throttled(chat_js: str) -> None:
    tool = chat_js.split("function toolNode()", 1)[1].split(
        "function paintBlock", 1
    )[0]
    assert 'details.addEventListener("toggle"' in tool
    assert "function paintToolBody(" in tool
    assert 'block.output !== null' in tool
    assert '"Output preview"' in tool
    run = chat_js.split("async function run({ retry = false } = {})", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert 'event.type === "tool_progress"' in run
    assert "schedule(stick);" in run


def test_agent_attachments_and_ai_proposals_are_first_class(
    chat_js: str, chat_css: str
) -> None:
    assert "pendingAttachments" in chat_js
    assert "attachments: retryInExistingAgentThread" in chat_js
    assert "lastUser?.attachments?.map" in chat_js
    assert 'event.type === "proposal"' in chat_js
    # The panel gates the call now, so the card no longer sends the reader
    # elsewhere to approve it; what it still states is provenance and that
    # nothing has reached the file.
    assert "AI-marked" in chat_js
    assert "Review and approve this revision-bound ChangeSet" not in chat_js
    assert "Nothing is written to the IFC file until you save." in chat_js
    assert ".chat-proposal" in chat_css
    assert ".chat-step" in chat_css
    # one normalizer for the wire shape, shared with the AI SDK boundary
    assert "normalizeIfcProposal(event)" in chat_js
    assert "proposal.changeSetId" in chat_js


def test_escape_closes_the_dialog_before_stopping_the_stream(chat_js: str):
    # Slice the Escape chain itself: the panel also registers a capture-phase
    # keydown listener to track input modality, which is not this handler.
    chain = chat_js.split('if (event.key !== "Escape") return;', 1)[1].split(
        "event.stopPropagation();", 1
    )[0]
    assert chain.index("closeSettings()") < chain.index("aborter?.abort()")
    assert chain.index("closeWorkspace()") < chain.index("aborter?.abort()")


def test_composer_shortcuts_live_behind_a_stable_keyboard_control(
    chat_js: str, chat_css: str
) -> None:
    assert '<b>Enter</b> queues' not in chat_js
    assert "keyboard: svg(" in chat_js
    assert "${I.keyboard}</button>" in chat_js
    assert 'data-act="shortcuts"' in chat_js
    assert 'aria-controls="chat-shortcuts"' in chat_js
    for copy in (
        "Send a message",
        "Start a new line",
        "Queue a message while a response is running",
        "Stop the active response",
    ):
        assert copy in chat_js
    assert ".chat-shortcuts[hidden] { display: none; }" in chat_css
    assert '.chat-shortcuts-toggle[aria-expanded="true"]' in chat_css
    chain = chat_js.split('if (event.key !== "Escape") return;', 1)[1].split(
        "event.stopPropagation();", 1
    )[0]
    assert chain.index("closeShortcuts") < chain.index("aborter?.abort()")


def test_errors_use_top_notifications_instead_of_transcript_notes(
    chat_js: str, chat_css: str
) -> None:
    assert 'data-role="notifications"' in chat_js
    assert 'notification.setAttribute("role", "alert")' in chat_js
    assert 'data-act="dismiss-notification"' in chat_js
    assert 'text.replace(/\\s+\\(at [\\s\\S]+\\)\\s*$/, "")' in chat_js
    assert "This IFC element has no geometry in the model" in chat_js
    note = chat_js.split("function note(text, tone = false)", 1)[1].split(
        "async function uploadFiles", 1
    )[0]
    assert 'if (kind === "bad")' in note
    assert "notifyFailure(text);" in note
    assert note.index("notifyFailure(text);") < note.index('line.className = "chat-note"')
    assert ".chat-notifications" in chat_css
    assert "position: absolute;" in chat_css.split(".chat-notifications", 1)[1].split("}", 1)[0]
    assert "pointer-events: auto;" in chat_css.split(".chat-notification {", 1)[1].split("}", 1)[0]


def test_stopping_before_content_keeps_an_alternating_visible_transcript(
    chat_js: str, chat_css: str
) -> None:
    run = chat_js.split("async function run({ retry = false } = {})", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert 'const stoppedMessage = stopped && !text ? "Response stopped before content."' in run
    assert 'stoppedBox.className = "chat-answer stopped";' in run
    assert "stoppedBox.textContent = stoppedMessage;" in run
    assert "text: transcriptText," in run
    assert ".chat-answer.stopped" in chat_css


def test_a_stopped_run_cannot_land_in_the_next_conversation(chat_js: str) -> None:
    """Aborting a fetch is asynchronous, so a run keeps unwinding after the
    user has already switched assistant or started a new chat."""
    run = chat_js.split("async function run({ retry = false } = {})", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert "conversationId: currentConversationId" in run
    assert "const runConversation = runIdentity.conversationId;" in run
    assert "if (!runIsCurrent(runIdentity)) return;" in run
    assert "conversationThreads[runConversation]" in run
    starter = chat_js.split("function startConversation(", 1)[1].split("\n  }", 1)[0]
    assert "invalidateActiveRun()" in starter


def test_standalone_chat_uses_status_theme_after_os_default(
    chat_js: str, chat_css: str, chat_page_js: str
) -> None:
    assert 'typeof options.onStatus === "function"' in chat_js
    startup = chat_js.split("const urlAgent =", 1)[1].split("return {", 1)[0]
    assert startup.index("refreshContext();") < startup.index("loadProviders();")
    assert "onStatus: () =>" in chat_page_js
    assert "document.documentElement.dataset.consoleTheme = root.dataset.theme" in chat_page_js
    assert "html:not([data-console-theme]) body.chat-page" in chat_css
    assert 'html[data-console-theme="light"] body.chat-page' in chat_css


def test_a_stale_model_list_cannot_win(chat_js: str):
    """Switching provider mid-load once mixed two providers' models together."""
    assert "modelRequest" in chat_js
    assert chat_js.count("ticket !== modelRequest") >= 2


def test_the_panel_never_calls_a_provider_directly(chat_js: str):
    """Keys stay server side; every request goes to this origin."""
    for line in chat_js.splitlines():
        if "fetch(" in line or "postJSON(" in line:
            assert "http://" not in line and "https://" not in line, line.strip()


def test_the_send_button_is_gated_until_a_model_is_chosen(chat_js: str):
    gate = chat_js.split("send.disabled = resetInProgress", 1)[1].split(";", 1)[0]
    assert "|| uploadsInFlight()" in gate
    assert "|| (!ready && !busy)" in gate
    # A selection-scoped workflow cannot start on nothing.
    assert "|| (!busy && workflowNeedsSelection())" in gate


def test_a_composer_upload_shows_its_progress_and_holds_send(chat_js: str) -> None:
    """Indexing a PDF takes seconds; sending meanwhile drops a pathless
    attachment silently."""
    upload = chat_js.split("async function uploadFiles(", 1)[1].split(
        "function setModelOptions", 1
    )[0]
    assert "pending: true" in upload
    assert "pendingAttachments.push(placeholder)" in upload
    assert "delete placeholder.pending" in upload
    assert "if (resetInProgress || uploadsInFlight()) return;" in chat_js
    assert 'attachment.pending ? " pending" : ""' in chat_js


def test_no_api_key_is_ever_written_to_browser_storage(chat_js: str):
    """The panel promises the key only lives in the running console. Local
    storage is disk, so the key must not go near the settings blob."""
    saved = chat_js.split("function saveSettings()", 1)[1].split("\n  }", 1)[0]
    assert "localStorage" in saved, "the settings blob is still saved here"
    assert "settings.keys" not in chat_js, "keys are back in the persisted settings"
    for line in chat_js.splitlines():
        if "localStorage" in line or "sessionStorage" in line:
            assert "key" not in line.lower(), line.strip()


def test_chat_persisted_settings_require_a_plain_object_and_write_is_best_effort(
    chat_js: str,
) -> None:
    _assert_plain_object_check(chat_js)
    loader = chat_js.split("function loadSettings()", 1)[1].split("function chosenModel()", 1)[0]
    assert "if (!isPlainObject(saved)) saved = {};" in loader
    assert "byProvider: isPlainObject(saved.byProvider) ? saved.byProvider : {}" in loader

    writer = chat_js.split("function saveSettings()", 1)[1].split("const provider =", 1)[0]
    assert "try {" in writer
    assert "localStorage.setItem(STORE, JSON.stringify(settings));" in writer
    assert "} catch {" in writer


def test_conversations_are_local_exportable_and_separate_from_credentials(
    chat_js: str, chat_history_js: str
):
    assert 'from "./chat_history.js"' in chat_js
    assert "historyStore.save(conversationRecord())" in chat_js
    assert "transcriptMarkdown(record" in chat_js
    assert "provider" in chat_history_js.casefold()
    assert "credentials" in chat_history_js.casefold()
    assert "api_key" not in chat_history_js
    assert "password" not in chat_history_js


def test_a_row_delete_is_armed_before_it_acts(chat_js: str, chat_css: str) -> None:
    """Deleting one conversation or agent is as irreversible as Delete all and
    sits beside the control that opens it."""
    arm = chat_js.split("function sideDeleteButton(", 1)[1].split(
        "function deleteButton(", 1
    )[0]
    assert "armedDelete?.button === remove" in arm
    assert 'remove.classList.add("armed")' in arm
    assert 'remove.textContent = "Delete?"' in arm
    assert 'remove.setAttribute("aria-label", confirmLabel)' in arm
    # anything else cancels it
    assert 'if (!event.target.closest(".chat-side-delete")) disarmDelete();' in chat_js
    assert "if (armedDelete) disarmDelete();" in chat_js
    assert ".chat-side-delete.armed" in chat_css
    for factory in ("deleteButton", "sideDeleteButton"):
        assert f"function {factory}(" in chat_js


def test_settings_delete_all_conversations_with_an_explicit_confirmation(chat_js: str) -> None:
    template = _template(chat_js)
    for action in (
        "request-clear-history",
        "cancel-clear-history",
        "confirm-clear-history",
    ):
        assert f'data-act="{action}"' in template
        assert f'action === "{action}"' in chat_js
    reset = chat_js.split("async function clearAllConversations", 1)[1].split(
        "async function finishInitialHistoryReset", 1
    )[0]
    assert 'postJSON("/api/agents/threads/clear", {})' in reset
    assert "historyStore.clear({ includeLegacy: true })" in reset
    assert "conversationThreads = {};" in reset
    assert 'localStorage.setItem(HISTORY_RESET, "done")' in reset
    assert 'action === "clear-history"' not in chat_js


def test_history_mode_and_model_changes_fork_context_instead_of_reusing_it(
    chat_js: str,
) -> None:
    mode = chat_js.split("function changeHistoryMode()", 1)[1].split(
        "function exportConversation", 1
    )[0]
    assert "conversationThreads = {};" in mode
    assert "startConversation(false, { focus: false })" in mode
    assert 'el("savehistory").addEventListener("change", changeHistoryMode)' in chat_js
    assert "nextStatus.project_scope" in chat_js
    assert "nextStatus.fingerprint" in chat_js
    assert "if (changedScope)" in chat_js
    assert "function forkConversationForConfigurationChange(" in chat_js
    assert "AI provider changed. A fresh conversation is ready." in chat_js
    assert "AI model changed. A fresh conversation is ready." in chat_js


def test_panel_visibility_closes_transient_layers_without_stopping_a_run(chat_js: str) -> None:
    lifecycle = chat_js.split("setVisible: (visible) =>", 1)[1].split("\n    },", 1)[0]
    assert "closeSettings({ restoreFocus: false })" in lifecycle
    assert "closeWorkspace({ restoreFocus: false })" in lifecycle
    assert "setSide(false" in lifecycle
    assert "abort" not in lifecycle.casefold()


def test_context_flow_and_secure_key_controls_are_visible(chat_js: str, chat_css: str):
    """Route, model, and mode stay on screen; the rest moved to the workspace."""
    assert 'class="chat-context-rail"' in chat_js
    assert 'data-role="modelname"' in chat_js
    assert 'data-role="ifcmodel"' in chat_js
    assert 'data-role="session-mode"' in chat_js
    assert 'data-role="plus-menu"' in chat_js
    assert 'data-act="export"' in chat_js
    assert "operating-system credential store" in chat_js
    assert ".chat-context-rail" in chat_css


def test_tool_results_are_paired_to_their_call_by_id(chat_flow_js: str):
    """Two calls to one tool in a round left a card spinning forever."""
    assert "run.tools.find((item) => item.id === event.id)" in chat_flow_js
    assert "item.name === event.name" not in chat_flow_js


def test_a_tool_is_drawn_where_it_ran(chat_js: str, chat_css: str, chat_flow_js: str):
    """A pile of chips above the answer said what ran, never where or why."""
    assert "run.blocks.push(entry)" in chat_flow_js
    assert 'kind: "tool",' in chat_flow_js
    assert "function syncStream(" in chat_js
    assert "function paintTool(" in chat_js
    # the card carries the arguments the model chose and what came back
    assert 'toolPart(input.code ? "Code" : "Input", input.text' in chat_js
    assert '.chat-tool-card' in chat_css
    assert '.chat-tool-part pre' in chat_css
    assert ".chat-tools {" not in chat_css, "the chip strip is gone"


def test_chat_distinguishes_the_ai_model_from_the_open_ifc_model(chat_js: str):
    assert "no AI model" in chat_js
    assert 'data-role="modelname"' in chat_js
    assert 'data-role="ifcmodel"' in chat_js
    assert 'data-role="ifcmodel-wrap"' in chat_js


def test_chat_builds_requests_through_the_ai_sdk_compatibility_boundary(
    chat_js: str,
) -> None:
    assert 'from "./chat_ai_sdk.js"' in chat_js
    assert "agentChatRequest(requestMessages" in chat_js
    assert "plainChatRequest(requestMessages" in chat_js


def test_header_and_composer_controls_open_the_shared_models_view(chat_js: str) -> None:
    """Every model entry point resolves to Models in the same workspace."""
    template = _template(chat_js)
    openers = _attributes_all(chat_js, "data-act", "settings")
    assert len(openers) == 3, "header, composer, and first-run setup should share one action"
    header = template.split('<header class="chat-head">', 1)[1].split("</header>", 1)[0]
    assert header.index('class="chat-spacer"') < header.index("chat-model-setup-toggle")
    assert 'chat-model-setup-toggle t-press" data-act="settings"' in header
    assert 'chat-workspace-toggle t-press" data-act="workspace"' in header
    assert 'chat-model-pill t-press" data-act="settings"' in template
    composer = re.search(r'<button class="chat-composer-pill chat-model-pill[^>]+>', template)
    assert composer is not None
    assert 'aria-controls="chat-workspace"' in composer.group(0)
    assert 'aria-expanded="false"' in composer.group(0)
    assert 'openWorkspace(trigger, "models")' in chat_js
    assert 'data-workspace-view="models"' in template
    assert 'data-workspace-view="app"' in template
    assert 'data-workspace-view="settings"' not in template


def test_chat_header_shows_only_the_agent_name(chat_js: str) -> None:
    header = chat_js.split('<header class="chat-head">', 1)[1].split("</header>", 1)[0]
    assert 'class="chat-title" data-role="title"' in header
    assert 'class="chat-subtitle" data-role="reach" hidden' in header
    # Capability counts and preview policy still exist in Agent workspace, but
    # no longer take a second line under the active agent's name. The one
    # thing the subtitle names is the workflow the conversation stands on.
    assert 'el("reach").textContent = "";' not in chat_js
    subtitle = chat_js.split("function syncSubtitle()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "conversationWorkflow ? `Workflow · ${conversationWorkflow.title}` : \"\"" in subtitle
    assert "reach.hidden = !conversationWorkflow" in subtitle


def test_open_sidebars_hide_duplicate_header_launchers_and_keep_focus(
    chat_js: str, chat_css: str
) -> None:
    hidden = _css_rule_with(chat_css, ".chat-root.side-open .chat-side-toggle", "display: none")
    for selector in (
        ".chat-root.side-open .chat-workspace-toggle",
        ".chat-root.side-open .chat-model-setup-toggle",
    ):
        assert selector in chat_css
    assert "display: none" in hidden
    side = chat_js.split("function setSide(", 1)[1].split(
        "const closeSideIfOverlay", 1
    )[0]
    assert 'trigger.classList.contains("chat-side-toggle")' in side
    assert "sideOpen && moveFocus && (!sideIsInline() || openerWillHide)" in side
    assert 'focusQuietly(el("side").querySelector("button:not(:disabled)"))' in side


def test_chat_makes_provider_egress_visible(chat_js: str, chat_css: str):
    for label in ("local", "network", "blocked"):
        assert f".chat-route.{label}" in chat_css
    assert "Prompts stay on this machine." in chat_js
    assert "Blocked by chat.local_only" in chat_js


def test_compact_layout_uses_overlays_instead_of_squeezing_the_canvas(
    script: str, styles: str, chat_css: str
) -> None:
    assert "window.innerWidth > 620" in script
    compact = styles.split("@media (max-width: 620px)", 1)[1]
    assert "#tree-panel," in compact and "position: absolute" in compact
    dock = chat_css.split("@media (max-width: 1040px)", 1)[1]
    assert "position: absolute" in dock
    assert "z-index: 26" in dock
    assert "inset: 0 auto 0 0" in dock
    assert "width: min(720px, 100%)" in dock


def test_sidebar_rows_override_the_global_centered_button_alignment(
    chat_css: str,
) -> None:
    selector = ".chat-root .chat-side-item,\n.chat-root .chat-side-new"
    assert selector in chat_css
    block = chat_css.split(selector, 1)[1].split("}", 1)[0]
    assert "justify-content: flex-start" in block


def test_the_sidebar_lists_agents_and_conversations(
    chat_js: str, chat_css: str, chat_sidebar_js: str
) -> None:
    """The sidebar is the panel's map: which assistants exist and what was
    asked. Plain chat is one of them, or there is no way back to it."""
    assert 'data-role="side-agents"' in chat_js
    assert 'data-role="side-history"' in chat_js
    assert "PLAIN_CHAT" in chat_js and "PLAIN_CHAT" in chat_sidebar_js
    assert ".side-open .chat-side" in chat_css
    assert "@media (prefers-reduced-motion: reduce)" in chat_css


def test_the_sidebar_state_survives_a_reload_without_storing_secrets(
    chat_js: str,
) -> None:
    assert "localStorage.setItem(SIDE_STORE" in chat_js
    assert "localStorage.getItem(SIDE_STORE)" in chat_js
    assert 'const SIDE_STORE = "ifc-console-chat-side"' in chat_js


def test_the_landing_assistant_does_not_depend_on_a_race(chat_js: str) -> None:
    """saveSettings runs from a background fetch and writes settings.agent, so
    reading the stored preference late turned "never chosen" into plain chat
    whenever the model list happened to load before the agent list."""
    assert "let preferredAgent = settings.agent;" in chat_js
    loader = chat_js.split("async function loadAgents()", 1)[1].split(
        "async function loadBlocks()", 1
    )[0]
    assert "preferredAgent" in loader
    assert "settings.agent" not in loader, "the live settings blob is not the preference"
    assert 'agents.some((agent) => agent.name === "general")' in chat_js


def test_custom_agents_can_be_deleted_from_the_panel(
    chat_js: str, chat_sidebar_js: str
) -> None:
    assert 'postJSON("/api/agents/custom/delete"' in chat_js
    assert "agent.deletable" in chat_js, "the rail asks the model, not the raw payload"
    assert 'deletable: agent.kind === "custom"' in chat_sidebar_js


def test_a_stale_install_is_reported_before_an_upload_fails(
    chat_js: str, chat_css: str
) -> None:
    """The old failure mode was a PDF upload dying with a package name."""
    assert 'api("/api/agents/capabilities")' in chat_js
    assert "capabilities.repair" in chat_js
    assert ".chat-alert" in chat_css


def test_run_progress_lives_in_the_message_not_in_permanent_chrome(
    chat_js: str, chat_css: str, chat_flow_js: str
) -> None:
    """A stage rail pinned above every conversation was chrome the reader paid
    for on every turn; progress now appears in the message making it."""
    assert 'from "./chat_flow.js"' in chat_js
    assert "applyEvent(state, event, { now: performance.now() })" in chat_js
    assert "function showStep(" in chat_js
    for stage in ("scope", "evidence", "method", "verify", "propose"):
        assert f'id: "{stage}"' in chat_flow_js
        assert f"{stage}:" in chat_js, f"{stage} has no human-readable step text"
    assert ".chat-step" in chat_css and ".chat-tool-card" in chat_css
    assert 'class="chat-workflow"' not in chat_js, "the permanent rail is gone"
    # the final repaint must retire the live line, whatever the last block was:
    # a run that ended on a tool card left it pulsing forever
    assert "if (streaming) showStep(view, state);" in chat_js
    assert "else view.step.hidden = true;" in chat_js


def test_a_proposal_card_shows_its_provenance(chat_js: str, chat_css: str) -> None:
    assert "proposal.method" in chat_js
    assert "proposal.source" in chat_js
    assert "provenance marker missing" in chat_js
    assert ".chat-proposal-facts" in chat_css


def test_standing_instructions_reach_the_agent_not_the_message(chat_js: str) -> None:
    assert "additional_instructions: el(\"system\").value.trim() || undefined" in chat_js
    assert 'data-act="instructions"' not in chat_js
    overview = chat_js.split("function wsOverview(", 1)[1].split(
        "function wsPipeline", 1
    )[0]
    # the editor is one of the folds at the foot of the agent page
    assert '"Instructions",' in overview
    assert "wsInstructions(inner)" in overview
    assert 'area.id = "chat-ws-instructions"' in chat_js


def test_the_workspace_explains_the_agent_instead_of_the_transcript(
    chat_js: str, chat_css: str
) -> None:
    """"What is this thing and what can it reach" has its own panel now."""
    assert 'api(`/api/agents/workspace?' in chat_js
    assert "workspaceModel(payload)" in chat_js
    for name in ("wsOverview", "wsPipeline", "wsCapabilities", "wsTools"):
        assert f"function {name}(" in chat_js, name
    pipeline = chat_js.split("function wsPipeline(", 1)[1].split(
        "function wsCapabilities", 1
    )[0]
    capabilities = chat_js.split("function wsCapabilities(", 1)[1].split(
        "function schemaType", 1
    )[0]
    tools = chat_js.split("function wsTools(", 1)[1].split("function contentRows", 1)[0]
    assert 'wsNode("details", "chat-ws-step"' in pipeline
    assert 'wsNode("details", "chat-ws-disclosure"' in capabilities
    assert 'wsNode("details", "chat-ws-tool"' in chat_js.split(
        "function wsToolRow(", 1
    )[1].split("function wsTools", 1)[0]
    assert "wsToolArguments(tool)" in chat_js
    # a long tool list is filterable and fills each row only when it is opened
    assert "toolSearch = query.value" in tools
    assert 'query.setAttribute("aria-label", "Filter tools by name or description")' in tools
    assert "tool.input_schema" in chat_js
    assert ".chat-ws-pipeline-detail" in chat_css
    assert ".chat-ws-disclosure > summary" in chat_css
    assert ".chat-ws-tool > summary" in chat_css
    examples = _css_rule_with(chat_css, ".chat-ws-examples", "grid-template-columns: 1fr")
    assert "grid-template-columns: 1fr" in examples
    # plain chat is an assistant like any other, so its workspace opens too
    assert "if (!agent)" not in chat_js.split("async function loadWorkspace", 1)[1].split(
        "function openWorkspace", 1
    )[0]


def test_a_rejected_token_is_forgotten_and_explained_as_a_link_problem(
    script: str,
) -> None:
    """A bookmarked /viewer URL with no #t= reused a dead token forever."""
    assert "function forgetStaleToken()" in script
    assert script.count("forgetStaleToken();") == 2, "both the fetch 401 and the WS 4401"
    assert "const tokenFromLink = hashParams.has(\"t\")" in script
    assert "missing its #t= access token" in script
    assert "Viewer authorization expired" not in script


def test_entrance_animations_only_run_on_rendered_elements(chat_css: str) -> None:
    """Fill modes are safe when the selector itself requires the open state."""
    assert "animation:" in chat_css
    animated = [
        (selector, body)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", chat_css, re.S)
        if "animation:" in body and "animation: none" not in body
    ]
    assert animated
    for selector, body in animated:
        if " both" in body:
            assert "[open]" in selector, f"fill mode can strand a closed element: {selector}"
    assert "@media (prefers-reduced-motion: reduce)" in chat_css


def test_nothing_animates_layout(chat_css: str) -> None:
    """Animating width on a grid item fights the layout engine and jams."""
    assert "transition: width" not in chat_css
    assert "transition: height" not in chat_css


def test_the_sidebar_opens_only_when_asked(chat_js: str) -> None:
    """It opened on hover, then on focus, then on returning focus after a
    dialog closed. One explicit toggle, and nothing else moves it."""
    assert "pointerenter" not in chat_js
    assert "pointerleave" not in chat_js
    assert "focusin" not in chat_js
    assert 'data-act="pin-rail"' not in chat_js, "the pin was a second, hidden state"
    assert chat_js.count('data-act="toggle-side"') >= 1
    assert "setSide(!sideOpen, { trigger: actionButton })" in chat_js


def test_the_panel_cannot_widen_the_viewer_it_is_docked_in(chat_css: str) -> None:
    """The dock must never be able to push its host into a sideways scroll."""
    block = chat_css.split(".chat-root {", 1)[1].split("\n}", 1)[0]
    assert "overflow: hidden" in block
    assert "grid-template-columns" in block


def test_the_conversation_owns_its_grid_track_by_name(chat_css: str) -> None:
    """An absolutely positioned sidebar leaves the grid flow, so the
    conversation fell back into the sidebar's 52px track and collapsed."""
    assert ".chat-main { grid-column: 2; grid-row: 1; }" in chat_css
    assert ".chat-side { grid-column: 1; grid-row: 1; }" in chat_css
    root = chat_css.split(".chat-root {", 1)[1].split("\n}", 1)[0]
    assert "grid-template-columns: var(--side-track) minmax(0, 1fr)" in root
    assert "--ws-track" not in chat_css


def test_a_closed_panel_is_out_of_reach_not_merely_offscreen(chat_js: str) -> None:
    """A drawer slid off-screen still took Tab focus and still took clicks."""
    assert 'el("side").inert = !sideOpen' in chat_js
    assert 'el("workspace").inert = true' in chat_js
    assert "dialog.inert = false" in chat_js
    assert "dialog.inert = true" in chat_js
    assert "dialog.showModal()" in chat_js
    assert "dialog.close()" in chat_js


def test_closed_sidebar_and_workspace_do_not_resize_the_chat(
    chat_js: str, chat_css: str
) -> None:
    assert ".chat-root.side-inline:not(.side-open) > .chat-side" in chat_css
    assert ".chat-root > dialog.chat-workspace:not([open])" in chat_css
    closed = _css_rule_with(chat_css, "dialog.chat-workspace:not([open])", "display: none")
    assert "display: none" in closed
    assert "dialog.showModal()" in chat_js
    assert "grid-template-columns: var(--side-track) minmax(0, 1fr)" in chat_css


def test_docked_icons_and_touch_controls_keep_their_component_geometry(
    chat_js: str, chat_css: str
) -> None:
    # The panel CSP is style-src 'self', so an icon can only carry its size as
    # an attribute; a style attribute is dropped and every icon collapses to
    # the 16px default.
    assert 'data-size="${size}"' in chat_js
    icon_rule = chat_css.split(".chat-root button svg {", 1)[1].split("}", 1)[0]
    assert "width: 16px" in icon_rule
    for size in (12, 13, 14, 15):
        assert f'.chat-root svg[data-size="{size}"]' in chat_css, size
    coarse = chat_css.split("@media (pointer: coarse)", 1)[1].split("}", 1)[0]
    assert ".chat-root button" in coarse
    assert "min-height: 44px" in coarse


def test_the_panel_emits_no_inline_style_attribute(chat_js: str) -> None:
    """The CSP blocks them, so one only shows up as a silent visual fallback."""
    assert 'style="' not in chat_js
    assert ".setAttribute(\"style\"" not in chat_js


def test_chat_layout_responds_to_its_container_not_only_the_viewport(
    chat_js: str, chat_css: str
) -> None:
    """A narrow dock can live inside a wide browser, so viewport breakpoints
    alone squeeze the conversation between a rail and workspace."""
    assert "new ResizeObserver(syncShellLayout)" in chat_js
    assert "const mainWidth = width" in chat_js
    assert 'root.classList.toggle("chat-compact", width < COMPACT_WIDTH || mainWidth < COMPACT_MAIN_WIDTH)' in chat_js
    assert 'root.classList.toggle("side-inline", inlineSide)' in chat_js
    assert '<dialog class="chat-workspace"' in chat_js
    assert "dialog.showModal()" in chat_js
    assert "inspector-nonmodal" not in chat_js
    assert ".chat-root.chat-compact" in chat_css
    assert ".chat-root.chat-compact > dialog.chat-workspace" in chat_css
    assert ".chat-root:not(.side-inline) .chat-side" in chat_css


def test_compact_sidebar_and_workspace_have_native_dismissal_layers(
    chat_js: str, chat_css: str
) -> None:
    assert 'class="chat-shell-scrim"' in chat_js
    assert 'action === "close-overlays"' in chat_js
    assert ".chat-shell-scrim" in chat_css
    assert "sideOpen && !sideIsInline()" in chat_js
    assert "dialog.chat-workspace::backdrop" in chat_css
    assert 'el("workspace").addEventListener("cancel"' in chat_js
    assert "event.preventDefault()" in chat_js
    assert "closeWorkspace()" in chat_js


def test_overlays_are_anchored_without_changing_the_conversation_track(chat_css: str) -> None:
    side = chat_css.split(".chat-root:not(.side-inline) .chat-side {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "grid-area: 1 / 1 / -1 / -1" in side
    assert "width: min(var(--side-width), 86%)" in side
    workspace = _css_rule_with(chat_css, "dialog.chat-workspace", "position: fixed")
    assert "position: fixed" in workspace
    assert re.search(r"inset:\s*0\s*;", workspace)
    assert "margin: auto" in workspace


def test_the_sidebar_heading_does_not_repeat_a_group_label(chat_js: str) -> None:
    """It said "Conversations" above the assistants list, and again above the
    conversations list."""
    top = chat_js.split('<div class="chat-side-top">', 1)[1].split("</div>", 1)[0]
    assert ">Conversations<" not in top
    assert 'data-role="side-scope"' in top
    # the two lists are both scoped to the open model, so name that instead
    assert 'scope.textContent = open || "No model"' in chat_js


def test_mode_and_autonomy_are_two_independent_controls(chat_js: str, chat_css: str) -> None:
    """What the assistant may touch and whether it asks first are different
    questions; one three-way control could not express Ask + Auto."""
    template = _template(chat_js)
    assert 'data-role="session-mode"' in template
    assert 'data-role="session-autonomy"' in template
    modes = chat_js.split('data-role="session-mode"', 1)[1].split("</select>", 1)[0]
    assert '"ask"' in modes and '"edit"' in modes and '"auto"' not in modes
    autonomy = chat_js.split('data-role="session-autonomy"', 1)[1].split("</select>", 1)[0]
    assert '"approval"' in autonomy and '"auto"' in autonomy
    assert "async function changeSessionAutonomy(" in chat_js
    assert ".chat-autonomy-select:has(select.auto)" in chat_css


def test_only_a_person_can_write_the_ifc_file(chat_js: str) -> None:
    """No stance grants an assistant the file: it works in memory and a human
    decides that the work is finished."""
    assert 'data-act="save-model"' in chat_js
    assert "async function saveModelFile()" in chat_js
    save = chat_js.split("async function saveModelFile()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert '"/api/session/save"' in save
    # the control only exists when there is something to decide about
    assert "save.hidden = !sessionStatus.dirty;" in chat_js


def test_an_approval_stops_the_run_and_offers_two_answers(
    chat_js: str, chat_css: str
) -> None:
    card = chat_js.split("function approvalNode()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "chat-approval-allow" in card and "chat-approval-deny" in card
    decide = chat_js.split("async function decideApproval(", 1)[1].split(
        SPLIT_BLOCK_END, 1
    )[0]
    assert '"/api/agents/approve"' in decide
    assert "request_id: block.requestId" in decide
    # the run is blocked, so nothing else will arrive to trigger a repaint
    assert 'event.type === "approval" ||' in chat_js
    assert 'event.type === "approval_decided"' in chat_js
    assert "Waiting for your approval" in chat_js
    assert ".chat-approval.waiting" in chat_css


def test_approval_cards_are_compact_explicit_and_bound_long_code(
    chat_js: str, chat_css: str
) -> None:
    """Completed decisions become one-row audit entries, while a waiting card
    names the one-call and conversation-long choices. Long code owns both
    scroll axes instead of widening or stretching the transcript."""
    card = chat_js.split("function approvalNode()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert 'document.createElement("details")' in card
    assert "Approve once" in card
    assert "Always allow this tool" in card
    assert 'pre tabindex="0"' in card
    paint = chat_js.split("function paintApproval(", 1)[1].split(
        SPLIT_BLOCK_END, 1
    )[0]
    assert 'node.open = block.state === "waiting"' in paint
    assert "approvalArgumentPreview(block)" in paint
    assert "approvalAllowlist.set(block.name" in paint
    rule = _css_rule_with(chat_css, ".chat-approval-args pre", "overflow-y: auto")
    assert "max-height:" in rule
    assert "overflow-x: auto" in rule
    code = _css_rule_with(chat_css, ".chat-approval-args code", "white-space: pre")
    assert "width: max-content" in code


def test_tool_inputs_and_results_stack_as_full_width_rows(
    chat_js: str, chat_css: str
) -> None:
    body = _css_rule_with(chat_css, ".chat-tool-body", "grid-template-columns")
    assert "minmax(0, 1fr)" in body
    assert "0.72fr" not in body and "1.28fr" not in body
    paint = chat_js.split("function paintToolBody(", 1)[1].split(
        SPLIT_BLOCK_END, 1
    )[0]
    assert "approvalArgumentPreview(block)" in paint
    source = _css_rule_with(chat_css, ".chat-tool-part.code code", "white-space: pre")
    assert "width: max-content" in source
    answer = _css_rule_with(chat_css, ".chat-answer pre", "overflow: auto")
    assert "max-height:" in answer


def test_the_agent_page_leads_with_what_it_is_and_may_do(chat_js: str) -> None:
    """Starter prompts, the stage map and the instruction editor are things
    you go and open, so they are identical folds at the foot of the page."""
    overview = chat_js.split("function wsOverview(", 1)[1].split(
        "function wsPipeline", 1
    )[0]
    assert "function wsFold(" in chat_js
    for title in ('"Suggested questions"', '"Instructions"'):
        assert title in overview, title
    # the standing policy card is gone; the guarantee moved onto the mark
    assert "chat-ws-policy" not in overview
    assert "with a provenance record, and never on disk" in chat_js


def test_agent_page_folds_keep_long_content_scrollable(
    chat_js: str, chat_css: str
) -> None:
    """Opening a long workflow or instruction must not push its content
    behind the fixed workspace footer, and keyboard users need the same scroll
    surface as wheel and trackpad users."""
    fold = chat_js.split("function wsFold(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "chat-ws-fold-body" in fold
    assert "inner.tabIndex = 0" in fold
    assert 'inner.setAttribute("role", "region")' in fold
    rule = _css_rule_with(chat_css, ".chat-ws-fold-body", "overflow-y: auto")
    assert "max-height:" in rule
    assert "overflow-y: auto" in rule
    assert "overscroll-behavior-y: contain" in rule


def test_the_workspace_sheet_closes_on_a_backdrop_click(chat_js: str) -> None:
    """A centred modal that only closes from its own X feels stuck."""
    handler = chat_js.split('el("workspace").addEventListener("mousedown"', 1)[1].split(
        "});", 1
    )[0]
    assert "event.target !== el(\"workspace\")" in handler
    # coordinates, not the event target: a native select popup paints over the
    # sheet and its click would otherwise read as an outside hit
    assert "getBoundingClientRect()" in handler
    assert "event.clientX < box.left" in handler
    # an unsaved Agent setup draft is not dismissed by a stray click
    assert 'workspaceView === "builder"' in handler
    assert "closeWorkspace();" in handler


def test_programmatic_focus_does_not_wear_the_keyboard_ring(
    chat_js: str, chat_css: str
) -> None:
    """Opening a surface with the mouse used to outline the control focus
    landed on, because :focus-visible cannot see how focus arrived."""
    helper = chat_js.split("function focusQuietly(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "pointerInput" in helper
    assert 'node.dataset.quietFocus = "1"' in helper
    assert "delete node.dataset.quietFocus" in helper
    assert 'root.addEventListener("pointerdown", () => { pointerInput = true; }, true);' in chat_js
    assert '.chat-root [data-quiet-focus]:focus-visible { outline: none; }' in chat_css
    # the ring itself is untouched for people who navigate by keyboard
    assert ".chat-root :focus-visible { outline: 2px solid var(--chat-accent)" in chat_css


def test_a_turn_is_marked_by_an_icon_not_a_repeated_label(chat_js: str) -> None:
    """Every turn said the same name and the same role word."""
    assert "IFC workbench" not in chat_js
    assert "<small>Request</small>" not in chat_js
    for role in ('aria-label="You"', 'role="img"'):
        assert role in chat_js
    assert "I.user" in chat_js
    user = chat_js.split('head.className = "chat-turn-head user"', 1)[1].split(
        "const bubble", 1
    )[0]
    assert "chat-turn-avatar" in user and "${I.user}" in user


def test_the_content_view_paints_from_the_workspace_payload(chat_js: str) -> None:
    """Both endpoints return the same shape, so the second fetch was a wait
    for something the panel already had."""
    seed = chat_js.split("function seedContentFromWorkspace()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "workspace.content" in seed
    assert "contentLibraryAgent = agent" in seed
    assert "if (!force && seedContentFromWorkspace())" in chat_js


def test_project_content_supports_bulk_and_range_selection(chat_js: str) -> None:
    """Granting twenty manuals one click at a time is twenty round trips."""
    content = chat_js.split("function renderContentWorkspace()", 1)[1].split(
        "function wsInstructions", 1
    )[0]
    assert "Select shown" in content and "Clear shown" in content
    assert "chat-content-tally" in content
    # bulk acts on what the filter is showing, not on the whole library
    assert "for (const file of shown)" in content
    assert "const extendRange = (path, wasChecked)" in content
    assert "if (event.shiftKey && onRange?.(path, box.checked)) event.preventDefault();" in chat_js


def test_the_viewer_exposes_selection_commands_to_the_panel(script: str) -> None:
    """An answer that names elements should be able to show them."""
    for command in ("set-selection", "clear-selection", "focus-selection"):
        assert f'"{command}"' in script, command
        assert f'command.action === "{command}"' in script, command
    handler = script.split('command.action === "set-selection"', 1)[1].split(
        'command.action === "capture-evidence"', 1
    )[0]
    # the panel speaks GlobalIds; only the viewer knows this scene's express ids
    assert "expressOf.get(guid)" in handler
    assert "None of those elements are in this model" in handler


def test_guids_in_answers_open_frame_and_isolate_their_ifc(
    script: str, chat_js: str
) -> None:
    select = chat_js.split("const selectInViewer", 1)[1].split("// GlobalId chips", 1)[0]
    assert 'action: isolate ? "isolate-guids" : "reveal-guids"' in select
    assert "model_id: modelId" in select
    shortcut = chat_js.split('document.addEventListener("keydown", (event) => {', 1)[1].split(
        "});", 1
    )[0]
    assert 'event.key.toLowerCase() !== "i"' in shortcut
    assert "globalIdsIn(textSelection.toString())" in shortcut
    panel_scope = "if (!root.contains(target) && !selectedText.length) return;"
    assert panel_scope in shortcut
    assert shortcut.index(panel_scope) < shortcut.index("event.preventDefault();")
    assert "selectInViewer(guids, { isolate: true" in shortcut

    resolver = script.split("async function modelContainingGuid", 1)[1].split(
        "function applyGuidCommand", 1
    )[0]
    assert "expressOf.has(guid)" in resolver
    assert "parsedModelCache.get(row.id)" in resolver
    assert 'api(`/api/elements/${encodeURIComponent(guid)}${query}`)' in resolver
    reveal = script.split("async function revealGuidCommand", 1)[1].split(
        "/**\n * Run one viewer command", 1
    )[0]
    assert "selectViewerModel(modelId)" in reveal
    assert "pendingGuidCommand" in reveal
    apply = script.split("function applyGuidCommand", 1)[1].split(
        "function applyPendingGuidCommand", 1
    )[0]
    assert "Number.isFinite(elements.get(id)?.box?.[0])" in apply
    assert "setSelection(ids, false)" in apply
    assert "focusSelection(true)" in apply
    assert "fitTo(ids)" in apply
    pending = script.split("function applyPendingGuidCommand", 1)[1].split(
        "async function revealGuidCommand", 1
    )[0]
    assert "showOverlay(" not in pending
    assert "sendViewerResult(command, false" in pending
    rebuild = script.split("async function buildScene", 1)[1].split(
        "// ---------------------------------------------------------------- spatial tree", 1
    )[0]
    assert "applyPendingGuidCommand(targetModelId, { reportFailure: true })" in rebuild


def test_the_agent_and_the_buttons_mean_the_same_thing_by_visibility(
    script: str,
) -> None:
    """isElementShown gates on four sets. The command cleared two of them and
    returned {isolated: 0} while one element was still alone on screen."""
    body = script.split("function showEverything(", 1)[1].split(chr(10) + "}", 1)[0]
    for released in (
        "userIsolateSet = null;",
        "isolateSet = null;",
        "hiddenManual.clear();",
        "hiddenByTree.clear();",
        "box.checked = true;",
    ):
        assert released in body, released
    # one body, reached from the button and from the command
    assert '$("tool-show-all").addEventListener("click", showEverything);' in script
    show_all = script.split('command.action === "show-all"', 1)[1].split(
        'command.action === "hide"', 1
    )[0]
    assert "showEverything();" in show_all
    # the result is read off the viewer, not written into the answer
    assert "isolated: userIsolateSet ? userIsolateSet.size : 0," in show_all
    assert "hidden: hiddenCount," in show_all
    assert "result = { isolated: 0 };" not in script

    # fitTo works off the boxes it is handed whether or not they are on screen,
    # so selecting or isolating a hidden element aimed the camera at nothing
    unhide = script.split("function unhide(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "ids.filter((id) => !isElementShown(id))" in unhide
    for gate in (
        "hiddenManual.delete(id);",
        "hiddenByTree.delete(id);",
        "if (isolateSet) isolateSet.add(id);",
        "if (userIsolateSet) userIsolateSet.add(id);",
    ):
        assert gate in unhide, gate
    select = script.split('command.action === "set-selection"', 1)[1].split(
        'command.action === "clear-selection"', 1
    )[0]
    assert "const unhidden = wantsFit ? unhide(ids) : 0;" in select
    assert "unhidden }" in select  # reported, never a silent repair
    # an empty list means select nothing, which additive turned into a no-op
    assert "setSelection(ids, ids.length > 0 && command.additive === true);" in select
    isolate = script.split('command.action === "isolate"', 1)[1].split(
        'command.action === "show-all"', 1
    )[0]
    assert "const unhidden = unhide(ids);" in isolate


def test_the_viewer_says_which_model_it_speaks_for_and_when_it_is_rebuilding(
    script: str,
) -> None:
    """Two halves of one wire contract. The hub kept a single measurements list
    and adopted the newest frame, so a second tab publishing its empty list on
    load erased the first tab's dimensions; and commands ran against the scene
    buildScene had already disposed, reporting every GlobalId as unknown."""
    send = script.split("function sendMeasurements()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const row = currentModelRow();" in send
    assert "model_id: row ? row.id : null," in send

    state = script.split("function sendSceneState(state)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "viewerDocumentOpen ? currentModelRow() : null" in state
    assert 'wsSend({ type: "scene_state", state, model_id: row?.id ?? null });' in state
    # a socket failure must never be what breaks a build
    assert "try {" in state and "} catch" in state

    build = script.split("async function buildScene(", 1)[1].split(chr(10) + "}", 1)[0]
    assert 'sendSceneState("rebuilding");' in build
    assert 'sendSceneState("ready");' in build
    assert build.index('sendSceneState("rebuilding")') < build.index("disposeModel();")
    assert build.index("sendSelection();") < build.index('sendSceneState("ready")')

    load = script.split("async function loadModel()", 1)[1].split(chr(10) + "}", 1)[0]
    # a queued reload means another rebuild follows; a 304 or an error means
    # none is coming and the hub must stop holding commands
    assert load.index("reloadQueued = true;") < load.index('sendSceneState("rebuilding");')
    assert 'else if (sceneState !== "ready") {' in load
    connect = script.split("function connect()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "sendSceneState(sceneState);" in connect


def test_hidden_and_isolated_state_is_visible_outside_the_popover(
    html: str, script: str, styles: str
) -> None:
    """The only indicator lived in the View tools popover, so an agent or a
    search Isolate could take two thirds of the model away with nothing on
    screen to say so and no visible way back."""
    for name in ("vis-info", "vis-info-text", "vis-show-all", "vis-clear-section"):
        assert f'id="{name}"' in html, name
    assert "#vis-info-text:not(:empty)::before" in styles
    info = script.split("function updateVisibilityInfo()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const isolated = userIsolateSet || isolateSet;" in info
    assert "`isolated to ${isolated.size}`" in info
    # a ghost is on screen, so it is counted as ghosted rather than reported a
    # second time as hidden
    assert "const gone = hiddenCount - ghostCount;" in info
    assert "${gone} of ${elements.size} hidden" in info
    assert "`${ghostCount} ghosted`" in info
    # the section and the projection are view states too, whoever set them
    assert "sectionState().axes" in info
    assert 'parts.push("orthographic")' in info
    assert '$("vis-info-text").textContent = parts.join(" · ");' in info
    # and the way back sits beside the state, not inside a closed popover
    assert '$("vis-show-all").addEventListener("click", showEverything);' in script
    assert '$("vis-clear-section").addEventListener("click", clearSections);' in script
    assert '$("tool-section-clear").addEventListener("click", clearSections);' in script
    # a state that only changes through these two has to be re-read from them
    clip = script.split("function updateClipping()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "updateVisibilityInfo();" in clip
    projection = script.split("function setProjection(kind)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "updateVisibilityInfo();" in projection

    # User isolation is immediate and does not create a second tab history row.
    assert 'id="tool-focus-sel"' in html
    focus = script.split("function focusSelection(fit)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "userIsolateSet = new Set(selection);" in focus
    assert '$("tool-isolate").addEventListener("click", () => focusSelection(false));' in script
    assert '$("tool-focus-sel").addEventListener("click", () => focusSelection(true));' in script
    assert '$("tool-focus-sel").disabled = none;' in script
    search = script.split("function renderSearch(payload)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "userIsolateSet = new Set(targets);" in search
    assert "if (e.shiftKey) focusSelection(true);" in script


def test_the_selection_is_context_not_a_control_in_the_rail(chat_js: str) -> None:
    """It is evidence going to the tools, so it sits with the attachments and
    not among the model and mode selectors."""
    tray = chat_js.split("function renderAttachments()", 1)[1].split(
        SPLIT_BLOCK_END, 1
    )[0]
    assert 'chat-attachment-chip selection' in tray
    assert 'data-act="drop-selection"' in tray
    assert '"focus-selection"' in tray
    # nothing is drawn when there is no selection: the whole model is always
    # available, so saying so on every turn was noise dressed as state
    assert "Whole model" not in chat_js
    assert 'data-role="selection-context"' not in chat_js
    assert 'action === "drop-selection"' in chat_js


def test_one_plus_control_gathers_message_context(chat_js: str, chat_css: str) -> None:
    """The paperclip and the camera used to sit in the rail beside standing
    configuration, which mixed one-off context in with settings."""
    options = chat_js.split("function plusOptions()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    for label in ("Attach a file", "Attach the current 3D view", "Mention project content"):
        assert label in options, label
    assert 'data-act="plus"' in chat_js
    assert 'aria-haspopup="menu"' in chat_js
    assert ".chat-plus-menu" in chat_css
    # opening it is a menu, so it closes on Escape and on any outside click
    assert 'if (!event.target.closest(".chat-plus-menu, .chat-plus")) closePlusMenu();' in chat_js
    assert 'else if (!el("plus-menu").hidden) closePlusMenu({ restoreFocus: true });' in chat_js


def test_at_mentions_and_slash_commands_share_one_popup(chat_js: str) -> None:
    token = chat_js.split("function activeToken()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    # a mention is anywhere after whitespace; a command only at the very start
    assert "(^|\\s)@([^\\s@]*)$" in token
    assert "^\\/([a-z-]*)$" in token
    assert "SLASH_COMMANDS" in chat_js
    for command in ("agent", "model", "content", "tools", "new", "export", "ask", "edit"):
        assert f'name: "{command}"' in chat_js, command
    # accepting a mention both names the file and grants it to this message
    apply = chat_js.split("function applySuggestion(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "pendingAttachments.push" in apply
    assert "input.setRangeText" in apply


def test_a_workflow_is_context_the_conversation_stands_on(
    chat_js: str, chat_css: str, chat_ai_sdk_js: str, chat_history_js: str
) -> None:
    """`/` lists the library first; choosing one attaches it as a chip whose
    hover preview is the exact text sent, Run starts it, and every later turn
    names it again so the console keeps the same thread."""
    commands = chat_js.split("function slashCommands()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert 'group: "Workflows"' in commands
    assert "run: () => attachWorkflow(flow.name)" in commands
    assert "...workflows," in commands
    assert ".chat-suggest-group" in chat_css
    attach = chat_js.split("function attachWorkflow(name", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    # A workflow joins a fresh conversation and the assistant it names.
    assert "if (wanted && wanted !== currentAgent) switchAgent(wanted);" in attach
    assert "else if (turns.length || busy) startConversation(true, { focus: false });" in attach
    tray = chat_js.split("function renderAttachments()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "workflowChip(conversationWorkflow, { context: workflowContext })" in tray
    preview = chat_js.split("function workflowPreviewNode(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    # The preview is content, never markup.
    assert "innerHTML" not in preview
    assert "pre.textContent = context?.instructions" in preview
    assert ".chat-chip-preview" in chat_css
    assert ".chat-attachment-chip.workflow:hover .chat-chip-preview" in chat_css
    assert ".chat-attachment-chip.workflow.open .chat-chip-preview" in chat_css
    for action in ("attach-workflow", "drop-workflow", "preview-workflow", "run-workflow"):
        assert f'action === "{action}"' in chat_js, action
    # Every turn of the conversation carries the workflow by name.
    run = chat_js.split("async function run(", 1)[1].split("async function submit()", 1)[0]
    assert "workflow: conversationWorkflow?.name || undefined" in run
    assert 'event.type === "workflow_context"' in run
    assert "body.workflow = workflow;" in chat_ai_sdk_js
    assert "function cleanWorkflow(value)" in chat_history_js
    record = chat_js.split("function conversationRecord()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "workflow: conversationWorkflow" in record
    restore = chat_js.split("function selectHistory(record)", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "conversationWorkflow = record.workflow ? resolveWorkflow(record.workflow) : null;" in restore
    # An empty composer still sends when a workflow waits to start.
    submit = chat_js.split("async function submit()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "const runLabel = flow && !turns.length ? `Run ${flow.title}` : \"\";" in submit
    assert "composerIntent({ busy, text: text || runLabel })" in submit
    assert "turn.prompt = text;" in submit


def test_the_panel_watches_memory_and_can_give_it_back(
    chat_js: str, chat_css: str, chat_memory_js: str
) -> None:
    template = _template(chat_js)
    assert 'data-role="memory" data-act="memory"' in template
    assert 'data-level="ok"' in template
    assert ".chat-memory[data-level=\"critical\"]" in chat_css
    snapshot = chat_js.split("function memorySnapshot()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    for source in ("heap: sampleHeap()", "server: sessionStatus.memory", "sessionStatus.viewer_memory", "turns,"):
        assert source in snapshot, source
    # Automatic relief is rate limited; a press is not.
    tick = chat_js.split("function memoryTick()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "Date.now() - memoryRelievedAt > 60_000" in tick
    relief = chat_js.split("function relieveMemory(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert 'action: "release-memory"' in relief
    assert "trimTranscriptMemory(plan.keepTurns)" in relief
    trim = chat_js.split("function trimTranscriptMemory(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "block.output = null;" in trim
    assert "export function reliefPlan(" in chat_memory_js
    # The console's reading rides on /api/status; the viewer's on its context.
    assert 'viewer_memory: viewerOpen' in chat_js
    assert "sessionStatus = { ...nextStatus, viewer_memory: sessionStatus.viewer_memory || null };" in chat_js


@pytest.fixture(scope="module")
def chat_ai_sdk_js() -> str:
    return _agent_asset("chat_ai_sdk.js")


@pytest.fixture(scope="module")
def chat_memory_js() -> str:
    return _agent_asset("chat_memory.js")


def test_the_pipeline_belongs_to_the_agent_that_has_it(chat_js: str) -> None:
    """Reachable stages follow from the blocks an agent holds, so a separate
    Pipeline page described no agent in particular."""
    assert 'data-workspace-view="pipeline"' not in chat_js
    assert "pipeline: wsPipeline" not in chat_js
    assert 'if (view === "pipeline") view = "agent";' in chat_js
    overview = chat_js.split("function wsOverview(", 1)[1].split(
        "function wsPipeline(", 1
    )[0]
    assert "wsPipeline(body)" in overview
    assert 'const detailViews = ["agent", "capabilities", "tools", "skills"];' in chat_js


def test_no_css_escape_is_double_escaped(chat_css: str) -> None:
    r"""`content: "\\203A"` printed the six characters instead of a chevron."""
    assert "\\\\2" not in chat_css


def test_the_camera_is_readable_and_settable_in_the_models_own_axes(
    script: str,
) -> None:
    """An agent that can read the camera and set it can compose a plan view, an
    elevation and a walkthrough. Before this it could only fit as a side effect
    of selecting something or of taking a screenshot."""
    for action in ("set-camera", "fit"):
        assert f'command.action === "{action}"' in script, action
        assert f'"{action}",' in script, action
    state = script.split("function cameraState()", 1)[1].split(
        "/** The scene-space pose", 1
    )[0]
    for field in (
        "position:",
        "target:",
        "up:",
        "fov:",
        "projection:",
        "ortho_height:",
        "distance:",
        "world_per_pixel:",
    ):
        assert field in state, field
    # coordinates leaving the viewer are the model's, never the viewport's
    assert "toModelPoint(camera.position)" in state
    assert "toModelPoint(controls.target)" in state
    assert "toModelDirection(up)" in state
    # up comes off the matrix, so it round-trips through lookAt whatever the
    # orbit controls did to the roll
    assert "setFromMatrixColumn(camera.matrixWorld, 1)" in state
    assert "fov: ortho ? null : camera.fov" in state

    camera = script.split("function applyCameraCommand(command)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "toScenePoint(modelTriple(command.position" in camera
    assert "toScenePoint(modelTriple(command.target" in camera
    assert "toSceneDirection(modelTriple(command.up" in camera
    # position and target a millimetre apart, or there is no view to describe
    assert "position.distanceTo(target) < CAMERA_MIN_REACH" in camera
    assert "CAMERA_MIN_REACH = 1e-3" in script
    assert "applyNearFar();" in camera
    assert "command.transition !== false" in camera
    # a projection swap moves each eye by its own rule, so there is no pose to
    # interpolate between them
    assert "wasOrtho === isOrtho()" in camera

    fit = script.split("function fitCommand(command)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "command.selection === true" in fit
    assert "frameBox(" in fit
    assert "command.padding" in fit
    assert "VIEW_DIRECTIONS[view]" in fit
    assert "return { framed, hidden, missing, camera: cameraState() };" in fit
    # fitting frames; it does not select
    assert "setSelection" not in fit

    context = script.split("function viewerContext(", 1)[1].split(
        "function scheduleViewerContext", 1
    )[0]
    assert "camera: cameraState()," in context
    assert "viewport: { width: viewportWidth, height: viewportHeight }," in context
    visible = context.split("visibility: {", 1)[1].split("}", 1)[0]
    for field in ("hidden:", "isolated:", "ghosted:", "total:"):
        assert field in visible, field


def test_a_camera_transition_yields_to_the_hand_on_the_mouse(script: str) -> None:
    """Easing that keeps writing the pose while the controls damp a drag is two
    things fighting over one camera."""
    start = script.split('controls.addEventListener("start"', 1)[1].split("});", 1)[0]
    assert "cameraTween = null;" in start
    # the glide runs before the controls read the pose, or update() answers
    # with the previous frame's
    loop = script.split("function renderFrame(now)", 1)[1]
    assert loop.index("stepCameraTween(now)") < loop.index("controls.update()")
    assert "renderer.setAnimationLoop" not in script
    step = script.split("function stepCameraTween(now)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "t * t * (3 - 2 * t)" in step
    # zoom is a ratio, so it is interpolated as one
    assert "Math.exp(mix(Math.log(from.zoom), Math.log(to.zoom)))" in step
    assert "if (t >= 1) cameraTween = null;" in step
    begin = script.split("function beginCameraTransition(from)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "if (motionPreference.matches) return;" in begin
    # a gesture pushes the new camera once it ends, not on every frame of it
    ended = script.split('controls.addEventListener("end"', 1)[1].split("});", 1)[0]
    assert 'scheduleViewerContext("camera")' in ended


def test_isolation_ghosts_the_context_instead_of_deleting_it(
    html: str, script: str
) -> None:
    """Isolating a duct used to remove the building around it, which leaves the
    duct floating with nothing to place it against."""
    assert "GHOST_LEVEL = 64" in script
    body = script.split("function applyVisibility()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "level = GHOST_LEVEL;" in body
    assert 'scheduleViewerContext("visibility")' in body
    # Isolation context and non-selected context may fade. Something the user
    # or the tree deliberately hid still goes away.
    ghosted = script.split("function isGhosted(id)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "isolatedOut(id) || selectionContext" in ghosted
    assert "!hiddenByTree.has(id) && !hiddenManual.has(id)" in ghosted
    # the draw material keeps the middle value and dithers it
    assert "if (ifcState.r < 0.05) discard;" in script
    assert "float ifcGhost = step(ifcState.r, 0.5);" in script
    assert "ifcOrdered(gl_FragCoord.xy) > uGhostFill" in script
    # Faded geometry stays selectable and measurable, while truly hidden
    # geometry (state zero) still cannot answer either pass.
    for name in ("pickMaterial", "depthMaterial"):
        material = script.split(f"const {name} = new THREE.ShaderMaterial(", 1)[1]
        assert (
            "if (texture2D(uStateTex, uv).r < 0.05) discard;" in material.split("});", 1)[0]
        ), name
    assert "return isElementShown(id) || isGhosted(id);" in script
    # It is an explicit display mode; the default is ordinary highlight-only.
    assert "let ghostContext = false;" in script
    assert "ghostContext = uiState.ghost === true;" in script
    # context recedes towards whatever the canvas is, in either theme
    assert "uGhostTint" in script
    assert "ghostTint.set(colors.canvas);" in script
    assert 'id="tool-ghost"' in html
    assert "command.ghost !== undefined" in script


def test_silhouette_edges_ride_along_with_the_surface_they_came_from(
    html: str, script: str
) -> None:
    """Untextured IFC with no edges is unreadable: two walls of one colour that
    meet are one blob."""
    assert "new THREE.LineBasicMaterial(" in script
    assert "{ depthBias: EDGE_DEPTH_BIAS }" in script
    # extraction is per unique shape and cached, so a door type placed four
    # hundred times pays for it once and an instanced one never pays at all
    lister = script.split("function edgeListFor(geom)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "if (geom.edges !== undefined) return geom.edges;" in lister
    assert "new THREE.EdgesGeometry(source, EDGE_ANGLE)" in lister
    # the lines carry the element index, so hiding, clipping, ghosting and
    # tinting all reach them through the one patched material
    finalize = script.split("function finalizeEdges(acc)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert 'geo.setAttribute("aElementIndex", idx);' in finalize
    assert "new THREE.LineSegments(geo, edgeMaterial)" in finalize
    # a line sits exactly on the triangle edge it came from, so without a nudge
    # towards the eye the depth test is a coin toss and the outline shimmers
    assert "gl_Position.z -= ${depthBias.toFixed(6)} * gl_Position.w;" in script
    # and it must never answer a pick or a depth probe in the surface's place
    assert "edgesWereVisible: edgeRoot.visible," in script
    assert "edgeRoot.visible = edgesWereVisible;" in script
    assert "edgeRoot.visible = state.edgesWereVisible;" in script
    # dropped while the buffer is scaled down: thin lines alias worst there
    visibility = script.split("function syncEdgeVisibility()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "edgesOn() && !(interacting && resScale < 1)" in visibility
    # the bake is gated on the model, not on the switch: an outline is staged
    # with its surface, so a switch flipped later could not build one
    assert "if (edgesAffordable) bakeEdges(" in script
    assert "edgesAffordable = placed.verts <= EDGE_VERTEX_BUDGET;" in script
    assert 'id="set-edges"' in html


def test_lengths_are_shown_in_the_unit_the_file_was_drawn_in(
    html: str, script: str, measure_math: str
) -> None:
    """The viewer printed metres whatever the file said, so a millimetre-drawn
    wall read 0.200 m here and 200 MILLIMETRE from the tools."""
    assert 'id="measure-unit"' in html
    assert 'id="measure-decimals"' in html
    for option in ("file", "mm", "cm", "m", "ft"):
        assert f'<option value="{option}"' in html, option
    # the arithmetic stays pure: the choice lives in app.js and is passed in
    assert "lengthUnitChoice" not in measure_math
    assert "export function unitForFile(units)" in measure_math
    assert "export function formatFeetInches(metres, denominator)" in measure_math
    # one wrapper per formatter, so the call sites did not have to move
    for wrapper, inner in (
        (
            "formatLength(metres)",
            "formatLengthIn(metres, activeLengthUnit(), activeDecimals())",
        ),
        ("formatArea(squareMetres)", "formatAreaIn(squareMetres, activeLengthUnit())"),
        (
            "formatVolume(cubicMetres)",
            "formatVolumeIn(cubicMetres, activeLengthUnit())",
        ),
    ):
        body = script.split(f"function {wrapper} {{", 1)[1].split(chr(10) + "}", 1)[0]
        assert inner in body, wrapper
    # the unit belongs to the model on screen, not to the console's active one
    assert "setFileUnits((row && row.units) || status.units || null);" in script
    assert "setFileUnits((currentModelRow() || {}).units || null);" in script
    # the slice field was metres whatever the file was drawn in, so 100 mm had
    # to be typed as 0.1
    assert "(Number(e.target.value) || 0) / perMetre()" in script
    slice_sync = script.split("function syncSliceInput()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "sliceDepth * unit.perMetre" in slice_sync
    assert 'id="section-depth-unit"' in html
    # the wire stays SI; the context says what the screen is labelled in
    context = script.split("function viewerContext(", 1)[1].split(
        "function scheduleViewerContext", 1
    )[0]
    assert "coordinated: coordinationApplied" in context
    units = context.split(chr(10) + "    units: {", 1)[1].split("},", 1)[0]
    for field in ("display:", "decimals:", "file_unit:", "to_si_factor:"):
        assert field in units, field


def test_measurement_card_is_compact_explicit_and_non_destructive(
    html: str, script: str, styles: str
) -> None:
    """The point workflow stays visible while the model is being clicked, and
    closing it must not look like or behave like deleting the saved ledger."""
    for element_id in (
        "tool-open-measure",
        "tool-measure",
        "tool-measure-path",
        "tool-measure-angle",
        "tool-measure-area",
        "measure-live",
        "measure-axis",
        "measure-finish",
        "measure-close",
        "measure-clear",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert 'data-measure-axis=""' in html
    for axis in "xyz":
        assert f'data-measure-axis="{axis}"' in html
    assert "function finishPath()" in script
    assert "function finishOpenMeasurement()" in script
    assert "controls.mouseButtons.LEFT = on ? null : ORBIT_LEFT_MOUSE" in script
    assert "controls.mouseButtons.RIGHT = on ? THREE.MOUSE.ROTATE" in script
    assert "controls.mouseButtons.MIDDLE = on ? THREE.MOUSE.PAN" in script
    assert "measurePressHit" in script and "measureDragActive" in script
    close = script.split('const measureClose = $("measure-close")', 1)[1].split(
        "updateToolButtons()", 1
    )[0]
    assert "setMeasurePanelOpen(false)" in close
    panel_close = script.split("function setMeasurePanelOpen(open)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "measureCardDismissed = !open" in panel_close
    assert "setMeasureMode(false)" in panel_close
    assert "canvas.focus({ preventScroll: true })" in close
    assert "clearMeasurements()" not in close
    render = script.split("function renderMeasurements()", 1)[1].split(
        "/** What to do next", 1
    )[0]
    assert "const hadCardFocus = card.contains(document.activeElement)" in render
    assert "if (card.hidden && hadCardFocus) canvas.focus" in render
    assert 'Clear all measurements' in html
    assert '.measure-row:focus-within .measure-drop' in styles
    assert '.measure-drop:focus-visible' in styles
    coarse = styles.split("@media (pointer: coarse)", 1)[1].split(
        "@media (forced-colors: active)", 1
    )[0]
    assert ".measure-head-actions .icon-btn" in coarse
    assert ".measure-drop" in coarse and "opacity: 1" in coarse
    assert ".measure-mode-grid" in styles
    assert ".measure-finish" in styles
    card_style = styles.split("#measure-card {", 1)[1].split("}", 1)[0]
    assert "top: 10px" in card_style
    assert "right: 12px" in card_style
    assert "bottom: auto" in card_style
    assert "width: min(300px" in card_style
    tool_panel = script.split("function setToolPanel", 1)[1].split(
        "function closePopovers", 1
    )[0]
    assert "setMeasurePanelOpen(false)" in tool_panel
    measure_launcher = script.split(
        '$("btn-tool-measure").addEventListener', 1
    )[1].split("const openMeasure", 1)[0]
    assert "closePopovers()" in measure_launcher
    popover_toggle = script.split("function togglePopover", 1)[1].split(
        'for (const [btnId, panelId] of POPOVERS)', 1
    )[0]
    assert "setMeasurePanelOpen(false)" in popover_toggle


def test_measurement_pointer_ownership_survives_touch_navigation_and_cancel(
    script: str,
) -> None:
    """Two fingers belong to camera navigation, and cancelling a drag may only
    remove the start that the same drag inserted."""
    assert "const activeTouchPointers = new Set();" in script
    assert "if (activeTouchPointers.size > 1)" in script
    assert "touchNavigationActive = true;" in script
    assert 'if (e.pointerType === "touch" && touchNavigationActive) return;' in script
    # TWO is deliberately left on OrbitControls' configured dolly/pan action.
    measure_mode = script.split("function setMeasureMode(on, kind)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "controls.touches.ONE = on ? null : ORBIT_ONE_TOUCH;" in measure_mode
    assert "controls.touches.TWO" not in measure_mode

    move = script.split('canvas.addEventListener("pointermove"', 1)[1].split(
        'canvas.addEventListener("pointerup"', 1
    )[0]
    assert "measureDragAddedStart = pending.length === 1;" in move
    cancel = script.split('canvas.addEventListener("pointercancel"', 1)[1].split(
        'window.addEventListener("blur"', 1
    )[0]
    blur = script.split('window.addEventListener("blur", () => {', 1)[1].split(
        chr(10) + "});", 1
    )[0]
    assert "if (measureDragAddedStart) undoPendingPoint();" in cancel
    assert "if (measureDragActive) undoPendingPoint();" not in cancel
    assert "if (measureDragAddedStart) undoPendingPoint();" in blur
    assert "if (measureDragActive) undoPendingPoint();" not in blur


def test_measurement_preview_line_reuses_one_dynamic_position_buffer(script: str) -> None:
    """Hovering must update one GPU buffer instead of abandoning one per sample."""
    preview = script.split("async function showSnapPreview(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "new THREE.BufferAttribute(new Float32Array(6), 3)" in preview
    assert "previewLinePosition.setUsage(THREE.DynamicDrawUsage);" in preview
    assert "previewLinePosition.setXYZ(0," in preview
    assert "previewLinePosition.setXYZ(1," in preview
    assert "previewLinePosition.needsUpdate = true;" in preview
    assert "previewLine.geometry.computeBoundingSphere();" in preview
    assert "previewLine.geometry.setFromPoints" not in preview


def test_section_slice_number_has_a_name_unit_and_fixed_layout(
    html: str, styles: str
) -> None:
    assert "Slice thickness" in html
    assert "0 = one-sided cut" in html
    assert 'class="number-field"' in html
    assert 'id="section-depth-unit" class="number-unit"' in html
    number = styles.split(".number-field {", 1)[1].split("}", 1)[0]
    assert "width: 92px" in number
    input_style = styles.split('.number-field input[type="number"] {', 1)[1].split(
        "}", 1
    )[0]
    assert "min-width: 0" in input_style
    assert "text-align: right" in input_style


def test_the_viewer_can_open_workflows_on_its_selection(chat_js: str, workflows_js: str):
    """One call from the viewer lands on the Run door with the selection scope
    already chosen, instead of asking the person to find the scope toggle."""
    assert "openWorkflows: (options = {}) => openWorkflows(undefined, options)" in chat_js
    assert 'else if (scope) workflowsPanel.launch?.({ scope });' in chat_js
    launch = workflows_js.split("launch({ scope = \"\" } = {})", 1)[1].split("dispose()", 1)[0]
    assert 'scope === "selection" && selectionCount(viewerContext)' in launch
    assert 'launcherScope = "selection"' in launch
    assert "renderAll()" in launch

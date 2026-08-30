/* Boot for the standalone workflows page. A separate file, not an inline
 * script, because the page runs under script-src 'self' with no unsafe-inline. */
import { mountWorkflows } from "/agents/static/workflows.js";

const root = document.getElementById("workflows-page-root");
// /workflows?workflow=<name> opens straight into one, which is what the
// terminal's `/workflows <name>` copies to the clipboard.
const requested = new URLSearchParams(location.search).get("workflow") || "";
const panel = mountWorkflows(root, { initial: requested });
panel.focus();

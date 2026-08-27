/* Boot for the standalone chat page. A separate file, not an inline script,
 * because the page runs under script-src 'self' with no unsafe-inline. */
import { mountChat } from "/viewer/static/chat.js";

const root = document.getElementById("chat-page-root");
const panel = mountChat(root, {
  onStatus: () => {
    // mountChat resolves workspace and local preferences on the component.
    // Mirror that exact result around it instead of repainting the page from
    // a second, potentially older status value.
    if (["light", "dark", "modern", "blue"].includes(root.dataset.theme)) {
      document.documentElement.dataset.consoleTheme = root.dataset.theme;
    }
  },
});
panel.focus();

/* Boot for the standalone chat page. A separate file, not an inline script,
 * because the page runs under script-src 'self' with no unsafe-inline. */
import { mountChat } from "/viewer/static/chat.js";

const panel = mountChat(document.getElementById("chat-page-root"), {
  onStatus: (status) => {
    if (status.theme === "dark" || status.theme === "light") {
      document.documentElement.dataset.consoleTheme = status.theme;
    }
  },
});
panel.focus();

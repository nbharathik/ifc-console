/* The session token from the page URL, and the authenticated fetch helper. */

// The token arrives in the URL fragment so it never reaches the server or its
// logs; keep it per-tab and scrub it from the address bar immediately.
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
// Read the query before scrubbing. A named optional panel is attached only
// when its launcher asks for it; a plain /viewer URL stays viewer-only even
// when an agent extension is installed and enabled in this session.
const queryParams = new URLSearchParams(location.search);
export const requestedPanel = queryParams.get("panel") || "";
export const token = hashParams.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (token) sessionStorage.setItem("ifc-console-token", token);
const tokenFromLink = hashParams.has("t");

// A rejected token is almost always a remembered one from an earlier console
// run, reached by opening a bookmarked URL with no #t= fragment. Forgetting it
// is what makes the next fresh link work, so the message can be about the
// link rather than about "authorization".
export function forgetStaleToken() {
  try {
    sessionStorage.removeItem("ifc-console-token");
  } catch {
    /* private mode: nothing was remembered anyway */
  }
}

export const STALE_TOKEN_TITLE = "This link has no valid access token";
export const STALE_TOKEN_BODY = tokenFromLink
  ? "The console that issued this link is no longer running, or it restarted with a new token. "
    + "Type /viewer in the ifc-console terminal for a fresh link."
  : "This URL is missing its #t= access token, so the browser fell back to a remembered one from "
    + "an earlier session. Type /viewer in the ifc-console terminal and use the link it copies.";
if (hashParams.has("t")) history.replaceState(null, "", location.pathname + location.search);

export async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${token}`, ...(options.headers || {}) };
  return fetch(path, { ...options, headers });
}

/* DOM helpers and the loading and message overlay of the viewer page. */

export const $ = (id) => document.getElementById(id);
const overlay = $("overlay");

export function showOverlay(title, detail = "", action = null, kind = "status") {
  overlay.dataset.state = "message";
  overlay.setAttribute("role", kind === "error" ? "alert" : "status");
  overlay.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  overlay.setAttribute("aria-busy", "false");
  overlay.textContent = "";
  const card = el("div", "overlay-card");
  card.appendChild(el("h2", "overlay-title", title));
  if (detail) card.appendChild(el("p", "overlay-message", detail));
  if (action) {
    const button = el("button", "overlay-action", action.label);
    button.type = "button";
    button.addEventListener("click", action.run);
    card.appendChild(button);
  }
  overlay.appendChild(card);
  overlay.hidden = false;
}
export function hideOverlay() {
  overlay.hidden = true;
  delete overlay.dataset.state;
  overlay.setAttribute("aria-busy", "false");
}
export function showProgress(label, fraction) {
  let card = overlay.querySelector(".loading-card");
  if (!card) {
    overlay.textContent = "";
    card = el("div", "overlay-card loading-card");
    card.appendChild(el("span", "loading-label"));
    const track = el("div", "progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", "Model loading progress");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.appendChild(el("div", "loading-bar"));
    card.appendChild(track);
    overlay.appendChild(card);
  }
  card.querySelector(".loading-label").textContent = label;
  const bar = card.querySelector(".loading-bar");
  const track = card.querySelector(".progress-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", "Model loading progress");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  if (fraction === null || fraction === undefined) {
    bar.classList.add("indeterminate");
    bar.style.width = "40%";
    track.removeAttribute("aria-valuenow");
  } else {
    bar.classList.remove("indeterminate");
    const percent = Math.round(Math.min(1, Math.max(0, fraction)) * 100);
    bar.style.width = `${percent}%`;
    track.setAttribute("aria-valuenow", String(percent));
  }
  overlay.dataset.state = "loading";
  overlay.setAttribute("role", "status");
  overlay.setAttribute("aria-live", "polite");
  overlay.setAttribute("aria-busy", "true");
  overlay.hidden = false;
}
export function hideProgress() {
  if (overlay.dataset.state === "loading") hideOverlay();
}
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

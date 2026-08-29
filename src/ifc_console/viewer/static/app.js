/* ifc-console viewer application.
 *
 * Build-free by design: plain ES modules, vendored three.js + web-ifc, no
 * bundler. Everything talks to the ifc-console server on the same origin:
 *   GET /api/model.ifc      the live in-memory model (ETag = fingerprint-rev)
 *   GET /api/elements/{id}  properties for the panel
 *   WS  /ws                 selection out, highlight/camera/screenshot in
 *
 * Fast path for large models: worker.js parses IFC off the main thread and
 * streams deduplicated geometry + placements as transferable chunks. The
 * viewer holds those chunks until parsing is complete, then the batcher bakes
 * the whole model into a few merged meshes (repeated geometry becomes
 * InstancedMesh). Per-element state lives in two small data textures read by
 * patched materials, picking is a 1x1 GPU id pass, and the render loop only
 * draws when something changed.
 *
 * The viewer is unprivileged: it renders, selects, and reports; it cannot
 * change the model or the session mode.
 */

import * as THREE from "./vendor/three.module.min.js";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { createViewerComponent } from "./viewer_component.js";
import {
  angleMeasure as angleCore,
  boxExtents,
  clearanceAxes,
  emptyBox,
  formatArea as formatAreaIn,
  formatLength as formatLengthIn,
  formatVolume as formatVolumeIn,
  geometryMass,
  LENGTH_UNITS,
  norm3,
  outlinePoints,
  planSpatialGrid,
  polylineMeasure as polylineCore,
  polygonMeasure as polygonCore,
  spanMeasure,
  unionBoxCorners,
  unitForFile,
  unitOf,
} from "./measure_math.js";

// ---------------------------------------------------------------- token / api
// The token arrives in the URL fragment so it never reaches the server or its
// logs; keep it per-tab and scrub it from the address bar immediately.
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
// Read the query before scrubbing. A named optional panel is attached only
// when its launcher asks for it; a plain /viewer URL stays viewer-only even
// when an agent extension is installed and enabled in this session.
const queryParams = new URLSearchParams(location.search);
const requestedPanel = queryParams.get("panel") || "";
const token = hashParams.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (token) sessionStorage.setItem("ifc-console-token", token);
const tokenFromLink = hashParams.has("t");

// A rejected token is almost always a remembered one from an earlier console
// run, reached by opening a bookmarked URL with no #t= fragment. Forgetting it
// is what makes the next fresh link work, so the message can be about the
// link rather than about "authorization".
function forgetStaleToken() {
  try {
    sessionStorage.removeItem("ifc-console-token");
  } catch {
    /* private mode: nothing was remembered anyway */
  }
}

const STALE_TOKEN_TITLE = "This link has no valid access token";
const STALE_TOKEN_BODY = tokenFromLink
  ? "The console that issued this link is no longer running, or it restarted with a new token. "
    + "Type /viewer in the ifc-console terminal for a fresh link."
  : "This URL is missing its #t= access token, so the browser fell back to a remembered one from "
    + "an earlier session. Type /viewer in the ifc-console terminal and use the link it copies.";
if (hashParams.has("t")) history.replaceState(null, "", location.pathname + location.search);

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${token}`, ...(options.headers || {}) };
  return fetch(path, { ...options, headers });
}

// ---------------------------------------------------------------- dom helpers
const $ = (id) => document.getElementById(id);
const overlay = $("overlay");

function showOverlay(title, detail = "", action = null, kind = "status") {
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
function hideOverlay() {
  overlay.hidden = true;
  delete overlay.dataset.state;
  overlay.setAttribute("aria-busy", "false");
}
function showProgress(label, fraction) {
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
function hideProgress() {
  if (overlay.dataset.state === "loading") hideOverlay();
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- units
// Geometry is SI metres whatever the file says, so every number the viewer
// holds is metres and only the label changes. The default is the file's own
// unit: a millimetre-authored model that reads "0.200 m" here and "200
// MILLIMETRE" from the tools is one product disagreeing with itself.
let lengthUnitChoice = "file";      // "file", or a key of LENGTH_UNITS
let fileLengthUnit = "m";           // what the open file is drawn in
let fileUnitInfo = null;            // the server's {length_unit, to_si_factor}
let lengthDecimals = null;          // null follows the unit's own default

function activeLengthUnit() {
  return LENGTH_UNITS[lengthUnitChoice] ? lengthUnitChoice : fileLengthUnit;
}

function activeDecimals() {
  return lengthDecimals === null ? unitOf(activeLengthUnit()).decimals : lengthDecimals;
}

function formatLength(metres) {
  return formatLengthIn(metres, activeLengthUnit(), activeDecimals());
}

function formatArea(squareMetres) {
  return formatAreaIn(squareMetres, activeLengthUnit());
}

function formatVolume(cubicMetres) {
  return formatVolumeIn(cubicMetres, activeLengthUnit());
}

/** How many of the active unit one metre is, for the controls that take one. */
function perMetre() {
  return unitOf(activeLengthUnit()).perMetre;
}

/** Repaint everything that carries a number, after a unit or precision change. */
function refreshUnitDisplay() {
  syncUnitControls();
  syncSliceInput();
  renderMeasurements();
  updateVisibilityInfo();
  scheduleViewerContext("units");
}

function syncUnitControls() {
  const picker = $("measure-unit");
  if (picker) {
    const fileOption = picker.querySelector('option[value="file"]');
    if (fileOption) fileOption.textContent = `File (${unitOf(fileLengthUnit).label})`;
    picker.value = lengthUnitChoice;
  }
  const places = $("measure-decimals");
  if (places) places.value = lengthDecimals === null ? "auto" : String(lengthDecimals);
}

function setLengthUnit(choice, { persist = true } = {}) {
  lengthUnitChoice = LENGTH_UNITS[choice] ? choice : "file";
  if (persist) {
    uiState.lengthUnit = lengthUnitChoice;
    saveUi();
  }
  refreshUnitDisplay();
}

function setLengthDecimals(value, { persist = true } = {}) {
  const places = Number(value);
  lengthDecimals = Number.isInteger(places) && places >= 0 && places <= 4 ? places : null;
  if (persist) {
    uiState.lengthDecimals = lengthDecimals;
    saveUi();
  }
  refreshUnitDisplay();
}

/** Adopt whatever unit the open file is drawn in, from the server's reading. */
function setFileUnits(units) {
  const next = unitForFile(units);
  const name = units ? units.length_unit || null : null;
  const known = fileUnitInfo ? fileUnitInfo.length_unit || null : null;
  if (units) fileUnitInfo = units;
  if (next === fileLengthUnit && name === known) return;
  fileLengthUnit = next;
  refreshUnitDisplay();
}

// ---------------------------------------------------------------- three scene
const canvas = $("canvas");
let renderer;
try {
  renderer = new THREE.WebGLRenderer({
    canvas, antialias: true, powerPreference: "high-performance",
  });
} catch (err) {
  showOverlay(
    "3D view unavailable",
    "Enable hardware acceleration or try another browser.",
    null,
    "error",
  );
  throw err;
}
renderer.outputColorSpace = THREE.SRGBColorSpace;

// Merged geometry frees its CPU-side arrays after upload, so a lost context
// cannot be repainted from what is still in memory. Drop the model at loss and
// refetch on restore; without this the canvas freezes until the user hits F5.
canvas.addEventListener("webglcontextlost", (event) => {
  event.preventDefault();
  disposeModel();
  currentEtag = null;
  showOverlay("3D context lost", "Rebuilding the view.");
});
canvas.addEventListener("webglcontextrestored", () => { loadModel(); });

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x101720);

const perspectiveCamera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
perspectiveCamera.position.set(12, 10, 12);
// Built next to it rather than on demand: the swap has to be able to happen
// mid-gesture without allocating anything.
const orthographicCamera = new THREE.OrthographicCamera(-10, 10, 10, -10, -1000, 5000);
orthographicCamera.position.copy(perspectiveCamera.position);
let camera = perspectiveCamera;
let orthoHeight = 40;

const controls = new OrbitControls(camera, canvas);
controls.dampingFactor = 0.12;
// Dolly toward the pointer, not the orbit target. On a site-scale model the
// target sits at the middle of the whole file, so zooming used to pull the
// camera into the centre and every look at a corner became zoom-then-pan.
controls.zoomToCursor = true;
controls.zoomSpeed = 1.6;
// Pan and orbit are both measured from the pivot, so a pivot that has been
// pulled to within millimetres of the lens makes a full drag move the camera
// almost nowhere. The floor is set from the model once it is known.
controls.minDistance = 0.05;
controls.listenToKeyEvents(canvas);
const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
// The factor OrbitControls would use at 60fps. Every frame rescales it to the
// time that frame actually took, so the glide is the same length in seconds
// whether the model draws in 4ms or 120ms.
const BASE_DAMPING = 0.12;
const applyMotionPreference = () => {
  controls.enableDamping = !motionPreference.matches;
};
applyMotionPreference();
motionPreference.addEventListener("change", applyMotionPreference);

scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.4);
sun.position.set(30, 50, 20);
scene.add(sun);

// The ground grid is one large plane whose shader draws 1 m / 10 m lines in
// world space with a camera-distance fade, so it reads as infinite for any
// model size. The lines live in world coordinates: repositioning the plane
// (to follow the camera target) never makes them swim.
function makeInfiniteGrid() {
  const material = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    extensions: { derivatives: true },
    uniforms: {
      uMinor: { value: new THREE.Color(0x24303d) },
      uMajor: { value: new THREE.Color(0x3c536a) },
      uFade: { value: 260 },
    },
    vertexShader: `
      varying vec3 vWorld;
      void main() {
        vec4 world = modelMatrix * vec4(position, 1.0);
        vWorld = world.xyz;
        gl_Position = projectionMatrix * viewMatrix * world;
      }`,
    fragmentShader: `
      varying vec3 vWorld;
      uniform vec3 uMinor;
      uniform vec3 uMajor;
      uniform float uFade;
      float gridLine(vec2 p, float spacing) {
        vec2 coord = p / spacing;
        vec2 width = max(fwidth(coord), vec2(0.0001));
        vec2 line = abs(fract(coord - 0.5) - 0.5) / width;
        float coverage = 1.0 - min(min(line.x, line.y), 1.0);

        // Suppress a grid level before it becomes sub-pixel. This prevents
        // distant lines from popping on and off while the camera is moving.
        float frequency = max(width.x, width.y);
        return coverage * (1.0 - smoothstep(0.35, 0.72, frequency));
      }
      void main() {
        float minor = gridLine(vWorld.xz, 1.0) * 0.34;
        float major = gridLine(vWorld.xz, 10.0) * 0.72;
        float fade = 1.0 - smoothstep(uFade * 0.35, uFade,
                                      distance(cameraPosition.xz, vWorld.xz));
        float alpha = max(minor, major) * fade;
        if (alpha < 0.015) discard;
        gl_FragColor = vec4(mix(uMinor, uMajor, step(minor, major)), alpha);
      }`,
  });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(8000, 8000), material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.renderOrder = -1;
  return mesh;
}

const grid = makeInfiniteGrid();
scene.add(grid);
let axes = null;   // rebuilt per model so its size matches the model
let groundY = 0;   // grid sits at the model's lowest point

// Theme: the workspace supplies the system choice over the WS. A local viewer
// preference may override it without changing the rest of the console.
const THEME_COLORS = {
  light: { canvas: 0xe7edf3, gridMinor: 0xc7d2dd, gridMajor: 0x9fb0c0 },
  dark: { canvas: 0x0d0e10, gridMinor: 0x24272c, gridMajor: 0x3b4048 },
  modern: { canvas: 0x080808, gridMinor: 0x202020, gridMajor: 0x383838 },
  blue: { canvas: 0x101720, gridMinor: 0x24303d, gridMajor: 0x3c536a },
};
let uiTheme = "blue";
let consoleTheme = null;
let themePreference = "system";

function resolvedTheme() {
  if (THEME_COLORS[themePreference]) return themePreference;
  if (consoleTheme && THEME_COLORS[consoleTheme]) return consoleTheme;
  return "blue";
}

function paintTheme(theme) {
  uiTheme = theme;
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.themePreference = themePreference;
  const picker = $("set-theme");
  if (picker) picker.value = themePreference === "system" ? theme : themePreference;
  const colors = THEME_COLORS[theme];
  scene.background.set(colors.canvas);
  grid.material.uniforms.uMinor.value.set(colors.gridMinor);
  grid.material.uniforms.uMajor.value.set(colors.gridMajor);
  // Ghosted context is pulled towards whatever the canvas is, so it recedes
  // in either theme instead of turning into grey fog on a light one.
  ghostTint.set(colors.canvas);
  invalidate();
  scheduleViewerContext("theme");
}

function applyTheme(name) {
  if (THEME_COLORS[name]) consoleTheme = name;
  paintTheme(resolvedTheme());
}

function setThemePreference(value, { persist = true } = {}) {
  themePreference = value === "system" || THEME_COLORS[value] ? value : "system";
  if (persist) {
    uiState.themePreference = themePreference;
    saveUi();
  }
  const picker = $("set-theme");
  if (picker) picker.value = themePreference === "system" ? resolvedTheme() : themePreference;
  paintTheme(resolvedTheme());
}

const modelRoot = new THREE.Group();
scene.add(modelRoot);

// ------------------------------------------------------- on-demand rendering
// The loop only draws when the camera moved or something invalidated the
// frame; while orbiting a heavy scene the buffer resolution drops so input
// stays responsive, and snaps back to full quality when motion settles.
let needsRender = true;
let resScale = 1;
let lastRenderMs = 0;
let interacting = false;
let userMovedCamera = false;
let frameRequest = 0;

function requestFrame() {
  if (frameRequest || document.hidden) return;
  frameRequest = requestAnimationFrame(renderFrame);
}

function invalidate() {
  needsRender = true;
  requestFrame();
}

// A reader that has to know where the camera is should not have to poll for
// it, but the controls fire start and end around every single wheel tick, so
// the push waits for the gesture to settle rather than riding each frame.
let cameraSettle = 0;
controls.addEventListener("start", () => {
  interacting = true;
  userMovedCamera = true;
  invalidate();
  clearTimeout(cameraSettle);
  // The hand on the mouse outranks any glide still in flight; carrying on
  // would fight the damping the gesture is about to apply.
  cameraTween = null;
});
controls.addEventListener("end", () => {
  interacting = false;
  invalidate();
  clearTimeout(cameraSettle);
  cameraSettle = setTimeout(() => scheduleViewerContext("camera"), 250);
});
// OrbitControls applies wheel zoom synchronously. Its later update() can then
// report no movement, so listen to change directly and never miss that frame.
controls.addEventListener("change", invalidate);
// Anything measured off the screen is measured off one camera. The counter
// says which, so an answer that arrives after the view moved can be dropped.
let cameraSerial = 0;
controls.addEventListener("change", () => { cameraSerial++; });

let viewportWidth = 0;
let viewportHeight = 0;
function applyResolution() {
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2) * resScale);
  renderer.setSize(viewportWidth, viewportHeight, false);
}
function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  if (w === viewportWidth && h === viewportHeight) return;
  viewportWidth = w;
  viewportHeight = h;
  applyResolution();
  applyProjectionShape();
  // Projection and drawing-buffer coordinates changed even though
  // OrbitControls did not emit change; discard any old async pick decode.
  cameraSerial++;
  // The observer fires before paint. Queue one component frame instead of
  // drawing here and then drawing the same state again from the render loop.
  invalidate();
}
new ResizeObserver(resize).observe(canvas.parentElement);
resize();

/** Give whichever camera is live the current aspect ratio. */
function applyProjectionShape() {
  const aspect = viewportWidth / Math.max(viewportHeight, 1);
  perspectiveCamera.aspect = aspect;
  perspectiveCamera.updateProjectionMatrix();
  const half = orthoHeight / 2;
  orthographicCamera.top = half;
  orthographicCamera.bottom = -half;
  orthographicCamera.left = -half * aspect;
  orthographicCamera.right = half * aspect;
  orthographicCamera.updateProjectionMatrix();
}

/** True while the viewer is drawing a parallel projection. */
function isOrtho() {
  return camera.isOrthographicCamera === true;
}

/**
 * Swap projections without moving the eye.
 *
 * The two cameras share a position and a pivot, so the switch only has to
 * choose a zoom that shows the same amount of model. Matching the vertical
 * extent at the pivot distance is what makes it read as a change of
 * projection rather than a jump to somewhere else.
 */
function setProjection(kind) {
  const wantOrtho = kind === "orthographic" || kind === "ortho" || kind === true;
  if (wantOrtho === isOrtho()) return isOrtho() ? "orthographic" : "perspective";
  const distance = Math.max(camera.position.distanceTo(controls.target), 1e-4);
  const target = wantOrtho ? orthographicCamera : perspectiveCamera;
  target.position.copy(camera.position);
  target.up.copy(camera.up);
  target.quaternion.copy(camera.quaternion);
  if (wantOrtho) {
    // The height perspective is showing at the pivot, held exactly.
    const visible = 2 * Math.tan((perspectiveCamera.fov * Math.PI) / 360) * distance;
    target.zoom = orthoHeight / Math.max(visible, 1e-6);
  } else {
    // ... and back the other way, by placing the eye where that height would
    // need it to be.
    const visible = orthoHeight / Math.max(orthographicCamera.zoom, 1e-6);
    const back = visible / (2 * Math.tan((perspectiveCamera.fov * Math.PI) / 360));
    target.position.copy(controls.target).addScaledVector(
      camera.position.clone().sub(controls.target).normalize(), back);
  }
  camera = target;
  controls.object = camera;
  applyProjectionShape();
  applyNearFar();
  controls.update();
  const button = $("tool-ortho");
  if (button) {
    button.setAttribute("aria-pressed", String(wantOrtho));
    button.classList.toggle("is-active", wantOrtho);
  }
  invalidate();
  scheduleViewerContext("projection");
  updateVisibilityInfo();
  return wantOrtho ? "orthographic" : "perspective";
}

/** The model's overall size, or a sane default before one is loaded. */
function sceneSpan() {
  if (!modelBox) return 50;
  return Math.max(
    modelBox[3] - modelBox[0],
    modelBox[4] - modelBox[1],
    modelBox[5] - modelBox[2],
    1e-3,
  );
}

/**
 * Depth range from where the camera is now, not from where it was framed.
 *
 * A fixed near plane is a zoom limit with no message: geometry simply vanishes
 * as you approach. Deriving it from the live pivot distance keeps the same
 * depth precision at every scale and lets the camera close on a detail as far
 * as the geometry allows.
 */
function applyNearFar() {
  const distance = Math.max(camera.position.distanceTo(controls.target), 1e-4);
  // A parallel projection has no perspective divide, so depth precision does
  // not collapse near the eye and the range can simply cover the model.
  const near = isOrtho()
    ? -Math.max(distance, sceneSpan()) * 2
    : Math.max(distance / 2000, 1e-4);
  const far = Math.max(distance, sceneSpan()) * (isOrtho() ? 4 : 200);
  if (near === camera.near && far === camera.far) return;
  camera.near = near;
  camera.far = far;
  camera.updateProjectionMatrix();
}

function renderNow() {
  grid.position.x = Math.round(controls.target.x / 100) * 100;
  grid.position.z = Math.round(controls.target.z / 100) * 100;
  syncScreenMarkers();
  const t0 = performance.now();
  renderer.render(scene, camera);
  lastRenderMs = performance.now() - t0;
  needsRender = false;
}

let lastFrameAt = 0;
function renderFrame(now) {
  frameRequest = 0;
  // First, so the orbit controls read the pose the glide has just written.
  if (cameraTween) stepCameraTween(now);
  if (controls.enableDamping) {
    const elapsed = lastFrameAt ? Math.min(now - lastFrameAt, 250) : 16.7;
    // 1 - (1 - f)^n is the same decay applied n times, so a 100ms frame gets
    // the six frames' worth of catch-up it stood in for.
    controls.dampingFactor = Math.min(
      1,
      1 - Math.pow(1 - BASE_DAMPING, Math.max(elapsed, 1) / 16.7),
    );
  }
  lastFrameAt = now;
  if (syncEdgeVisibility()) needsRender = true;
  const moved = controls.update();
  if (moved || needsRender) applyNearFar();
  if (moved || needsRender) {
    if (moved && interacting) {
      // Adaptive resolution: last frame's cost decides this frame's scale.
      const want = lastRenderMs > 45 ? 0.45 : lastRenderMs > 20 ? 0.6 : resScale;
      if (want < resScale) {
        resScale = want;
        applyResolution();
      }
    }
    renderNow();
  } else if (!interacting && resScale !== 1) {
    resScale = 1;
    applyResolution();
    renderNow();
  }
  // Damping and camera transitions need successive frames. Everything else
  // sleeps until invalidate() is called, avoiding a 60 Hz callback in an idle
  // viewer or a background Agent workspace.
  if (moved || cameraTween || needsRender) requestFrame();
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) invalidate();
});
requestFrame();

// Screenshots and picking need a full-resolution buffer regardless of the
// adaptive scale the orbit interaction may have left behind.
function ensureFullResolution() {
  if (resScale === 1) return false;
  resScale = 1;
  applyResolution();
  // The drawing buffer was resized, so the canvas is owed a redraw; the caller
  // knows whether anything else it did already owes one.
  return true;
}

// ---------------------------------------------------------------- model state
let currentEtag = null;
let loading = false;
let reloadQueued = false;
// Which resident model this tab shows. null follows the console's active
// model; a model_id pins one of the attached ones (read-only, like the tools).
let viewModelId = null;
let modelRows = [];
const closedModelTabs = new Set();
// A browser tab may keep the Agent workspace open without claiming that its
// hidden WebGL scene is an available viewer. The model remains resident on the
// server; this flag only says whether this page is currently showing it.
let viewerDocumentOpen = true;
// Each IFC tab keeps its own working view in memory. Switching documents no
// longer carries a cut, camera or selection into an unrelated model, and
// returning to a tab feels like returning to the same drawing.
const modelTabViews = new Map();
// Parsing is the expensive WebAssembly step. Keep the parsed chunks for every
// recent IFC revision so returning to a tab is quick, but bound the typed
// arrays: an unlimited cache duplicated the GPU scene for every resident file.
const parsedModelCache = new Map(); // model id -> { etag, parsed, bytes }
const PARSED_CACHE_MAX_ENTRIES = 2;
const PARSED_CACHE_BUDGET = Math.round(
  Math.min(256, Math.max(96, (Number(navigator.deviceMemory) || 4) * 64)) * 1_048_576,
);
let parsedModelCacheBytes = 0;

function parsedModelBytes(parsed) {
  let bytes = 0;
  const addArrays = (record) => {
    for (const value of Object.values(record || {})) {
      if (ArrayBuffer.isView(value)) bytes += value.byteLength;
    }
  };
  for (const chunk of parsed?.chunks || []) {
    addArrays(chunk.geometry);
    addArrays(chunk.placements);
  }
  addArrays(parsed?.maps);
  for (const guid of parsed?.maps?.guids || []) bytes += guid.length * 2;
  return bytes;
}

function dropParsedModel(modelId) {
  const previous = parsedModelCache.get(modelId);
  if (!previous) return;
  parsedModelCache.delete(modelId);
  parsedModelCacheBytes -= previous.bytes;
}

function cacheParsedModel(modelId, etag, parsed) {
  if (!modelId || !etag || modelRows.length < 2) return;
  const bytes = parsedModelBytes(parsed);
  dropParsedModel(modelId);
  if (bytes > PARSED_CACHE_BUDGET) return;
  parsedModelCache.set(modelId, { etag, parsed, bytes });
  parsedModelCacheBytes += bytes;
  while (
    parsedModelCache.size > PARSED_CACHE_MAX_ENTRIES
    || parsedModelCacheBytes > PARSED_CACHE_BUDGET
  ) {
    dropParsedModel(parsedModelCache.keys().next().value);
  }
}

function cachedParsedModel(modelId, etag) {
  const entry = parsedModelCache.get(modelId);
  if (!entry || entry.etag !== etag) {
    if (entry) dropParsedModel(modelId);
    return null;
  }
  // Map insertion order is the LRU list.
  parsedModelCache.delete(modelId);
  parsedModelCache.set(modelId, entry);
  return entry;
}
// A selection belongs to its IFC, not to whichever tab happens to be visible.
// GlobalIds make these snapshots stable across rebuilds and let the chat carry
// several models' selections at once.
const modelSelections = new Map(); // model id -> GlobalIds
let pendingModelTabView = null;
let loadingModelId = null;
// The scene can keep showing the previous IFC while another tab parses. This
// id names what the meshes and express-id maps actually belong to.
let renderedModelId = null;

function modelQuery() {
  return viewModelId ? `?model=${encodeURIComponent(viewModelId)}` : "";
}

const guidOf = new Map();     // expressID -> GlobalId
const expressOf = new Map();  // GlobalId -> expressID

const selection = new Set();      // expressIDs
let highlightSet = new Set();     // expressIDs
let highlightColor = "#ff3b30";
let isolateSet = null;            // expressIDs visible during isolation, or null
const hiddenByTree = new Set();   // expressIDs hidden via tree checkboxes
let userIsolateSet = null;        // user-driven isolation from the view tools
const hiddenManual = new Set();   // expressIDs hidden via the view tools

// Color theme (LLM computes, viewer paints): GlobalId-keyed so it survives
// scene rebuilds; unmatched ids simply paint nothing after a model switch.
const themeByGuid = new Map();    // GlobalId -> hex
let themeLegend = [];             // [{label, color, count}]
let themeTitle = "";

let viewerContextQueued = false;
let viewerContextReason = "state";

function selectedGuids() {
  return [...selection].map((id) => guidOf.get(id)).filter(Boolean);
}

function rememberCurrentSelection() {
  if (!viewerDocumentOpen) return [];
  const modelId = renderedModelId || currentModelRow()?.id;
  if (!modelId) return [];
  const guids = selectedGuids();
  if (guids.length) modelSelections.set(modelId, guids);
  else modelSelections.delete(modelId);
  return guids;
}

function selectionGuidsForModel(modelId) {
  const currentId = viewerDocumentOpen
    ? (renderedModelId || currentModelRow()?.id)
    : null;
  if (modelId === currentId) return selectedGuids();
  return [...(modelSelections.get(modelId) || [])];
}

function modelSelectionRows() {
  if (!viewerDocumentOpen) return [];
  return modelRows.flatMap((row) => {
    const guids = selectionGuidsForModel(row.id);
    return guids.length ? [{
      model_id: row.id,
      model: row.name,
      count: guids.length,
      guids,
    }] : [];
  });
}

function viewerContext(reason = viewerContextReason) {
  const row = viewerDocumentOpen ? currentModelRow() : null;
  const fallbackName = $("model-name")?.textContent || "";
  return {
    version: 1,
    reason,
    open: viewerDocumentOpen,
    model: row
      ? { id: row.id, name: row.name, schema: row.schema || "" }
      : viewerDocumentOpen && fallbackName
        ? { id: null, name: fallbackName, schema: $("schema")?.textContent || "" }
        : null,
    models: modelRows.map((item) => ({
      id: item.id,
      name: item.name,
      schema: item.schema || "",
      active: item.active === true,
    })),
    selection: { count: selection.size, guids: selectedGuids() },
    selections: modelSelectionRows(),
    mode: $("mode")?.dataset.mode || null,
    theme: { preference: themePreference, resolved: uiTheme },
    isolated: userIsolateSet ? userIsolateSet.size : 0,
    // `hidden` is what isElementShown rejects, which is what hide, isolate,
    // fit and every pick act on; a ghost is drawn but not among them, so it is
    // counted there and named again on its own.
    visibility: {
      hidden: hiddenCount,
      isolated: (userIsolateSet || isolateSet)?.size ?? 0,
      ghosted: ghostCount,
      total: elements.size,
    },
    focus: { active: Boolean(userIsolateSet), count: userIsolateSet?.size || 0 },
    views: Object.keys(VIEW_DIRECTIONS),
    projection: isOrtho() ? "orthographic" : "perspective",
    // Where the eye is, in the model's axes. Without it an agent is reasoning
    // about a screen it cannot see, and world_per_pixel is how it knows
    // whether the detail it is about to talk about is even resolvable.
    camera: cameraState(),
    viewport: { width: viewportWidth, height: viewportHeight },
    // Coordinates in and out of this viewer are the model's, not the
    // viewport's; this says how the two are related on the current file.
    frame: {
      units: "metres, model axes (z up)",
      coordinated: coordinationApplied,
      axes: axisFrame,
    },
    // The wire stays SI; this is what the screen is labelled in, and what the
    // file was drawn in, so an answer can quote the same unit the user reads.
    units: {
      display: activeLengthUnit(),
      decimals: activeDecimals(),
      file: activeLengthUnit() === fileLengthUnit,
      file_unit: fileLengthUnit,
      length_unit: fileUnitInfo ? fileUnitInfo.length_unit || null : null,
      to_si_factor: fileUnitInfo ? fileUnitInfo.to_si_factor ?? null : null,
    },
    section: sectionState(),
    savedViews: savedViews().map((view) => view.name),
    panels: {
      model: !$("tree-panel")?.classList.contains("collapsed"),
      properties: !$("props-panel")?.classList.contains("collapsed"),
    },
    capabilities: {
      modelSelection: true,
      selectionContext: true,
      screenshotEvidence: true,
      panelControl: true,
      themeControl: true,
      cameraControl: true,
      commands: [
        "get-context",
        "set-theme",
        "set-model",
        "set-panel",
        "capture-evidence",
        "set-selection",
        "clear-selection",
        "focus-selection",
        "isolate",
        "show-all",
        "hide",
        "focus",
        "unfocus",
        "set-view",
        "set-camera",
        "fit",
        "measure-element",
        "measure-laser",
        "measure-points",
        "measure-angle",
        "measure-area",
        "clear-measurements",
        "set-projection",
        "set-section",
        "save-view",
        "restore-view",
        "list-views",
      ],
    },
  };
}

// The viewer is one reusable component whether the caller is its own chrome,
// an attached Agent panel, or the server's WebSocket command handler. Panels
// receive the typed facade directly; DOM events remain a compatibility shim.
const viewerComponentHost = createViewerComponent({
  readContext: viewerContext,
  execute: runViewerCommand,
});

function scheduleViewerContext(reason = "state") {
  viewerContextReason = reason;
  if (viewerContextQueued) return;
  viewerContextQueued = true;
  queueMicrotask(() => {
    viewerContextQueued = false;
    viewerComponentHost.publish(viewerContext(viewerContextReason));
  });
}

function sendViewerResult(command, ok, result = null, error = null) {
  viewerComponentHost.publishResult(command, ok, result, error);
}

// --------------------------------------------------- element state textures
// Per-element render state lives in two RGBA textures indexed by a dense
// element index carried in an aElementIndex vertex/instance attribute:
//   state:    R visible flag, G emissive strength (selection / highlight glow)
//   override: RGB replacement color, A override flag (selection, highlight,
//             LLM color themes)
// Hiding or restyling any subset of elements is a few byte writes plus one
// texture upload: no geometry rebuilds, no material swaps, no draw calls.
const STATE_W = 1024;
let stateH = 64;
let stateData = new Uint8Array(STATE_W * stateH * 4);
let overrideData = new Uint8Array(STATE_W * stateH * 4);
let stateTex = null;
let overrideTex = null;
const liveShaders = new Set();

function makeStateTextures() {
  if (stateTex) stateTex.dispose();
  if (overrideTex) overrideTex.dispose();
  stateTex = new THREE.DataTexture(stateData, STATE_W, stateH);
  overrideTex = new THREE.DataTexture(overrideData, STATE_W, stateH);
  for (const tex of [stateTex, overrideTex]) {
    tex.minFilter = THREE.NearestFilter;
    tex.magFilter = THREE.NearestFilter;
    tex.needsUpdate = true;
  }
  for (const holder of liveShaders) {
    holder.uniforms.uStateTex.value = stateTex;
    holder.uniforms.uOverrideTex.value = overrideTex;
    holder.uniforms.uStateSize.value.set(STATE_W, stateH);
  }
  if (pickMaterial) {
    pickMaterial.uniforms.uStateTex.value = stateTex;
    pickMaterial.uniforms.uStateSize.value.set(STATE_W, stateH);
  }
}

function ensureStateCapacity(count) {
  if (count <= STATE_W * stateH) return;
  let newH = stateH;
  while (STATE_W * newH < count) newH *= 2;
  const newState = new Uint8Array(STATE_W * newH * 4);
  const newOverride = new Uint8Array(STATE_W * newH * 4);
  newState.set(stateData);
  newState.fill(255, stateData.length);   // grown region: visible, no glow
  for (let i = stateData.length; i < newState.length; i += 4) {
    newState[i + 1] = 0;
    newState[i + 2] = 0;
    newState[i + 3] = 0;
  }
  newOverride.set(overrideData);
  stateData = newState;
  overrideData = newOverride;
  stateH = newH;
  makeStateTextures();
}

function resetStateTextures() {
  for (let i = 0; i < stateData.length; i += 4) {
    stateData[i] = 255;
    stateData[i + 1] = 0;
    stateData[i + 2] = 0;
    stateData[i + 3] = 0;
  }
  overrideData.fill(0);
}
resetStateTextures();

const _styleColor = new THREE.Color();
function writeElementStyle(index, hex, emissive) {
  const o = index * 4;
  if (hex === null) {
    overrideData[o + 3] = 0;
    stateData[o + 1] = 0;
  } else {
    _styleColor.set(hex);
    overrideData[o] = Math.round(Math.min(1, _styleColor.r) * 255);
    overrideData[o + 1] = Math.round(Math.min(1, _styleColor.g) * 255);
    overrideData[o + 2] = Math.round(Math.min(1, _styleColor.b) * 255);
    overrideData[o + 3] = 255;
    stateData[o + 1] = Math.round(Math.min(1, emissive) * 255);
  }
}

function commitStyles() {
  stateTex.needsUpdate = true;
  overrideTex.needsUpdate = true;
  invalidate();
}

// ---------------------------------------------------------------- section planes
// One plane per axis, shared by every patched material. The array identity is
// stable so materials never need re-assigning, only a recompile when the count
// changes.
const AXES = ["x", "y", "z"];
const AXIS_NORMALS = {
  x: new THREE.Vector3(-1, 0, 0),
  y: new THREE.Vector3(0, -1, 0),
  z: new THREE.Vector3(0, 0, -1),
};
const clipPlanes = {
  x: new THREE.Plane(AXIS_NORMALS.x.clone(), 0),
  y: new THREE.Plane(AXIS_NORMALS.y.clone(), 0),
  z: new THREE.Plane(AXIS_NORMALS.z.clone(), 0),
};
// The far side of a slice, one per axis, only pushed when there is a slice.
const clipPlanesBack = {
  x: new THREE.Plane(AXIS_NORMALS.x.clone(), 0),
  y: new THREE.Plane(AXIS_NORMALS.y.clone(), 0),
  z: new THREE.Plane(AXIS_NORMALS.z.clone(), 0),
};
let sliceDepth = 0;
const activeClipPlanes = [];
const section = {
  x: { on: false, t: 1, flip: false },
  y: { on: false, t: 1, flip: false },
  z: { on: false, t: 1, flip: false },
};

// Ghosting. The state texture's R channel is no longer a flag: 255 is drawn,
// 0 is gone, and this middle value is context. Isolating a duct used to delete
// the building around it, which is the opposite of what a review tool does and
// leaves the isolated part floating with nothing to place it against.
const GHOST_LEVEL = 64;
// Screen-door transparency: a share of the pixels is discarded on an ordered
// 4x4 grid. Real alpha would mean a transparent pass, a sort and a second draw
// of half the model; this costs one compare and stays in the opaque queue.
const ghostFill = { value: 0.22 };
const ghostTint = new THREE.Color(THEME_COLORS.dark.canvas);

// Patched Lambert: forward the element index, discard hidden elements, dither
// ghosted ones, substitute the override color, add the glow term.
const patchedMaterials = new Set();
function patchMaterial(mat, { depthBias = 0 } = {}) {
  patchedMaterials.add(mat);
  mat.clippingPlanes = activeClipPlanes;
  mat.onBeforeCompile = (shader) => {
    const previous = mat.userData.ifcShader;
    if (previous) liveShaders.delete(previous);
    mat.userData.ifcShader = shader;
    shader.uniforms.uStateTex = { value: stateTex };
    shader.uniforms.uOverrideTex = { value: overrideTex };
    shader.uniforms.uStateSize = { value: new THREE.Vector2(STATE_W, stateH) };
    shader.uniforms.uGhostFill = ghostFill;
    shader.uniforms.uGhostTint = { value: ghostTint };
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>",
        "#include <common>\nattribute float aElementIndex;\nvarying float vIfcIndex;")
      .replace("#include <begin_vertex>",
        "vIfcIndex = aElementIndex;\n#include <begin_vertex>");
    if (depthBias) {
      // Edge lines sit exactly on the triangle edges they came from, so half
      // their pixels lose the depth test to the surface and the outline
      // shimmers. A constant nudge towards the eye in clip space is scale
      // independent and costs one multiply-add.
      shader.vertexShader = shader.vertexShader.replace(
        "#include <project_vertex>",
        `#include <project_vertex>\ngl_Position.z -= ${depthBias.toFixed(6)} * gl_Position.w;`);
    }
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>",
        "#include <common>\n"
        + "uniform sampler2D uStateTex;\n"
        + "uniform sampler2D uOverrideTex;\n"
        + "uniform vec2 uStateSize;\n"
        + "uniform float uGhostFill;\n"
        + "uniform vec3 uGhostTint;\n"
        + "varying float vIfcIndex;\n"
        // A 4x4 ordered threshold built from two nested 2x2 ones: sixteen
        // distinct values per tile, no texture and no integer ops.
        + "float ifcOrdered(vec2 p) {\n"
        + "  vec2 c = floor(mod(p, 4.0));\n"
        + "  vec2 lo = mod(c, 2.0);\n"
        + "  vec2 hi = floor(c * 0.5);\n"
        + "  return (4.0 * mod(2.0 * lo.x + 3.0 * lo.y, 4.0)\n"
        + "        + mod(2.0 * hi.x + 3.0 * hi.y, 4.0)) / 16.0;\n"
        + "}")
      .replace("#include <clipping_planes_fragment>",
        "float ifcId = floor(vIfcIndex + 0.5);\n"
        + "vec2 ifcUv = vec2((mod(ifcId, uStateSize.x) + 0.5) / uStateSize.x,\n"
        + "                  (floor(ifcId / uStateSize.x) + 0.5) / uStateSize.y);\n"
        + "vec4 ifcState = texture2D(uStateTex, ifcUv);\n"
        + "if (ifcState.r < 0.05) discard;\n"
        + "float ifcGhost = step(ifcState.r, 0.5);\n"
        + "if (ifcGhost > 0.5 && ifcOrdered(gl_FragCoord.xy) > uGhostFill) discard;\n"
        + "vec4 ifcOverride = texture2D(uOverrideTex, ifcUv);\n"
        + "#include <clipping_planes_fragment>")
      // A colour theme repaints an element outright; a selection or a
      // highlight must not. Flat-filling the body threw away the shading
      // that says what the shape is, so it tints part way and puts the
      // rest of the energy into a view-dependent rim, which reads as an
      // outline along the silhouette.
      .replace("#include <color_fragment>",
        "#include <color_fragment>\n"
        + "float ifcMarked = step(0.001, ifcState.g);\n"
        + "float ifcTint = step(0.5, ifcOverride.a) * mix(1.0, 0.42, ifcMarked);\n"
        + "diffuseColor.rgb = mix(diffuseColor.rgb, ifcOverride.rgb, ifcTint);\n"
        // Context reads as context: what survives the dither is pulled most of
        // the way to the background so it never competes with the subject.
        + "diffuseColor.rgb = mix(diffuseColor.rgb, uGhostTint, 0.55 * ifcGhost);")
      .replace("#include <emissivemap_fragment>",
        "#include <emissivemap_fragment>\n"
        + "vec3 ifcView = normalize(vViewPosition);\n"
        + "float ifcFace = clamp(abs(dot(normal, ifcView)), 0.0, 1.0);\n"
        + "float ifcRim = pow(1.0 - ifcFace, 2.2);\n"
        + "totalEmissiveRadiance += ifcOverride.rgb * ifcState.g * (0.30 + 2.2 * ifcRim);");
    liveShaders.add(shader);
  };
  mat.customProgramCacheKey = () => (depthBias ? "ifc-state-edge" : "ifc-state");
  return mat;
}

const mergedMaterial = patchMaterial(new THREE.MeshLambertMaterial({
  vertexColors: true, side: THREE.DoubleSide,
}));
const mergedTransparentMaterial = patchMaterial(new THREE.MeshLambertMaterial({
  vertexColors: true, side: THREE.DoubleSide, transparent: true, depthWrite: false,
}));
const instancedMaterial = patchMaterial(new THREE.MeshLambertMaterial({
  side: THREE.DoubleSide,
}));
const instancedTransparentMaterials = new Map();
function instancedTransparentMaterialFor(alpha) {
  const key = alpha.toFixed(3);
  let mat = instancedTransparentMaterials.get(key);
  if (!mat) {
    mat = patchMaterial(new THREE.MeshLambertMaterial({
      side: THREE.DoubleSide, transparent: true, depthWrite: false, opacity: alpha,
    }));
    instancedTransparentMaterials.set(key, mat);
  }
  return mat;
}

// ---------------------------------------------------------------- edges
// Untextured IFC geometry with no edges is unreadable: two walls of the same
// colour that meet are one blob, and the adaptive resolution makes it an
// aliased one. The lines carry the same aElementIndex as the surface they came
// from, so hiding, clipping, ghosting and tinting all reach them for free.
const EDGE_ANGLE = 30;
const EDGE_DEPTH_BIAS = 0.0003;
// Measurement keeps only a compact feature index. A tessellation too large
// to inspect uses exact surface picking without feature snapping: a box edge
// is not necessarily an edge of the product and must never be presented as
// one. A product can retain at most 800 real segments. Placements reference the
// shared local array, keeping the wider snap coverage inexpensive.
const SNAP_TRIANGLE_LIMIT = 50_000;
const SNAP_SEGMENT_LIMIT = 800;
const EMPTY_SNAP_EDGES = new Float32Array(0);
// Extraction is per unique shape and the bake is per placement, so the cost
// tracks the merged half of the model; past this the lines stop paying for
// their memory and the switch says so.
const EDGE_VERTEX_BUDGET = 6_000_000;
const edgeRoot = new THREE.Group();
edgeRoot.name = "edges";
scene.add(edgeRoot);
const edgeMaterial = patchMaterial(new THREE.LineBasicMaterial({
  color: 0x0d131a, transparent: true, opacity: 0.55, depthWrite: false,
}), { depthBias: EDGE_DEPTH_BIAS });
let edgesWanted = false;     // IFC surfaces are the default; outlines are opt-in
let edgesAffordable = true;  // this model is small enough to draw them
let edgeDrawCount = 0;

function edgesOn() {
  return edgesWanted && edgesAffordable;
}

/**
 * Edges are dropped while the buffer is scaled down.
 *
 * That is the frame that is already late, and thin lines are exactly what a
 * 0.45 buffer cannot draw without turning into a crawl of stairsteps.
 */
function syncEdgeVisibility() {
  const want = edgesOn() && !(interacting && resScale < 1);
  if (edgeRoot.visible === want) return false;
  edgeRoot.visible = want;
  return true;
}

/** Keep the Display switch honest about a model too big to outline. */
function syncEdgeSwitch() {
  const box = $("set-edges");
  if (!box) return;
  box.checked = edgesWanted;
  box.disabled = !edgesAffordable;
  const note = $("set-edges-note");
  if (note) {
    note.textContent = edgesAffordable
      ? "Optional mesh creases over the default IFC surfaces."
      : "Unavailable: this model is too large to outline.";
  }
}

/**
 * The crease and boundary edges of one unique shape, in its own coordinates.
 *
 * Deduplicated geometry is the point: a door type placed four hundred times
 * pays for this once. Cached on the registry entry and built lazily, so a
 * shape that ends up instanced never pays at all.
 */
function edgeListFor(geom) {
  if (geom.edges !== undefined) return geom.edges;
  let out = null;
  try {
    const source = new THREE.BufferGeometry();
    source.setAttribute("position", new THREE.BufferAttribute(geom.positions, 3));
    source.setIndex(new THREE.BufferAttribute(geom.indices, 1));
    const edges = new THREE.EdgesGeometry(source, EDGE_ANGLE);
    out = edges.getAttribute("position").array;
    source.dispose();
    edges.dispose();
  } catch {
    // A degenerate tessellation is not worth failing a model load over.
    out = null;
  }
  geom.edges = out;
  return out;
}

/** Actual crease/boundary edges when affordable, otherwise no false feature. */
function snapEdgeListFor(geom) {
  if (geom.snapEdges !== undefined) return geom.snapEdges;
  const triangles = geom.indices.length / 3;
  const extracted = triangles <= SNAP_TRIANGLE_LIMIT ? edgeListFor(geom) : null;
  if (extracted && extracted.length) {
    const count = Math.floor(extracted.length / 6);
    if (count <= SNAP_SEGMENT_LIMIT) {
      geom.snapEdges = Float32Array.from(extracted);
    } else {
      // Spread the budget across the shape; taking only the first edges makes
      // a long or multipart product snap at one end and nowhere else.
      const sampled = new Float32Array(SNAP_SEGMENT_LIMIT * 6);
      for (let i = 0; i < SNAP_SEGMENT_LIMIT; i++) {
        const at = Math.floor((i * count) / SNAP_SEGMENT_LIMIT) * 6;
        sampled.set(extracted.subarray(at, at + 6), i * 6);
      }
      geom.snapEdges = sampled;
    }
  } else {
    geom.snapEdges = EMPTY_SNAP_EDGES;
  }
  return geom.snapEdges;
}

/** Retain shared local features plus this placement while ingest still owns both. */
function recordSnapParts(rec, geom, matrix) {
  const segments = snapEdgeListFor(geom);
  const sourceCount = Math.floor(segments.length / 6);
  if (!sourceCount) return;
  for (let i = 0; i < 16; i++) {
    if (!Number.isFinite(matrix[i])) return;
  }
  const placed = Float32Array.from(matrix);
  placed[12] -= origin[0];
  placed[13] -= origin[1];
  placed[14] -= origin[2];
  if (!rec.snapParts) rec.snapParts = [];
  rec.snapParts.push({ segments, matrix: placed, sourceCount, count: 0 });
}

/** Share the per-product edge budget fairly across all of its placements. */
function finalizeSnapParts() {
  for (const rec of elements.values()) {
    const parts = rec.snapParts || [];
    let remaining = SNAP_SEGMENT_LIMIT;
    for (let i = 0; i < parts.length; i++) {
      const share = Math.max(1, Math.floor(remaining / (parts.length - i)));
      parts[i].count = Math.min(parts[i].sourceCount, remaining, share);
      remaining -= parts[i].count;
      if (remaining <= 0) break;
    }
  }
}

// GPU id picking: a 1x1 render with this override encodes elementIndex + 1
// into 24 bits of color; 0 is background. Hidden elements discard, so a pick
// can never land on something the user cannot see.
const pickMaterial = new THREE.ShaderMaterial({
  side: THREE.DoubleSide,
  clipping: true,
  uniforms: {
    uStateTex: { value: null },
    uStateSize: { value: new THREE.Vector2(STATE_W, stateH) },
  },
  vertexShader: `
    attribute float aElementIndex;
    varying float vIfcIndex;
    #include <clipping_planes_pars_vertex>
    void main() {
      vIfcIndex = aElementIndex;
      vec4 p = vec4(position, 1.0);
      #ifdef USE_INSTANCING
        p = instanceMatrix * p;
      #endif
      vec4 mvPosition = modelViewMatrix * p;
      #include <clipping_planes_vertex>
      gl_Position = projectionMatrix * mvPosition;
    }`,
  fragmentShader: `
    varying float vIfcIndex;
    uniform sampler2D uStateTex;
    uniform vec2 uStateSize;
    #include <clipping_planes_pars_fragment>
    void main() {
      #include <clipping_planes_fragment>
      float id = floor(vIfcIndex + 0.5);
      vec2 uv = vec2((mod(id, uStateSize.x) + 0.5) / uStateSize.x,
                     (floor(id / uStateSize.x) + 0.5) / uStateSize.y);
      if (texture2D(uStateTex, uv).r < 0.05) discard;
      float enc = id + 1.0;
      gl_FragColor = vec4(
        floor(enc / 65536.0) / 255.0,
        floor(mod(enc, 65536.0) / 256.0) / 255.0,
        mod(enc, 256.0) / 255.0,
        1.0);
    }`,
});
// Same 1x1 trick for measuring: encode view-axis depth into 24 bits. The range
// is tightened to the model bounds for every probe instead of spanning the
// camera's deliberately huge far plane. That keeps millimetre-scale picks
// stable even in a kilometre-scale site. Merged chunks free their CPU arrays
// after upload, so the GPU remains the source of the exact surface point.
const depthMaterial = new THREE.ShaderMaterial({
  side: THREE.DoubleSide,
  clipping: true,
  uniforms: {
    uStateTex: { value: null },
    uStateSize: { value: new THREE.Vector2(STATE_W, stateH) },
    uNear: { value: 0 },
    uFar: { value: 1 },
  },
  vertexShader: `
    attribute float aElementIndex;
    varying float vIfcIndex;
    varying vec3 vMeasureViewPosition;
    #include <clipping_planes_pars_vertex>
    void main() {
      vIfcIndex = aElementIndex;
      vec4 p = vec4(position, 1.0);
      #ifdef USE_INSTANCING
        p = instanceMatrix * p;
      #endif
      vec4 mvPosition = modelViewMatrix * p;
      vMeasureViewPosition = mvPosition.xyz;
      #include <clipping_planes_vertex>
      gl_Position = projectionMatrix * mvPosition;
    }`,
  fragmentShader: `
    varying float vIfcIndex;
    varying vec3 vMeasureViewPosition;
    uniform sampler2D uStateTex;
    uniform vec2 uStateSize;
    uniform float uNear;
    uniform float uFar;
    #include <clipping_planes_pars_fragment>
    void main() {
      #include <clipping_planes_fragment>
      float id = floor(vIfcIndex + 0.5);
      vec2 uv = vec2((mod(id, uStateSize.x) + 0.5) / uStateSize.x,
                     (floor(id / uStateSize.x) + 0.5) / uStateSize.y);
      if (texture2D(uStateTex, uv).r < 0.05) discard;
      float measured = -vMeasureViewPosition.z;
      float d = clamp((measured - uNear) / (uFar - uNear), 0.0, 1.0);
      vec3 enc = fract(vec3(1.0, 255.0, 65025.0) * d);
      enc -= enc.yzz * vec3(1.0 / 255.0, 1.0 / 255.0, 0.0);
      gl_FragColor = vec4(enc, 1.0);
    }`,
});

makeStateTextures();
const pickTarget = new THREE.WebGLRenderTarget(1, 1);
const pickBuffer = new Uint8Array(4);

// ---------------------------------------------------------------- batcher
// Parsed chunks land here only after the complete IFC is available. Unique
// geometries register once; every placement then either bakes into a merged
// buffer for the grid cell it stands in, or joins an InstancedMesh if that
// geometry is placed often enough to be worth a draw call of its own. Merged
// buffers finalize into meshes at a vertex limit.
const CHUNK_VERTEX_LIMIT = 500_000;
const SPATIAL_SPLIT_VERTS = 50_000;
// A geometry placed fewer times than this is cheaper baked into its
// neighbourhood: one InstancedMesh per pair of mirrored doors cost thousands
// of uncullable draw calls to save a few thousand vertices.
const INSTANCE_MIN = 8;
// Past this many copies one instanced mesh spans the whole model and can never
// be culled, so it is split by octant. Splitting on the finer merge grid
// instead turns a curtain wall into one draw call per panel, which is the bug
// this threshold exists to avoid, only from the other side.
const INSTANCE_SPLIT = 256;
// Roughly one merged draw call's worth of geometry per cell, and the vertices
// the open cells may hold between them while the rest of the model arrives.
// Chunks are in no spatial order, so every cell stays open to the end.
const CELL_VERTEX_TARGET = 120_000;
const STAGING_VERTEX_BUDGET = 6_000_000;
const MIN_CHUNK_VERTS = 150_000;
const MIN_CELLS = 8;
const ORIGIN_THRESHOLD = 1e4;

const elements = new Map();       // expressID -> {index, box: number[6]}
const elementsByIndex = [];       // dense index -> expressID
const registry = new Map();       // geometryID -> {positions, normals, indices, box, radius}
const geomUse = new Map();        // `${gid}:${alphaKey}[#cell]` -> InstEntry
let accumulators = new Map();     // cellKey -> Accumulator
let modelBox = null;              // [minx,miny,minz,maxx,maxy,maxz] world f64
let cellSize = 0;                 // batching grid pitch, 0 while unsplit
let cellFlushAt = CHUNK_VERTEX_LIMIT;
let origin = [0, 0, 0];
let drawCount = 0;
let triangleCount = 0;

class GrowArray {
  constructor(Type) {
    this.Type = Type;
    this.data = new Type(4096);
    this.length = 0;
  }

  reserve(extra) {
    const need = this.length + extra;
    if (need <= this.data.length) return;
    let size = this.data.length;
    while (size < need) size *= 2;
    const next = new this.Type(size);
    next.set(this.data.subarray(0, this.length));
    this.data = next;
  }

  trim() {
    return this.data.slice(0, this.length);
  }
}

class Accumulator {
  constructor(transparent) {
    this.transparent = transparent;
    this.positions = new GrowArray(Float32Array);
    this.normals = new GrowArray(Float32Array);
    this.colors = new GrowArray(Float32Array);
    this.elementIndex = new GrowArray(Float32Array);
    this.index = new GrowArray(Uint32Array);
    this.vertexCount = 0;
    // The outline of the same cell, staged alongside so one flush ships both.
    this.edgePositions = null;
    this.edgeElementIndex = null;
    this.edgeVertexCount = 0;
  }

  edges() {
    if (!this.edgePositions) {
      this.edgePositions = new GrowArray(Float32Array);
      this.edgeElementIndex = new GrowArray(Float32Array);
    }
    return this.edgePositions;
  }
}

function elementRecord(expressID) {
  let rec = elements.get(expressID);
  if (!rec) {
    const index = elementsByIndex.length;
    ensureStateCapacity(index + 1);
    rec = {
      index,
      box: [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity],
      area: 0,
      volume: 0,
      // The placement of the biggest part, kept unflattened so measurement
      // can ask about the element's own axes rather than the world's.
      obb: null,
      obbReach: -1,
      snapParts: null,
      scaled: false,
    };
    elements.set(expressID, rec);
    elementsByIndex.push(expressID);
  }
  return rec;
}

const _m4 = new THREE.Matrix4();
const _n3 = new THREE.Matrix3();

// ------------------------------------------------------------- coordinates
// Two frames. The scene's, which is what three.js draws and what every pick
// and bounding box is in; and the model's, which is what the IFC file says
// and what every answer has to be in. web-ifc's coordination matrix is the
// step between them, and `origin` is the extra shift this viewer applies to
// keep a georeferenced file inside f32.
// web-ifc hands geometry back Y-up: the file's Z becomes the scene's Y and
// the file's Y becomes the scene's -Z. Its coordination matrix carries the
// origin shift on top of that and nothing else, so both are needed to get
// back to the file's own coordinates.
const IFC_TO_GL = new THREE.Matrix4().set(
  1, 0, 0, 0,
  0, 0, 1, 0,
  0, -1, 0, 0,
  0, 0, 0, 1,
);
let coordinationMatrix = new THREE.Matrix4();
let coordinationApplied = false;
const modelToScene = new THREE.Matrix4();
const sceneToModel = new THREE.Matrix4();
// Which scene axis each model axis runs along, and which way round.
let axisFrame = {
  x: { axis: "x", sign: 1 }, y: { axis: "z", sign: 1 }, z: { axis: "y", sign: 1 },
};
const MODEL_OF_SCENE = { x: "x", y: "z", z: "y" };

function refreshFrames() {
  modelToScene.copy(coordinationMatrix).multiply(IFC_TO_GL);
  modelToScene.premultiply(
    new THREE.Matrix4().makeTranslation(-origin[0], -origin[1], -origin[2]));
  sceneToModel.copy(modelToScene).invert();
  const probe = new THREE.Vector3();
  for (const name of ["x", "y", "z"]) {
    probe.set(name === "x" ? 1 : 0, name === "y" ? 1 : 0, name === "z" ? 1 : 0);
    probe.transformDirection(modelToScene);
    const axis = Math.abs(probe.x) >= Math.abs(probe.y) && Math.abs(probe.x) >= Math.abs(probe.z)
      ? "x" : Math.abs(probe.y) >= Math.abs(probe.z) ? "y" : "z";
    axisFrame[name] = { axis, sign: probe[axis] < 0 ? -1 : 1 };
    MODEL_OF_SCENE[axis] = name;
  }
}
refreshFrames();

/** A scene point as the model's own [x, y, z]. */
function toModelPoint(point) {
  const out = point.clone().applyMatrix4(sceneToModel);
  return [out.x, out.y, out.z];
}

/** A model [x, y, z] as a scene point. */
function toScenePoint(triple) {
  return new THREE.Vector3(triple[0], triple[1], triple[2]).applyMatrix4(modelToScene);
}

/** Where `value` on one scene axis falls on the model axis that runs along it. */
function toModelAxis(sceneAxis, value) {
  const probe = new THREE.Vector3();
  probe[sceneAxis] = value;
  const out = probe.applyMatrix4(sceneToModel);
  return out[MODEL_OF_SCENE[sceneAxis]];
}

/** And back: a model-axis position as a position on the scene axis. */
function toSceneAxis(sceneAxis, modelValue) {
  const at0 = toModelAxis(sceneAxis, 0);
  const at1 = toModelAxis(sceneAxis, 1);
  return at1 === at0 ? 0 : (modelValue - at0) / (at1 - at0);
}

/** A scene direction as the model's own unit [x, y, z]; no origin shift. */
function toModelDirection(vector) {
  const out = vector.clone().transformDirection(sceneToModel);
  return [out.x, out.y, out.z];
}

/** And back: a model direction as a scene direction. */
function toSceneDirection(triple) {
  return new THREE.Vector3(triple[0], triple[1], triple[2])
    .transformDirection(modelToScene);
}

/**
 * The origin shift, the model bounds, every placement's world box and how
 * often each geometry is placed, in one walk over the placements.
 *
 * COORDINATE_TO_ORIGIN already recentres typical files; the shift catches
 * models whose placements still carry georeferenced offsets, applied in f64
 * before anything is cast to f32 so vertices never jitter.
 *
 * The boxes come back with it because ingest wants exactly the same eight
 * corners, and because modelBox has to be whole before the first chunk is
 * batched: cell keys taken against a box that is still growing put the
 * earliest chunks in the wrong cells.
 */
function decideOrigin(chunks) {
  origin = [0, 0, 0];
  const probe = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
  const layout = new Map();
  const uses = new Map();
  let total = 0;
  for (const chunk of chunks) {
    const placements = chunk.placements;
    const count = placements.expressIDs.length;
    const boxes = new Float64Array(count * 6);
    let verts = 0;
    for (let p = 0; p < count; p++) {
      const at = p * 6;
      emptyBox(boxes, at);
      const geom = registry.get(placements.geometryIDs[p]);
      if (!geom) continue;
      verts += geom.positions.length / 3;
      const key = useKeyFor(placements.geometryIDs[p], placements.colors[p * 4 + 3]);
      uses.set(key, (uses.get(key) || 0) + 1);
      unionBoxCorners(
        boxes, geom.box, placements.matrices.subarray(p * 16, p * 16 + 16), at,
        origin);
      for (let k = 0; k < 3; k++) {
        if (boxes[at + k] < probe[k]) probe[k] = boxes[at + k];
        if (boxes[at + k + 3] > probe[k + 3]) probe[k + 3] = boxes[at + k + 3];
      }
    }
    layout.set(chunk, { boxes, verts });
    total += verts;
  }
  const maxAbs = Math.max(...probe.map((v) => Math.abs(v)).filter((v) => isFinite(v)), 0);
  if (maxAbs > ORIGIN_THRESHOLD) {
    origin = [
      (probe[0] + probe[3]) / 2,
      (probe[1] + probe[4]) / 2,
      (probe[2] + probe[5]) / 2,
    ];
    // Both were measured before the shift was known. Subtracting after the
    // fact is exact: IEEE subtraction is monotonic, so no min or max moves.
    for (const entry of layout.values()) {
      const boxes = entry.boxes;
      for (let i = 0; i < boxes.length; i += 6) {
        for (let k = 0; k < 3; k++) {
          boxes[i + k] -= origin[k];
          boxes[i + k + 3] -= origin[k];
        }
      }
    }
    for (let k = 0; k < 3; k++) {
      probe[k] -= origin[k];
      probe[k + 3] -= origin[k];
    }
  }
  return { box: isFinite(probe[0]) ? probe : null, layout, uses, verts: total };
}

function freeUploadedArray() {
  this.array = null;
}

function finalizeAccumulator(acc) {
  if (!acc.vertexCount) return;
  const geo = new THREE.BufferGeometry();
  const pos = new THREE.BufferAttribute(acc.positions.trim(), 3);
  const nor = new THREE.BufferAttribute(acc.normals.trim(), 3);
  const col = new THREE.BufferAttribute(acc.colors.trim(), acc.transparent ? 4 : 3);
  const idx = new THREE.BufferAttribute(acc.elementIndex.trim(), 1);
  geo.setAttribute("position", pos);
  geo.setAttribute("normal", nor);
  geo.setAttribute("color", col);
  geo.setAttribute("aElementIndex", idx);
  geo.setIndex(new THREE.BufferAttribute(acc.index.trim(), 1));
  // Sphere first: culling needs it, and the arrays are freed after upload.
  geo.computeBoundingSphere();
  for (const attr of [pos, nor, col, idx, geo.index]) attr.onUpload(freeUploadedArray);
  const mesh = new THREE.Mesh(
    geo, acc.transparent ? mergedTransparentMaterial : mergedMaterial);
  mesh.matrixAutoUpdate = false;
  modelRoot.add(mesh);
  drawCount++;
  finalizeEdges(acc);
}

function finalizeEdges(acc) {
  if (!acc.edgeVertexCount) return;
  const geo = new THREE.BufferGeometry();
  const pos = new THREE.BufferAttribute(acc.edgePositions.trim(), 3);
  const idx = new THREE.BufferAttribute(acc.edgeElementIndex.trim(), 1);
  geo.setAttribute("position", pos);
  geo.setAttribute("aElementIndex", idx);
  geo.computeBoundingSphere();
  for (const attr of [pos, idx]) attr.onUpload(freeUploadedArray);
  const lines = new THREE.LineSegments(geo, edgeMaterial);
  lines.matrixAutoUpdate = false;
  edgeRoot.add(lines);
  edgeDrawCount++;
}

/**
 * Which batching cell a placement's box belongs to.
 *
 * A 2x2x2 split of the model was too coarse to cull: a camera standing inside
 * a room intersects most octants, so almost every triangle was submitted every
 * frame, on every 1x1 pick and on every hover probe. The grid is a real one,
 * sized by planSpatialGrid against the whole model's bounds, which is why
 * modelBox has to be settled before the first chunk is batched.
 */
function cellKeyFor(box, at, transparent) {
  const prefix = transparent ? "t" : "o";
  if (!cellSize || !modelBox) return prefix;
  const cx = Math.floor((box[at] - modelBox[0]) / cellSize);
  const cy = Math.floor((box[at + 1] - modelBox[1]) / cellSize);
  const cz = Math.floor((box[at + 2] - modelBox[2]) / cellSize);
  return `${prefix}${cx},${cy},${cz}`;
}

/** Which eighth of the model a placement stands in, for splitting instances. */
function octantKeyFor(box, at) {
  if (!modelBox) return "";
  let key = "";
  for (let k = 0; k < 3; k++) {
    key += box[at + k] < (modelBox[k] + modelBox[k + 3]) / 2 ? "0" : "1";
  }
  return key;
}

function bakeMerged(rec, geom, matrix, color, transparent, key) {
  let acc = accumulators.get(key);
  if (!acc || acc.transparent !== transparent) {
    acc = new Accumulator(transparent);
    accumulators.set(key, acc);
  }
  const vCount = geom.positions.length / 3;
  const stride = transparent ? 4 : 3;
  acc.positions.reserve(vCount * 3);
  acc.normals.reserve(vCount * 3);
  acc.colors.reserve(vCount * stride);
  acc.elementIndex.reserve(vCount);
  acc.index.reserve(geom.indices.length);

  _m4.fromArray(matrix);
  _n3.getNormalMatrix(_m4);
  const m = matrix;
  const n = _n3.elements;
  const P = acc.positions;
  const N = acc.normals;
  const C = acc.colors;
  const E = acc.elementIndex;
  const src = geom.positions;
  const srcN = geom.normals;
  const r = color[0], g = color[1], b = color[2], a = color[3];
  for (let v = 0; v < vCount; v++) {
    const s = v * 3;
    const x = src[s], y = src[s + 1], z = src[s + 2];
    P.data[P.length++] = m[0] * x + m[4] * y + m[8] * z + m[12] - origin[0];
    P.data[P.length++] = m[1] * x + m[5] * y + m[9] * z + m[13] - origin[1];
    P.data[P.length++] = m[2] * x + m[6] * y + m[10] * z + m[14] - origin[2];
    const nx = srcN[s], ny = srcN[s + 1], nz = srcN[s + 2];
    let tx = n[0] * nx + n[3] * ny + n[6] * nz;
    let ty = n[1] * nx + n[4] * ny + n[7] * nz;
    let tz = n[2] * nx + n[5] * ny + n[8] * nz;
    const len = norm3(tx, ty, tz) || 1;
    N.data[N.length++] = tx / len;
    N.data[N.length++] = ty / len;
    N.data[N.length++] = tz / len;
    C.data[C.length++] = r;
    C.data[C.length++] = g;
    C.data[C.length++] = b;
    if (transparent) C.data[C.length++] = a;
    E.data[E.length++] = rec.index;
  }
  const base = acc.vertexCount;
  const I = acc.index;
  for (let i = 0; i < geom.indices.length; i++) {
    I.data[I.length++] = geom.indices[i] + base;
  }
  acc.vertexCount += vCount;
  // Affordability, not the switch: the outline is staged with the surface it
  // belongs to, so a switch flipped later could not go back and build one.
  if (edgesAffordable) bakeEdges(acc, rec, geom, m);
  if (acc.vertexCount >= cellFlushAt) {
    finalizeAccumulator(acc);
    accumulators.delete(key);
  }
}

/** Place one shape's outline into the same cell its surface went into. */
function bakeEdges(acc, rec, geom, m) {
  const src = edgeListFor(geom);
  if (!src || !src.length) return;
  const EP = acc.edges();
  const EE = acc.edgeElementIndex;
  const count = src.length / 3;
  EP.reserve(src.length);
  EE.reserve(count);
  for (let v = 0; v < count; v++) {
    const s = v * 3;
    const x = src[s], y = src[s + 1], z = src[s + 2];
    EP.data[EP.length++] = m[0] * x + m[4] * y + m[8] * z + m[12] - origin[0];
    EP.data[EP.length++] = m[1] * x + m[5] * y + m[9] * z + m[13] - origin[1];
    EP.data[EP.length++] = m[2] * x + m[6] * y + m[10] * z + m[14] - origin[2];
    EE.data[EE.length++] = rec.index;
  }
  acc.edgeVertexCount += count;
}

class InstEntry {
  constructor(gid, geom, alpha, expected) {
    this.gid = gid;
    // The count is known before ingest starts, so the per-instance arrays are
    // allocated once instead of doubling from sixteen and copying each time.
    this.expected = expected;
    this.geomRadius = Math.hypot(
      Math.max(Math.abs(geom.box[0]), Math.abs(geom.box[3])),
      Math.max(Math.abs(geom.box[1]), Math.abs(geom.box[4])),
      Math.max(Math.abs(geom.box[2]), Math.abs(geom.box[5])));
    this.capacity = 0;
    this.count = 0;
    this.tMin = [Infinity, Infinity, Infinity];
    this.tMax = [-Infinity, -Infinity, -Infinity];
    this.maxScale = 1;
    this.material = alpha < 0.999
      ? instancedTransparentMaterialFor(alpha) : instancedMaterial;
    // The source attributes are shared by every generation of the mesh; only
    // the per-instance arrays are reallocated on growth. Copied out of the
    // parser chunk: a subarray view would pin the whole 8 MB chunk for the
    // life of the model.
    this.posAttr = new THREE.BufferAttribute(geom.positions.slice(), 3);
    this.norAttr = new THREE.BufferAttribute(geom.normals.slice(), 3);
    this.idxAttr = new THREE.BufferAttribute(geom.indices.slice(), 1);
    this.sphere = new THREE.Sphere();
    this.mesh = null;
    this.elementIndexAttr = null;
    this._grow();
  }

  _grow() {
    const old = this.mesh;
    const capacity = old ? this.capacity * 2 : Math.max(16, this.expected);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", this.posAttr);
    geo.setAttribute("normal", this.norAttr);
    geo.setIndex(this.idxAttr);
    geo.boundingSphere = this.sphere;
    const attr = new THREE.InstancedBufferAttribute(new Float32Array(capacity), 1);
    geo.setAttribute("aElementIndex", attr);
    const mesh = new THREE.InstancedMesh(geo, this.material, capacity);
    mesh.count = this.count;
    mesh.matrixAutoUpdate = false;
    mesh.boundingSphere = this.sphere;
    if (old) {
      mesh.instanceMatrix.array.set(old.instanceMatrix.array.subarray(0, this.count * 16));
      attr.array.set(this.elementIndexAttr.array.subarray(0, this.count));
      const oldColor = old.instanceColor;
      mesh.setColorAt(0, _instColor.setRGB(1, 1, 1));
      if (oldColor) {
        mesh.instanceColor.array.set(oldColor.array.subarray(0, this.count * 3));
      }
      modelRoot.remove(old);
      old.dispose();
      old.geometry.dispose();
    } else {
      drawCount++;
    }
    this.capacity = capacity;
    this.mesh = mesh;
    this.elementIndexAttr = attr;
    modelRoot.add(mesh);
  }

  add(rec, matrix, color) {
    if (this.count >= this.capacity) this._grow();
    const i = this.count++;
    _m4.fromArray(matrix);
    const e = _m4.elements;
    e[12] -= origin[0];
    e[13] -= origin[1];
    e[14] -= origin[2];
    this.mesh.setMatrixAt(i, _m4);
    this.mesh.setColorAt(i, _instColor.setRGB(color[0], color[1], color[2]));
    this.elementIndexAttr.array[i] = rec.index;
    this.mesh.count = this.count;
    this.mesh.instanceMatrix.needsUpdate = true;
    this.mesh.instanceColor.needsUpdate = true;
    this.elementIndexAttr.needsUpdate = true;
    // Incremental culling sphere: translation AABB + rotated geometry reach.
    for (let k = 0; k < 3; k++) {
      const t = e[12 + k];
      if (t < this.tMin[k]) this.tMin[k] = t;
      if (t > this.tMax[k]) this.tMax[k] = t;
    }
    const scale = Math.max(
      norm3(e[0], e[1], e[2]),
      norm3(e[4], e[5], e[6]),
      norm3(e[8], e[9], e[10]));
    if (scale > this.maxScale) this.maxScale = scale;
    this.sphere.center.set(
      (this.tMin[0] + this.tMax[0]) / 2,
      (this.tMin[1] + this.tMax[1]) / 2,
      (this.tMin[2] + this.tMax[2]) / 2);
    this.sphere.radius = norm3(
      this.tMax[0] - this.tMin[0],
      this.tMax[1] - this.tMin[1],
      this.tMax[2] - this.tMin[2]) / 2 + this.geomRadius * this.maxScale;
  }
}
const _instColor = new THREE.Color();

function registerChunkGeometry(chunk) {
  const g = chunk.geometry;
  let po = 0;
  let io = 0;
  let bo = 0;
  for (let i = 0; i < g.ids.length; i++) {
    const vc = g.vertexCounts[i];
    const ic = g.indexCounts[i];
    const positions = g.positions.subarray(po, po + vc * 3);
    const indices = g.indices.subarray(io, io + ic);
    registry.set(g.ids[i], {
      positions,
      normals: g.normals.subarray(po, po + vc * 3),
      indices,
      box: g.bounds.subarray(bo, bo + 6),
      // Deduplicated, so this runs once per shape however many times it is
      // placed. Doing it later is not an option: the arrays are freed.
      mass: geometryMass(positions, indices),
    });
    po += vc * 3;
    io += ic;
    bo += 6;
  }
}

/**
 * Add one placement's area, volume and candidate oriented box to its element.
 *
 * IFC placements are rigid in every file worth measuring, so the local numbers
 * carry over unchanged. Where a placement does scale, volume follows the
 * determinant exactly and area follows it only under uniform scale, which is
 * why a scaled element says so rather than quietly reporting the wrong area.
 */
function accrueMass(rec, geom, m) {
  const sx = norm3(m[0], m[1], m[2]);
  const sy = norm3(m[4], m[5], m[6]);
  const sz = norm3(m[8], m[9], m[10]);
  const det = sx * sy * sz;
  if (Math.max(sx, sy, sz) > Math.min(sx, sy, sz) * 1.01) rec.scaled = true;
  rec.area += geom.mass.area * Math.cbrt(det * det);
  rec.volume += geom.mass.volume * det;
  // Biggest part wins the frame: a wall with a small opening solid attached
  // should be measured along the wall.
  const reach = norm3(
    (geom.box[3] - geom.box[0]) * sx,
    (geom.box[4] - geom.box[1]) * sy,
    (geom.box[5] - geom.box[2]) * sz);
  if (reach > rec.obbReach) {
    rec.obbReach = reach;
    const local = new Float32Array(16);
    for (let k = 0; k < 16; k++) local[k] = m[k];
    // The origin shift happens in f64 here so the f32 store never sees the
    // georeferenced magnitude that made the shift necessary.
    local[12] = m[12] - origin[0];
    local[13] = m[13] - origin[1];
    local[14] = m[14] - origin[2];
    rec.obb = { m: local, box: Float32Array.from(geom.box) };
  }
}

/** How the batcher tells two placements of one shape apart: shape and alpha. */
function useKeyFor(geometryID, alpha) {
  return alpha < 0.999 ? `${geometryID}:${alpha.toFixed(3)}` : `${geometryID}:o`;
}

function ingestChunk(chunk, layout, uses) {
  const p = chunk.placements;
  const P = p.expressIDs.length;
  const boxes = layout.boxes;

  for (let i = 0; i < P; i++) {
    const geom = registry.get(p.geometryIDs[i]);
    if (!geom) continue;
    const expressID = p.expressIDs[i];
    const matrix = p.matrices.subarray(i * 16, i * 16 + 16);
    const color = p.colors.subarray(i * 4, i * 4 + 4);
    const rec = elementRecord(expressID);
    const at = i * 6;
    for (let k = 0; k < 3; k++) {
      if (boxes[at + k] < rec.box[k]) rec.box[k] = boxes[at + k];
      if (boxes[at + k + 3] > rec.box[k + 3]) rec.box[k + 3] = boxes[at + k + 3];
    }
    accrueMass(rec, geom, matrix);
    recordSnapParts(rec, geom, matrix);
    triangleCount += geom.indices.length / 3;

    const alpha = color[3];
    const transparent = alpha < 0.999;
    const useKey = useKeyFor(p.geometryIDs[i], alpha);
    // Decided once for the whole model rather than on the second sighting, so
    // a shape placed twice no longer buys a draw call of its own.
    const copies = uses.get(useKey) || 1;
    if (copies < INSTANCE_MIN) {
      bakeMerged(
        rec, geom, matrix, color, transparent, cellKeyFor(boxes, at, transparent));
      continue;
    }
    const split = copies >= INSTANCE_SPLIT;
    const instKey = split ? `${useKey}#${octantKeyFor(boxes, at)}` : useKey;
    let entry = geomUse.get(instKey);
    if (entry === undefined) {
      entry = new InstEntry(
        p.geometryIDs[i], geom, alpha, split ? Math.ceil(copies / 8) : copies);
      geomUse.set(instKey, entry);
    }
    entry.add(rec, matrix, color);
  }
}

function finalizeAllAccumulators() {
  for (const acc of accumulators.values()) finalizeAccumulator(acc);
  accumulators.clear();
}

function disposeModel() {
  for (const child of [...modelRoot.children]) {
    modelRoot.remove(child);
    if (child.isInstancedMesh) child.dispose();
    if (child.geometry) child.geometry.dispose();
  }
  for (const child of [...edgeRoot.children]) {
    edgeRoot.remove(child);
    child.geometry.dispose();
  }
  edgeDrawCount = 0;
  for (const mat of instancedTransparentMaterials.values()) {
    const shader = mat.userData.ifcShader;
    if (shader) liveShaders.delete(shader);
    patchedMaterials.delete(mat);
    mat.dispose();
  }
  instancedTransparentMaterials.clear();
  elements.clear();
  elementsByIndex.length = 0;
  registry.clear();
  geomUse.clear();
  accumulators = new Map();
  modelBox = null;
  cellSize = 0;
  cellFlushAt = CHUNK_VERTEX_LIMIT;
  origin = [0, 0, 0];
  drawCount = 0;
  triangleCount = 0;
  resetStateTextures();
  commitStyles();
  guidOf.clear();
  expressOf.clear();
  selection.clear();
  highlightSet.clear();
  isolateSet = null;
  hiddenByTree.clear();
  userIsolateSet = null;
  hiddenManual.clear();
  updateSelectionInfo();
  updateHighlightInfo();
  clearProperties();
}

function updateStats() {
  $("stats").textContent = elements.size
    ? `${elements.size} products · ${Math.round(triangleCount / 1000)}k tris · `
      + `${drawCount + edgeDrawCount} draws`
    : "";
}

function updateGround() {
  const empty = !modelBox;
  const radius = empty ? 20 : Math.hypot(
    modelBox[3] - modelBox[0],
    modelBox[4] - modelBox[1],
    modelBox[5] - modelBox[2]) / 2;
  // enough clearance below the model that the depth buffer reliably hides
  // the grid under floors (a few mm would z-fight at building distances)
  groundY = empty ? 0 : modelBox[1] - Math.max(0.05, radius * 0.002);
  grid.position.y = groundY;
  grid.material.uniforms.uFade.value = Math.min(Math.max(radius * 5, 120), 2500);
  if (axes) {
    scene.remove(axes);
    axes.dispose();
  }
  axes = new THREE.AxesHelper(Math.max(5, radius * 0.3));
  axes.position.y = groundY + 0.001;
  scene.add(axes);
  applySceneSettings();
  invalidate();
}

// ---------------------------------------------------------------- model load
// worker.js does the parsing; if workers are unavailable the same parser
// module runs inline on the main thread as a fallback.
let worker = null;
let workerBusy = false;
let workerIdleTimer = 0;
let loadGen = 0;
let activeHandlers = null;
const WORKER_IDLE_MS = 30_000;

function clearWorkerIdle() {
  clearTimeout(workerIdleTimer);
  workerIdleTimer = 0;
}

function stopWorker() {
  clearWorkerIdle();
  if (worker) worker.terminate();
  worker = null;
  workerBusy = false;
}

function scheduleWorkerIdle() {
  clearWorkerIdle();
  if (!worker || workerBusy) return;
  // web-ifc closes the model but its WASM heap stays at its high-water mark.
  // Keep it briefly for a tab switch, then return that memory to the browser.
  workerIdleTimer = setTimeout(() => {
    if (!workerBusy) stopWorker();
  }, WORKER_IDLE_MS);
}

function routeParserMessage(msg) {
  if (!activeHandlers || msg.seq !== loadGen) return;
  const h = activeHandlers;
  if (msg.type === "chunk") h.onChunk(msg);
  else if (msg.type === "progress") h.onProgress(msg);
  else if (msg.type === "coordination") h.onCoordination(msg);
  else if (msg.type === "maps") h.onMaps(msg);
  else if (msg.type === "tree") h.onTree(msg);
  else if (msg.type === "done") h.onDone(msg);
  else if (msg.type === "error" && msg.init_failed) {
    stopWorker();
    h.onWorkerLost();
  }
  else if (msg.type === "error") h.onError(new Error(msg.message));
}

function spawnWorker() {
  clearWorkerIdle();
  worker = new Worker("/viewer/static/worker.js", { type: "module" });
  worker.onmessage = (event) => routeParserMessage(event.data);
  worker.onerror = (event) => {
    // A worker that cannot boot (or crashed) fails the current load over to
    // the inline path; the next load will try a fresh worker again.
    console.warn("[ifc-console] parser worker failed", event.message || event);
    const h = activeHandlers;
    stopWorker();
    if (h) h.onWorkerLost();
  };
}

async function parseInline(buffer, handlers) {
  const seq = loadGen;
  const [{ IfcAPI }, { parseModel }] = await Promise.all([
    import("./vendor/web-ifc-api.js"),
    import("./parser.js"),
  ]);
  if (!parseInline.api) {
    const api = new IfcAPI();
    api.SetWasmPath("/viewer/static/vendor/", true);
    await api.Init();
    parseInline.api = api;
  }
  await parseModel(parseInline.api, buffer, (message) => {
    if (seq === loadGen) routeParserMessage({ seq, ...message });
  });
  return handlers;
}

function parseBuffer(buffer) {
  clearWorkerIdle();
  loadGen++;
  const seq = loadGen;
  if (workerBusy && worker) {
    // A parse is still running for a previous revision: wasm cannot be
    // interrupted, so drop the whole worker and start fresh.
    stopWorker();
  }
  return new Promise((resolve, reject) => {
    let finished = false;
    let fallbackStarted = false;
    let chunks = [];
    let maps = null;
    let tree = null;
    let coordination = null;
    const handlers = {
      onChunk: (msg) => chunks.push(msg),
      onCoordination: (msg) => { coordination = msg.matrix; },
      onProgress: (msg) => {
        if (msg.stage === "geometry") {
          showProgress(`Reading geometry: ${msg.products} elements`, null);
        } else {
          showProgress("Reading names and IDs", msg.total ? msg.resolved / msg.total : null);
        }
      },
      onMaps: (msg) => { maps = msg; },
      onTree: (msg) => { tree = msg.tree; },
      onDone: () => {
        finished = true;
        workerBusy = false;
        if (activeHandlers === handlers) activeHandlers = null;
        scheduleWorkerIdle();
        resolve({ chunks, maps, tree, coordination });
      },
      onError: (err) => {
        finished = true;
        if (activeHandlers === handlers) activeHandlers = null;
        stopWorker();
        reject(err);
      },
      onWorkerLost: () => {
        if (finished || fallbackStarted || seq !== loadGen) return;
        fallbackStarted = true;
        chunks = [];
        maps = null;
        tree = null;
        coordination = null;
        showProgress("Retrying model load", null);
        parseInline(buffer, handlers).catch(handlers.onError);
      },
    };
    activeHandlers = handlers;
    if (typeof Worker === "undefined") {
      // No worker to lose: parse the original buffer with no copy at all.
      handlers.onWorkerLost();
      return;
    }
    try {
      if (!worker) spawnWorker();
      workerBusy = true;
      // A copy is transferred, not the original: onWorkerLost still needs
      // readable bytes to fall back to the inline parser.
      const copy = buffer.slice();
      worker.postMessage({ seq, buffer: copy.buffer }, [copy.buffer]);
    } catch {
      stopWorker();
      handlers.onWorkerLost();
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden && !workerBusy) stopWorker();
});
window.addEventListener("pagehide", stopWorker);

async function fetchModelBytes(res) {
  const total = Number(res.headers.get("content-length")) || 0;
  if (!res.body || !res.body.getReader) {
    return new Uint8Array(await res.arrayBuffer());
  }
  const reader = res.body.getReader();
  // FileResponse supplies Content-Length. Fill that one allocation directly
  // instead of retaining every network chunk and then allocating the whole
  // model again at the end. Unknown/chunked responses keep the fallback list.
  let buffer = total ? new Uint8Array(total) : null;
  const parts = buffer ? null : [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (buffer && received + value.length <= buffer.length) {
      buffer.set(value, received);
    } else if (buffer) {
      // A misleading Content-Length should cost one growth, not corrupt data.
      let size = Math.max(received + value.length, buffer.length * 2, 64 * 1024);
      const grown = new Uint8Array(size);
      grown.set(buffer.subarray(0, received));
      grown.set(value, received);
      buffer = grown;
    } else {
      parts.push(value);
    }
    received += value.length;
    showProgress(
      `Downloading model: ${(received / 1_048_576).toFixed(1)} MB`,
      total ? received / total : null);
  }
  if (buffer) return received === buffer.length ? buffer : buffer.slice(0, received);
  buffer = new Uint8Array(received);
  let offset = 0;
  for (const part of parts) {
    buffer.set(part, offset);
    offset += part.length;
  }
  return buffer;
}

async function loadModel() {
  if (!viewerDocumentOpen) return;
  if (loading) {
    reloadQueued = true;
    // another rebuild follows this one; the scene is not settling yet
    sendSceneState("rebuilding");
    return;
  }
  loading = true;
  const targetRow = currentModelRow();
  loadingModelId = targetRow?.id || null;
  const targetModelId = loadingModelId;
  const targetEtag = targetRow?.etag || null;
  try {
    const cached = targetModelId ? cachedParsedModel(targetModelId, targetEtag) : null;
    if (cached) {
      showProgress("Opening cached model", null);
      const rendered = await buildScene(null, cached.parsed, targetModelId, cached.etag);
      if (rendered) {
        currentEtag = cached.etag;
        hideProgress();
        refreshStatus();
      }
      return;
    }
    showProgress("Loading model", null);
    const headers = currentEtag ? { "If-None-Match": currentEtag } : {};
    const res = await api(`/api/model.ifc${modelQuery()}`, { headers });
    if (res.status === 304) {
      hideProgress();
      hideOverlay();
      return;
    }
    if (res.status === 404) {
      hideProgress();
      disposeModel();
      renderTree(null);
      setModelInfo(null);
      updateStats();
      showOverlay(
        "No model loaded",
        "Choose a model with /file in the ifc-console terminal. This view updates automatically.",
      );
      return;
    }
    if (res.status === 413) {
      hideProgress();
      const body = await res.json().catch(() => ({}));
      showOverlay(
        "Model too large for the viewer",
        body.message || "Raise viewer.max_model_mb only if you trust this file.",
        null,
        "error",
      );
      return;
    }
    if (res.status === 401) {
      hideProgress();
      forgetStaleToken();
      showOverlay(STALE_TOKEN_TITLE, STALE_TOKEN_BODY, null, "error");
      return;
    }
    if (!res.ok) {
      hideProgress();
      showOverlay(
        "Could not fetch the model",
        `The local server returned HTTP ${res.status}.`,
        { label: "Try again", run: () => loadModel() },
        "error",
      );
      return;
    }
    const nextEtag = res.headers.get("ETag");
    // Boot the worker (and its 5.9 MB web-ifc module) while the body streams
    // in, instead of serializing the two.
    if (!worker) {
      try { spawnWorker(); } catch { /* parseBuffer falls back to inline */ }
    }
    const buffer = await fetchModelBytes(res);
    const rendered = await buildScene(buffer, null, targetModelId, nextEtag);
    if (rendered) {
      currentEtag = nextEtag;
      hideProgress();
      // the hub's change frames carry no name/schema; re-sync the top bar
      refreshStatus();
    }
  } catch (err) {
    console.error("[ifc-console] model load failed", err);
    hideProgress();
    showOverlay(
      "Could not build the 3D view",
      String(err.message || err),
      { label: "Try again", run: () => loadModel() },
      "error",
    );
  } finally {
    loading = false;
    if (reloadQueued) {
      reloadQueued = false;
      loadModel();
    } else if (sceneState !== "ready") {
      // a 304, a 404 or an error left the scene as it was; the hub must not
      // go on holding commands for a rebuild that is not happening
      sendSceneState("ready");
    }
  }
}

/**
 * Yield to the browser, whether or not anyone is watching.
 *
 * A hidden tab gets no animation frames at all, so a build that yields on one
 * stops at the first pause and only finishes when someone switches to it. The
 * timeout is the floor, not the path: a visible tab still lands on the frame.
 */
function nextFrame() {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    requestAnimationFrame(finish);
    setTimeout(finish, 100);
  });
}

async function buildScene(buffer) {
  const residentParsed = arguments[1] || null;
  const targetModelId = arguments.length > 2 ? arguments[2] : loadingModelId;
  const targetEtag = arguments.length > 3 ? arguments[3] : null;
  const switchingTabs = pendingModelTabView?.modelId === targetModelId
    || (renderedModelId !== null && renderedModelId !== targetModelId);
  if (switchingTabs && renderedModelId && selection.size) rememberCurrentSelection();
  // Live edits trigger rebuilds; carry selection and highlights across so a
  // refresh does not silently drop what the user (or the LLM) marked.
  const keepSelection = switchingTabs
    ? [...(modelSelections.get(targetModelId) || [])]
    : [...selection].map((id) => guidOf.get(id)).filter(Boolean);
  const keepPropertyGuid = keepSelection.at(-1) || null;
  const keepHighlight = switchingTabs
    ? [] : [...highlightSet].map((id) => guidOf.get(id)).filter(Boolean);
  const keepIsolate = !switchingTabs && isolateSet !== null;
  const keepUserIsolate = switchingTabs || userIsolateSet === null
    ? null
    : [...userIsolateSet].map((id) => guidOf.get(id)).filter(Boolean);
  // Every revision bump rebuilds, so throwing the measurements away here meant
  // the assistant writing one property set deleted the user's whole morning of
  // dimensions, server copy included. They are anchored to GlobalIds instead.
  const keepMeasurements = switchingTabs ? [] : measurementCarry();
  sendSceneState("rebuilding");
  showProgress(residentParsed ? "Opening cached model" : "Reading IFC model", null);

  const parsed = residentParsed || await parseBuffer(buffer);
  if (targetModelId && targetEtag) {
    cacheParsedModel(targetModelId, targetEtag, parsed);
  }
  // A user may switch again while WebAssembly is still parsing. Keep the
  // finished result warm, but never replace the newly requested tab with it.
  if (!viewerDocumentOpen || currentModelRow()?.id !== targetModelId) {
    reloadQueued = viewerDocumentOpen;
    return false;
  }
  disposeModel();
  renderedModelId = targetModelId;
  userMovedCamera = false;
  showProgress("Preparing complete model", null);
  // Yield once so the label above actually paints: everything below is one
  // long synchronous block on a big model.
  await nextFrame();
  for (const chunk of parsed.chunks) registerChunkGeometry(chunk);
  const placed = decideOrigin(parsed.chunks);
  modelBox = placed.box;
  const spatial = planSpatialGrid(placed.box, placed.verts, {
    cellVertexTarget: CELL_VERTEX_TARGET,
    stagingBudget: STAGING_VERTEX_BUDGET,
    minChunkVerts: MIN_CHUNK_VERTS,
    chunkVertexLimit: CHUNK_VERTEX_LIMIT,
    splitVerts: SPATIAL_SPLIT_VERTS,
    minCells: MIN_CELLS,
  });
  cellSize = spatial.size;
  cellFlushAt = spatial.flushAt;
  // Decided once, before the first bake: an outline is staged next to the
  // surface it belongs to, so the choice cannot change halfway through.
  edgesAffordable = placed.verts <= EDGE_VERTEX_BUDGET;
  syncEdgeSwitch();
  coordinationApplied = Array.isArray(parsed.coordination)
    && parsed.coordination.length === 16;
  coordinationMatrix = coordinationApplied
    ? new THREE.Matrix4().fromArray(parsed.coordination)
    : new THREE.Matrix4();
  refreshFrames();
  for (const axis of AXES) syncSectionRow(axis);
  let done = 0;
  for (const chunk of parsed.chunks) {
    ingestChunk(chunk, placed.layout.get(chunk), placed.uses);
    // Batching dominates the wait on a large model; give the browser a frame
    // every so often so the progress bar advances instead of freezing.
    if ((++done % 24) === 0) {
      showProgress("Preparing complete model", done / parsed.chunks.length);
      await nextFrame();
    }
  }
  finalizeSnapParts();
  finalizeAllAccumulators();
  if (parsed.maps) {
    for (let i = 0; i < parsed.maps.guidIds.length; i++) {
      const id = parsed.maps.guidIds[i];
      guidOf.set(id, parsed.maps.guids[i]);
      expressOf.set(parsed.maps.guids[i], id);
    }
  }
  renderTree(parsed.tree);
  registry.clear();
  geomUse.clear();
  updateGround();
  updateStats();
  if (switchingTabs) {
    // A document with no prior tab state starts as an uncut IFC view. A saved
    // tab view is restored once its GlobalIds exist again below.
    for (const axis of AXES) {
      section[axis].on = false;
      section[axis].t = 1;
      section[axis].flip = false;
      syncSectionRow(axis);
    }
    setSliceDepth(0);
    setProjection("perspective");
  }
  updateClipping();

  for (const guid of keepSelection) {
    const id = expressOf.get(guid);
    if (id !== undefined) selection.add(id);
  }
  highlightSet = new Set(
    keepHighlight.map((g) => expressOf.get(g)).filter((id) => id !== undefined));
  if (keepIsolate && highlightSet.size) isolateSet = new Set(highlightSet);
  if (keepUserIsolate) {
    const restored = keepUserIsolate
      .map((guid) => expressOf.get(guid))
      .filter((id) => id !== undefined);
    userIsolateSet = restored.length ? new Set(restored) : null;
  }
  applyAppearance();
  applyVisibility();
  markTreeSelection();
  updateSelectionInfo();
  updateHighlightInfo();
  if (keepPropertyGuid && expressOf.has(keepPropertyGuid)) {
    showProperties(keepPropertyGuid);
  }
  // Express ids are not stable across rebuilds, so any open result list is
  // stale; re-running it keeps the panel honest after an edit.
  refreshSearch();
  // A rebuild resets the server's selection; tell it what survived.
  sendSelection();
  // Last, once every visibility gate is settled: each end re-resolves through
  // its GlobalId, so a wall that moved carries its dimension with it, and a
  // replayed clearance sees the same model the user does.
  restoreMeasurements(keepMeasurements);
  if (switchingTabs) {
    const saved = pendingModelTabView?.modelId === targetModelId
      ? pendingModelTabView.view
      : modelTabViews.get(targetModelId) || null;
    if (pendingModelTabView?.modelId === targetModelId) pendingModelTabView = null;
    if (saved) restoreView(saved);
  }
  if (!userMovedCamera) fitTo(null);
  // A transcript click may have queued this model switch. Its element can
  // still turn out to have no mesh once parsing completes; that is a local
  // command failure, not a failed model build.
  applyPendingGuidCommand(targetModelId, { reportFailure: true });
  invalidate();
  sendSceneState("ready");
  return true;
}

// ---------------------------------------------------------------- spatial tree
const SPATIAL_TYPES = new Set([
  "IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE",
  "IFCFACILITY", "IFCBRIDGE", "IFCROAD", "IFCRAILWAY", "IFCMARINEFACILITY",
]);

function isSpatial(node) {
  return SPATIAL_TYPES.has(String(node.type || "").toUpperCase());
}

function branchElements(node) {
  const ids = new Set();
  const visit = (branch) => {
    if (elements.has(branch.expressID)) ids.add(branch.expressID);
    for (const child of branch.children || []) visit(child);
  };
  visit(node);
  return [...ids];
}

function renderTree(rootNode) {
  const container = $("tree");
  container.textContent = "";
  if (!rootNode) return;
  const list = el("ul");
  list.appendChild(buildTreeItem(rootNode, 0));
  container.appendChild(list);
}

// Children build lazily (on first expand, in slices) so a 100k-element model
// does not become a 100k-row DOM before the user ever opens a storey.
const TREE_SLICE = 250;

function buildTreeItem(node, depth) {
  const li = el("li");
  const row = el("div", "tree-row");
  const children = node.children || [];
  const spatial = isSpatial(node);
  // Project / Site / Building / Storey come pre-expanded; elements collapsed.
  const expanded = depth < 4;

  const toggle = el("button", "tree-toggle", children.length ? (expanded ? "▾" : "▸") : " ");
  toggle.type = "button";
  if (!children.length) {
    toggle.disabled = true;
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-hidden", "true");
  }
  row.appendChild(toggle);

  if (spatial && children.length) {
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.title = "toggle visibility of this branch";
    checkbox.setAttribute(
      "aria-label",
      `Show ${node._name || String(node.type || "model branch")}`,
    );
    checkbox.addEventListener("change", () => {
      for (const id of branchElements(node)) {
        if (checkbox.checked) hiddenByTree.delete(id);
        else hiddenByTree.add(id);
      }
      applyVisibility();
    });
    row.appendChild(checkbox);
  }

  const cls = String(node.type || "?");
  const label = el("button", "tree-label");
  label.type = "button";
  label.appendChild(el("span", null, node._name ? `${node._name} ` : ""));
  label.appendChild(el("span", "cls", node._name ? `(${cls})` : cls));
  label.dataset.expressId = node.expressID;
  label.title = node._name ? `${node._name} (${cls})` : cls;
  label.setAttribute("aria-pressed", "false");
  row.appendChild(label);
  li.appendChild(row);

  let kids = null;
  let built = 0;
  const buildSlice = () => {
    const frag = document.createDocumentFragment();
    const end = Math.min(children.length, built + TREE_SLICE);
    for (; built < end; built++) {
      frag.appendChild(buildTreeItem(children[built], depth + 1));
    }
    if (built < children.length) {
      const moreItem = el("li", "tree-more-item");
      const more = el("button", "tree-more", `Show ${Math.min(TREE_SLICE, children.length - built)} more (${children.length - built} hidden)`);
      more.type = "button";
      more.addEventListener("click", () => {
        moreItem.remove();
        buildSlice();
      });
      moreItem.appendChild(more);
      frag.appendChild(moreItem);
    }
    kids.appendChild(frag);
  };
  const setOpen = (open) => {
    if (!kids) return;
    if (open && !built) buildSlice();
    kids.hidden = !open;
    toggle.textContent = kids.hidden ? "▸" : "▾";
    toggle.setAttribute("aria-label", `${kids.hidden ? "Expand" : "Collapse"} ${label.title}`);
    toggle.setAttribute("aria-expanded", String(!kids.hidden));
    label.setAttribute("aria-expanded", String(!kids.hidden));
  };
  if (children.length) {
    kids = el("ul");
    li.appendChild(kids);
    setOpen(expanded);
    if (!expanded) toggle.textContent = "▸";
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      setOpen(kids.hidden);
    });
  }

  // Clicking a name only selects; framing stays on F or the view tools.
  label.addEventListener("click", (event) => {
    const additive = event.ctrlKey || event.metaKey;
    if (spatial) {
      setOpen(true); // the label is a much bigger target than the arrow
      setSelection(branchElements(node), additive);
    } else if (elements.has(node.expressID)) {
      setSelection([node.expressID], additive);
    }
  });
  label.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" && children.length) {
      event.preventDefault();
      setOpen(true);
      kids.querySelector(".tree-label")?.focus();
    } else if (event.key === "ArrowLeft" && children.length && !kids.hidden) {
      event.preventDefault();
      setOpen(false);
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const visible = [...$("tree").querySelectorAll(".tree-label")]
        .filter((item) => item.offsetParent !== null);
      const index = visible.indexOf(label);
      const target = event.key === "Home" ? 0
        : event.key === "End" ? visible.length - 1
          : Math.min(
            visible.length - 1,
            Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)),
          );
      visible[target]?.focus();
    }
  });
  return li;
}

function markTreeSelection() {
  for (const label of document.querySelectorAll(".tree-label.selected")) {
    label.classList.remove("selected");
    label.setAttribute("aria-pressed", "false");
  }
  for (const id of selection) {
    const label = document.querySelector(`.tree-label[data-express-id="${id}"]`);
    if (label) {
      label.classList.add("selected");
      label.setAttribute("aria-pressed", "true");
    }
  }
  markSearchSelection();
}

// ---------------------------------------------------------------- appearance
const SELECT_COLOR = "#4f8ff7";
const SELECT_GLOW = 0.35;
const HIGHLIGHT_GLOW = 0.35;

function styleOf(id) {
  if (highlightSet.has(id)) return [highlightColor, HIGHLIGHT_GLOW];
  if (selection.has(id)) return [SELECT_COLOR, SELECT_GLOW];
  const themed = themeByGuid.get(guidOf.get(id));
  if (themed) return [themed, 0];
  return [null, 0];
}

// Only the products whose state actually changed are restyled: a click in a
// 100k-product model writes a handful of texture bytes, not the whole scene.
function applyAppearanceTo(ids) {
  for (const id of ids) {
    const rec = elements.get(id);
    if (!rec) continue;
    const [hex, glow] = styleOf(id);
    writeElementStyle(rec.index, hex, glow);
  }
  commitStyles();
}

function applyAppearance() {
  applyAppearanceTo(elements.keys());
}

function isolatedOut(id) {
  return (isolateSet !== null && !isolateSet.has(id))
    || (userIsolateSet !== null && !userIsolateSet.has(id));
}

function isElementShown(id) {
  return !hiddenByTree.has(id) && !hiddenManual.has(id) && !isolatedOut(id);
}

/**
 * Whether an element is off screen only because something else was isolated.
 *
 * That is the case worth ghosting: an isolated duct with the building around
 * it faded still says where in the building it is. An element the user or the
 * tree deliberately hid was meant to go away, and it does.
 */
function isGhosted(id) {
  const selectionContext = selection.size > 0 && !selection.has(id);
  return ghostContext && (isolatedOut(id) || selectionContext)
    && !hiddenByTree.has(id) && !hiddenManual.has(id);
}

/** Ghosted geometry is still visible geometry: it must remain selectable and
 * measurable so the solid highlight can move through the faded context. */
function isElementPickable(id) {
  return isElementShown(id) || isGhosted(id);
}

let hiddenCount = 0;
let ghostCount = 0;
let ghostContext = false;
function applyVisibility() {
  hiddenCount = 0;
  ghostCount = 0;
  for (const [id, rec] of elements) {
    const shown = isElementShown(id);
    if (!shown) hiddenCount++;
    let level = 0;
    if (shown) level = 255;
    else if (isGhosted(id)) {
      level = GHOST_LEVEL;
      ghostCount++;
    }
    stateData[rec.index * 4] = level;
  }
  commitStyles();
  updateVisibilityInfo();
  // Isolation, hiding and ghosting are all view state a reader has to see; it
  // changed from a button, from the tree and from a command with no push.
  scheduleViewerContext("visibility");
}

/** Turn ghosted context on or off, and repaint what that changes. */
function setGhostContext(on) {
  const want = on !== false;
  if (want === ghostContext) return ghostContext;
  ghostContext = want;
  const box = $("tool-ghost");
  if (box) box.checked = ghostContext;
  uiState.ghost = ghostContext;
  saveUi();
  applyVisibility();
  return ghostContext;
}

// ---------------------------------------------------------------- selection
function setSelection(ids, additive) {
  const options = arguments[2] || {};
  const touched = new Set(selection);  // what leaves the selection restyles too
  if (!additive) selection.clear();
  for (const id of ids) {
    if (additive && selection.has(id)) selection.delete(id);
    else selection.add(id);
    touched.add(id);
  }
  applyAppearanceTo(touched);
  if (ghostContext) applyVisibility();
  markTreeSelection();
  updateSelectionInfo();
  if (options.remember !== false) rememberCurrentSelection();
  syncModelSelectionCounts();
  if (options.publish !== false) sendSelection();
  const last = ids[ids.length - 1];
  if (last !== undefined && selection.has(last) && guidOf.has(last)) {
    showProperties(guidOf.get(last));
  } else if (!selection.size) {
    clearProperties();
  }
}

function updateSelectionInfo() {
  const n = selection.size;
  const status = $("sel-info");
  const propertiesTab = $("props-panel-tab");
  status.dataset.selectionCount = String(n);
  propertiesTab.classList.toggle("has-context", n > 0);
  propertiesTab.title = n ? `Open properties for ${n} selected` : "Open properties panel";
  propertiesTab.setAttribute(
    "aria-label",
    n ? `Open properties for ${n} selected` : "Open properties panel",
  );
  updateToolButtons();
  if (!n) {
    status.textContent = "No selection";
    scheduleViewerContext("selection");
    renderViewerFilters();
    return;
  }
  const shown = [...selection].slice(0, 3)
    .map((id) => guidOf.get(id) || `#${id}`).join(", ");
  status.textContent = `${n} selected · ${shown}${n > 3 ? ", …" : ""}`;
  scheduleViewerContext("selection");
  renderViewerFilters();
}

function updateHighlightInfo() {
  const n = highlightSet.size;
  $("hl-info").textContent = n ? `${n} highlighted · ${highlightColor}` : "";
  // The clear control only earns space when there is something to clear.
  $("btn-clear-hl").hidden = n === 0;
  renderViewerFilters();
}

// The hub keeps the first 500; sending more just inflates the frame.
const SELECTION_WIRE_MAX = 500;

function sendSelection() {
  const guids = viewerDocumentOpen
    ? rememberCurrentSelection().slice(0, SELECTION_WIRE_MAX)
    : [];
  const row = viewerDocumentOpen ? currentModelRow() : null;
  const selections = modelSelectionRows().map((item) => ({
    model_id: item.model_id,
    guids: item.guids.slice(0, SELECTION_WIRE_MAX),
  }));
  wsSend({ type: "selection", guids, model_id: row ? row.id : null, selections });
}

// GPU pick: render the pixel under the cursor with the id-encoding override
// material and decode which element it belongs to.
function pickElementAt(clientX, clientY) {
  if (!elements.size) return null;
  const scaled = ensureFullResolution();
  const rect = canvas.getBoundingClientRect();
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const x = Math.floor(((clientX - rect.left) / rect.width) * size.x);
  const y = Math.floor(((clientY - rect.top) / rect.height) * size.y);

  const prevBackground = scene.background;
  const prevOverride = scene.overrideMaterial;
  const gridWasVisible = grid.visible;
  const axesWasVisible = axes ? axes.visible : false;
  const measureWasVisible = measureGroup.visible;
  const snapWasVisible = snapGroup.visible;
  const edgesWereVisible = edgeRoot.visible;
  const sectionHelperWasVisible = sectionHelperRoot.visible;
  const prevTarget = renderer.getRenderTarget();
  const prevClearColor = renderer.getClearColor(new THREE.Color()).clone();
  const prevClearAlpha = renderer.getClearAlpha();
  try {
    scene.background = null;
    grid.visible = false;
    if (axes) axes.visible = false;
    // markers carry no element index, so leaving them in makes a click on one
    // decode as element 0; the snap glyph sits under the cursor by definition
    measureGroup.visible = false;
    snapGroup.visible = false;
    // An outline sits exactly on the surface it outlines, so at a silhouette
    // the winner of the depth test is a coin toss; the surface answers.
    edgeRoot.visible = false;
    sectionHelperRoot.visible = false;
    scene.overrideMaterial = pickMaterial;
    camera.setViewOffset(size.x, size.y, x, y, 1, 1);
    renderer.setRenderTarget(pickTarget);
    renderer.setClearColor(0x000000, 0);
    renderer.clear();
    renderer.render(scene, camera);
    renderer.readRenderTargetPixels(pickTarget, 0, 0, 1, 1, pickBuffer);
  } finally {
    renderer.setRenderTarget(prevTarget);
    renderer.setClearColor(prevClearColor, prevClearAlpha);
    camera.clearViewOffset();
    scene.overrideMaterial = prevOverride;
    scene.background = prevBackground;
    grid.visible = gridWasVisible;
    if (axes) axes.visible = axesWasVisible;
    measureGroup.visible = measureWasVisible;
    snapGroup.visible = snapWasVisible;
    edgeRoot.visible = edgesWereVisible;
    sectionHelperRoot.visible = sectionHelperWasVisible;
    // Only a resolution change owes the canvas a redraw; the pass itself never
    // touched it.
    if (scaled) invalidate();
  }

  const encoded = pickBuffer[0] * 65536 + pickBuffer[1] * 256 + pickBuffer[2];
  if (!encoded) return null;
  const expressID = elementsByIndex[encoded - 1];
  return expressID === undefined ? null : expressID;
}

// Click-to-select with a small movement threshold so orbiting never selects.
const DRAG_THRESHOLD = 6;
let downAt = null;
let downPointerId = null;
let latestMeasurePointer = null;
let measurePressHit = null;
let measureDragActive = false;
let measureDragAddedStart = false;
const activeTouchPointers = new Set();
let touchNavigationActive = false;
canvas.addEventListener("pointerdown", (e) => {
  // The viewport has to hold focus for arrow-key panning, but :focus-visible
  // cannot tell this focus() from a Tab press and drew a ring around the
  // model on every click. Mark it so the ring is a keyboard affordance only.
  canvas.dataset.quietFocus = "1";
  canvas.focus({ preventScroll: true });
  if (e.pointerType === "touch") {
    activeTouchPointers.add(e.pointerId);
    if (activeTouchPointers.size > 1) {
      // OrbitControls owns a two-finger dolly/pan. Cancel the measurement
      // gesture before either touch can turn that camera move into a point.
      touchNavigationActive = true;
      if (measureDragAddedStart) undoPendingPoint();
      downAt = null;
      downPointerId = null;
      measurePressHit = null;
      measureDragActive = false;
      measureDragAddedStart = false;
      latestMeasurePointer = null;
      canvas.classList.remove("is-dragging");
      if (measureMode) clearSnapPreview();
      return;
    }
    // Once a two-finger gesture starts, its remaining finger still belongs to
    // navigation until every touch is up.
    if (touchNavigationActive) return;
  }
  downAt = [e.clientX, e.clientY];
  downPointerId = e.pointerId;
  measurePressHit = measureMode && e.button === 0 && !e.altKey
    ? (cachedMeasurePoint(e.clientX, e.clientY)
      || measurePointAt(e.clientX, e.clientY))
    : null;
  measureDragActive = false;
  measureDragAddedStart = false;
  canvas.classList.remove("is-dragging");
  if (measureMode) clearSnapPreview();
  // Every gesture starts from the surface under the cursor: orbit, pan and
  // zoom all scale from the pivot distance, so pan and orbit re-seat it too.
  if (!measureMode || e.button === 2) repivotIfStale(e.clientX, e.clientY);
});

const _pivotDir = new THREE.Vector3();
const _pivotSeat = new THREE.Vector3();

// Zooming to the cursor walks the camera away from wherever the model was
// framed, leaving the orbit pivot floating somewhere else. The fix cannot be
// to copy the cursor's 3D point: the camera always looks at the target, so an
// off-axis pivot snaps the view sideways. Instead the pivot slides ALONG the
// view axis to the depth of the surface under the cursor: the view does not
// change at all, and orbit, pan and zoom now scale to what is in front of you.
function repivotIfStale(clientX, clientY) {
  const point = surfacePointAt(clientX, clientY);
  if (!point) return;
  camera.getWorldDirection(_pivotDir);
  const depth = _pivotSeat.copy(point).sub(camera.position).dot(_pivotDir);
  if (!(depth > 1e-6)) return;
  _pivotSeat.copy(camera.position).addScaledVector(_pivotDir, depth);
  // already there; a micro-move would only fight the controls
  if (_pivotSeat.distanceTo(controls.target) < depth * 0.02) return;
  controls.target.copy(_pivotSeat);
  controls.update();
  grid.position.set(_pivotSeat.x, groundY, _pivotSeat.z);
  invalidate();
}

// After a run of cursor-zoom the pivot depth is stale even without a click;
// one depth probe after the wheel settles keeps the next orbit anchored.
let wheelRepivot = 0;
canvas.addEventListener("wheel", (e) => {
  clearTimeout(wheelRepivot);
  wheelRepivot = setTimeout(() => {
    if (!downAt && !measureMode) repivotIfStale(e.clientX, e.clientY);
  }, 160);
}, { passive: true });

canvas.addEventListener("keydown", () => { delete canvas.dataset.quietFocus; });

/**
 * Whether a keystroke belongs to the viewport rather than to a text field.
 *
 * Single-letter shortcuts are only shortcuts where nobody is typing: measure
 * mode used to read S, X, Y, Z and Backspace off the window, so writing the
 * word "sxyz" into the chat composer toggled snapping, set an invisible axis
 * lock and ate the delete key.
 */
function isShortcutSurface(target) {
  return target === document.body
    || target === canvas
    || Boolean(target?.closest?.(".tree-label, .search-hit"));
}

window.addEventListener("keydown", (e) => {
  if (e.defaultPrevented) return;
  if (!measureMode || e.ctrlKey || e.metaKey || e.altKey) return;
  if (!isShortcutSurface(e.target)) return;
  const key = e.key.toLowerCase();
  if (key === "s") {
    setSnap(!snapEnabled);
    return;
  }
  if (key === "backspace") {
    // undo the last click instead of editing whatever holds focus
    if (undoPendingPoint()) e.preventDefault();
    return;
  }
  if (!["distance", "path"].includes(measureKind)) return;
  if (!["x", "y", "z"].includes(key)) return;
  // A toggle, not a hold: locking stays on across clicks until the same key,
  // another axis, or leaving measure mode. Holding a key while aiming a
  // two-click measurement was the two-handed version of this.
  axisLock = axisLock === key ? "" : key;
  renderMeasurements();
});
canvas.addEventListener("blur", () => { delete canvas.dataset.quietFocus; });
canvas.addEventListener("pointerleave", () => {
  latestMeasurePointer = null;
  clearSnapPreview();
});
// A preview is a claim about where the cursor is pointing, and moving the
// camera invalidates it before the pointer has moved at all.
controls.addEventListener("change", () => clearSnapPreview());
controls.addEventListener("end", () => {
  if (measureMode && latestMeasurePointer) {
    queueSnapPreview(latestMeasurePointer[0], latestMeasurePointer[1]);
  }
});
canvas.addEventListener("pointermove", (e) => {
  if (e.pointerType === "touch" && touchNavigationActive) return;
  latestMeasurePointer = [e.clientX, e.clientY];
  if (measureMode && !downAt) queueSnapPreview(e.clientX, e.clientY);
  if (!downAt || e.pointerId !== downPointerId) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  if (moved > DRAG_THRESHOLD) {
    canvas.classList.add("is-dragging");
    if (measureMode && (e.buttons & 1) && measureKind === "distance") {
      if (!pending.length && measurePressHit) {
        handleMeasureClick(downAt[0], downAt[1], measurePressHit);
        measureDragAddedStart = pending.length === 1;
      }
      measureDragActive = pending.length === 1;
      if (measureDragActive) queueSnapPreview(e.clientX, e.clientY);
    }
  }
});
canvas.addEventListener("pointerup", (e) => {
  canvas.classList.remove("is-dragging");
  if (e.pointerType === "touch") {
    const wasNavigation = touchNavigationActive;
    activeTouchPointers.delete(e.pointerId);
    if (!activeTouchPointers.size) touchNavigationActive = false;
    if (wasNavigation) {
      downAt = null;
      downPointerId = null;
      measurePressHit = null;
      measureDragActive = false;
      measureDragAddedStart = false;
      return;
    }
  }
  if (!downAt || e.pointerId !== downPointerId) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  downAt = null;
  downPointerId = null;
  const pressHit = measurePressHit;
  const draggedMeasurement = measureDragActive;
  measurePressHit = null;
  measureDragActive = false;
  measureDragAddedStart = false;
  if (e.button !== 0) return;

  if (measureMode) {
    if (e.altKey) {
      const id = pickElementAt(e.clientX, e.clientY);
      const dimensions = id === null ? null : elementDimensions(id);
      if (dimensions) {
        recordMeasurement(
          "dimensions", dimensions, dimensions.guid || `#${id}`,
          "", null, centreAnchor(dimensions));
      }
      return;
    }
    if (draggedMeasurement || moved <= DRAG_THRESHOLD) {
      handleMeasureClick(e.clientX, e.clientY, draggedMeasurement ? null : pressHit);
    } else {
      // Measurement owns the primary button, so a shaky press still places a
      // point even when it crossed the selection/orbit drag threshold.
      handleMeasureClick(e.clientX, e.clientY);
    }
    return;
  }

  if (moved > DRAG_THRESHOLD) return;

  const hit = pickElementAt(e.clientX, e.clientY);
  const additive = e.ctrlKey || e.metaKey || e.shiftKey;
  if (hit !== null) setSelection([hit], additive);
  else if (!additive) setSelection([], false);
});

// Double-click frames the element you clicked: the fastest way from a
// site-scale view to working distance on one small part. Off the model it
// re-seats the pivot depth without moving the camera.
canvas.addEventListener("dblclick", (e) => {
  if (measureMode) {
    // pointerup already added a point for each half of the pair, so the second
    // one goes back before the outline closes on the first
    const duplicateWasRejected = measureProblem.startsWith("That point is the same");
    if ((measureKind === "area" || measureKind === "path") && !duplicateWasRejected) {
      undoPendingPoint();
    }
    measureProblem = "";
    if (finishOpenMeasurement()) renderMeasurements();
    return;
  }
  const id = pickElementAt(e.clientX, e.clientY);
  if (id !== null && id !== undefined) {
    fitTo([id]);
    return;
  }
  repivotIfStale(e.clientX, e.clientY);
});
canvas.addEventListener("pointercancel", (e) => {
  if (e.pointerType === "touch") {
    activeTouchPointers.delete(e.pointerId);
    if (!activeTouchPointers.size) touchNavigationActive = false;
  }
  if (measureDragAddedStart) undoPendingPoint();
  downAt = null;
  downPointerId = null;
  measurePressHit = null;
  measureDragActive = false;
  measureDragAddedStart = false;
  canvas.classList.remove("is-dragging");
});
window.addEventListener("blur", () => {
  if (measureDragAddedStart) undoPendingPoint();
  downAt = null;
  downPointerId = null;
  measurePressHit = null;
  measureDragActive = false;
  measureDragAddedStart = false;
  activeTouchPointers.clear();
  touchNavigationActive = false;
  canvas.classList.remove("is-dragging");
});

// ---------------------------------------------------------------- camera
const _fitBox = new THREE.Box3();
function boundsOf(ids) {
  _fitBox.makeEmpty();
  let any = false;
  if (ids) {
    for (const id of ids) {
      const rec = elements.get(id);
      if (!rec || !isFinite(rec.box[0])) continue;
      _fitBox.min.x = Math.min(_fitBox.min.x, rec.box[0]);
      _fitBox.min.y = Math.min(_fitBox.min.y, rec.box[1]);
      _fitBox.min.z = Math.min(_fitBox.min.z, rec.box[2]);
      _fitBox.max.x = Math.max(_fitBox.max.x, rec.box[3]);
      _fitBox.max.y = Math.max(_fitBox.max.y, rec.box[4]);
      _fitBox.max.z = Math.max(_fitBox.max.z, rec.box[5]);
      any = true;
    }
  } else {
    for (const [id, rec] of elements) {
      if (!isElementShown(id) || !isFinite(rec.box[0])) continue;
      _fitBox.min.x = Math.min(_fitBox.min.x, rec.box[0]);
      _fitBox.min.y = Math.min(_fitBox.min.y, rec.box[1]);
      _fitBox.min.z = Math.min(_fitBox.min.z, rec.box[2]);
      _fitBox.max.x = Math.max(_fitBox.max.x, rec.box[3]);
      _fitBox.max.y = Math.max(_fitBox.max.y, rec.box[4]);
      _fitBox.max.z = Math.max(_fitBox.max.z, rec.box[5]);
      any = true;
    }
  }
  return any ? _fitBox : null;
}

function frameBox(box, direction, padding = 1) {
  const center = box.getCenter(new THREE.Vector3());
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  sphere.radius *= padding > 0 ? padding : 1;
  // The standoff is a perspective question even in a parallel projection:
  // it is what puts the model inside the depth range, and the ortho camera
  // has no field of view to ask.
  const verticalFov = THREE.MathUtils.degToRad(perspectiveCamera.fov);
  const horizontalFov = 2 * Math.atan(
    Math.tan(verticalFov / 2) * perspectiveCamera.aspect);
  const halfFov = Math.max(0.01, Math.min(verticalFov, horizontalFov) / 2);
  const distance = Math.max(sphere.radius, 1) / Math.sin(halfFov) * 1.1;
  if (isOrtho()) {
    // Fitting a parallel projection is a zoom, not a move.
    orthoHeight = Math.max(sceneSpan(), 1e-3);
    applyProjectionShape();
    const aspect = viewportWidth / Math.max(viewportHeight, 1);
    const fit = Math.max(sphere.radius * 2.2, 1e-4);
    camera.zoom = Math.min(orthoHeight / fit, (orthoHeight * aspect) / fit);
    camera.updateProjectionMatrix();
  }
  const dir = direction
    || camera.position.clone().sub(controls.target).normalize();
  camera.position.copy(center.clone().add(dir.multiplyScalar(distance)));
  controls.target.copy(center);
  grid.position.set(center.x, groundY, center.z);
  // 20mm on a 200m site, well under a millimetre in a single room: close
  // enough to sit inside a connection detail, far enough that pan and orbit
  // still have a radius to be proportional to.
  // The zoom floor scales with the model but stays close enough to inspect a
  // bolt on a site-scale file: 1 cm at a kilometre, never above a millimetre
  // of slack on small models.
  controls.minDistance = Math.max(sceneSpan() * 1e-5, 1e-3);
  applyNearFar();
  invalidate();
}

function fitTo(ids) {
  const box = boundsOf(ids);
  if (!box) return;
  frameBox(box, null);
}

// View presets in three.js coords; web-ifc outputs Y-up (IFC Z becomes Y).
const VIEW_DIRECTIONS = {
  top: [0, 1, 0.0001],
  bottom: [0, -1, 0.0001],
  front: [0, 0, 1],
  back: [0, 0, -1],
  left: [-1, 0, 0],
  right: [1, 0, 0],
  iso: [1, 0.8, 1],
};

function setView(view, ids) {
  const dir = VIEW_DIRECTIONS[view];
  if (!dir) return;
  const box = boundsOf(ids) || boundsOf(null);
  if (!box) return;
  frameBox(box, new THREE.Vector3(...dir).normalize());
}

function fitTargetIds(fit) {
  if (fit === "selection" && selection.size) return [...selection];
  if (fit === "highlighted" && highlightSet.size) return [...highlightSet];
  return null;
}

// ------------------------------------------------------------- camera control
// An agent that can read the camera and set it can compose a plan view, an
// elevation and a walkthrough out of two primitives, so both halves speak the
// model's own axes in metres and both report the same shape.
const CAMERA_TRANSITION_MS = 420;
// A millimetre: below this the eye is at the pivot and there is no view.
const CAMERA_MIN_REACH = 1e-3;
let cameraTween = null;

/** Everything about the current view, in the model's own frame. */
function cameraState() {
  camera.updateMatrixWorld();
  const up = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1);
  const ortho = isOrtho();
  return {
    position: toModelPoint(camera.position),
    target: toModelPoint(controls.target),
    up: toModelDirection(up),
    fov: ortho ? null : camera.fov,
    projection: ortho ? "orthographic" : "perspective",
    ortho_height: ortho
      ? (camera.top - camera.bottom) / Math.max(camera.zoom, 1e-9) : null,
    distance: camera.position.distanceTo(controls.target),
    world_per_pixel: worldPerPixel(controls.target),
  };
}

/** The scene-space pose a transition interpolates, before or after a move. */
function cameraPose() {
  return {
    position: camera.position.toArray(),
    target: controls.target.toArray(),
    up: camera.up.toArray(),
    zoom: camera.zoom,
    fov: perspectiveCamera.fov,
  };
}

const _tweenUp = new THREE.Vector3();

/**
 * Ease from where the camera was to where a command already put it.
 *
 * The command applies its move immediately and reports the settled state, so
 * the answer never describes a frame halfway through; the tween then rewinds
 * the drawing to the old pose and glides back. Nothing renders in between, so
 * there is no visible jump.
 */
function beginCameraTransition(from) {
  const to = cameraPose();
  if (motionPreference.matches) return;
  const moved = _tweenUp.fromArray(from.position).distanceTo(camera.position)
    + _tweenUp.fromArray(from.target).distanceTo(controls.target);
  if (!(moved > CAMERA_MIN_REACH) && from.zoom === to.zoom && from.fov === to.fov) return;
  cameraTween = { from, to, start: performance.now(), ms: CAMERA_TRANSITION_MS };
}

function stepCameraTween(now) {
  const { from, to, start, ms } = cameraTween;
  const t = Math.min(1, Math.max(0, (now - start) / ms));
  // smoothstep: no velocity at either end, so it leaves and arrives quietly
  const e = t * t * (3 - 2 * t);
  const mix = (a, b) => a + (b - a) * e;
  camera.position.set(
    mix(from.position[0], to.position[0]),
    mix(from.position[1], to.position[1]),
    mix(from.position[2], to.position[2]));
  controls.target.set(
    mix(from.target[0], to.target[0]),
    mix(from.target[1], to.target[1]),
    mix(from.target[2], to.target[2]));
  _tweenUp.set(
    mix(from.up[0], to.up[0]), mix(from.up[1], to.up[1]), mix(from.up[2], to.up[2]));
  if (_tweenUp.lengthSq() > 1e-12) camera.up.copy(_tweenUp.normalize());
  // Zoom is a ratio, so it is interpolated as one; a linear ramp from 0.01 to
  // 10 spends almost the whole glide arriving.
  if (from.zoom !== to.zoom && from.zoom > 0 && to.zoom > 0) {
    camera.zoom = Math.exp(mix(Math.log(from.zoom), Math.log(to.zoom)));
    camera.updateProjectionMatrix();
  }
  if (from.fov !== to.fov) {
    perspectiveCamera.fov = mix(from.fov, to.fov);
    perspectiveCamera.updateProjectionMatrix();
  }
  if (t >= 1) cameraTween = null;
  invalidate();
}

/** A [x, y, z] argument in model axes, or null when the caller omitted it. */
function modelTriple(value, name) {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value) || value.length !== 3 || !value.every(Number.isFinite)) {
    throw new Error(`${name} must be [x, y, z] in model axes, in metres`);
  }
  return value;
}

/**
 * Put the camera exactly where a caller asked for it.
 *
 * Position and target are the whole of a view; up is the roll, and it defaults
 * to the model's own up rather than the viewport's so a caller never has to
 * know which way web-ifc turned the file.
 */
function applyCameraCommand(command) {
  const before = cameraPose();
  const wasOrtho = isOrtho();
  if (command.projection !== undefined && command.projection !== null) {
    setProjection(command.projection);
  }
  const target = command.target === undefined || command.target === null
    ? controls.target.clone()
    : toScenePoint(modelTriple(command.target, "target"));
  const position = command.position === undefined || command.position === null
    ? camera.position.clone()
    : toScenePoint(modelTriple(command.position, "position"));
  if (position.distanceTo(target) < CAMERA_MIN_REACH) {
    throw new Error(
      "The camera position and target are the same point; they must be at least a millimetre apart",
    );
  }
  // Omitting up means the model's own up, not whatever the last command left
  // behind: a caller that names a position and a target has described a view,
  // and it should be the same view every time. A plan looks straight down that
  // axis, and lookAt's own convention then puts the model's +Y up the screen,
  // which is what a floor plan means by north.
  const up = command.up === undefined || command.up === null
    ? toSceneDirection([0, 0, 1])
    : toSceneDirection(modelTriple(command.up, "up"));
  if (command.up !== undefined && command.up !== null) {
    const view = position.clone().sub(target).normalize();
    if (up.lengthSq() < 1e-12 || Math.abs(up.dot(view)) > 0.999) {
      throw new Error("up cannot be zero, or parallel to the direction of view");
    }
  }
  perspectiveCamera.up.copy(up);
  orthographicCamera.up.copy(up);
  if (command.fov !== undefined && command.fov !== null) {
    const fov = Number(command.fov);
    if (!(fov > 1 && fov < 179)) throw new Error("fov must be between 1 and 179 degrees");
    perspectiveCamera.fov = fov;
  }
  camera.position.copy(position);
  controls.target.copy(target);
  applyProjectionShape();
  camera.lookAt(target);
  applyNearFar();
  controls.update();
  userMovedCamera = true;
  cameraTween = null;
  // A projection swap moves the eye by a different rule at each end, so there
  // is no pose to interpolate; it lands directly.
  if (command.transition !== false && wasOrtho === isOrtho()) beginCameraTransition(before);
  invalidate();
  scheduleViewerContext("camera");
  return cameraState();
}

/**
 * Frame something and say what was framed, without touching the selection.
 *
 * This is the one way an agent frames anything: fitting used to be reachable
 * only as a side effect of selecting or of taking a screenshot.
 */
function fitCommand(command) {
  const before = cameraPose();
  const missing = [];
  let ids = null;
  if (command.selection === true) {
    if (!selection.size) throw new Error("Nothing is selected to fit");
    ids = [...selection];
  } else if (Array.isArray(command.guids) && command.guids.length) {
    ids = [];
    for (const guid of command.guids) {
      const id = expressOf.get(guid);
      const rec = id === undefined ? null : elements.get(id);
      if (rec && isFinite(rec.box[0])) ids.push(id);
      else missing.push(guid);
    }
    if (!ids.length) throw new Error("None of those elements have geometry in this model");
  }
  const view = command.view === undefined || command.view === null
    ? "" : String(command.view);
  if (view && !VIEW_DIRECTIONS[view]) {
    throw new Error(
      `Unknown view ${view}; use one of ${Object.keys(VIEW_DIRECTIONS).join(", ")}`,
    );
  }
  const box = boundsOf(ids);
  if (!box) throw new Error("There is nothing on screen to fit");
  const asked = Number(command.padding);
  const padding = Number.isFinite(asked) && asked > 0 ? asked : 1;
  frameBox(
    box, view ? new THREE.Vector3(...VIEW_DIRECTIONS[view]).normalize() : null, padding);
  userMovedCamera = true;
  cameraTween = null;
  if (command.transition === true) beginCameraTransition(before);
  scheduleViewerContext("camera");
  let framed = 0;
  let hidden = 0;
  if (ids) {
    framed = ids.length;
    // The camera is aimed at where they are whether or not they are on screen,
    // so say how many of them the frame cannot actually show.
    for (const id of ids) if (!isElementShown(id)) hidden++;
  } else {
    for (const [id, rec] of elements) if (isElementShown(id) && isFinite(rec.box[0])) framed++;
  }
  return { framed, hidden, missing, camera: cameraState() };
}

// ---------------------------------------------------------------- sectioning
const AXIS_INDEX = { x: 0, y: 1, z: 2 };

// While a cut slider moves, draw the real plane through the model. A number
// alone cannot show which storey or bay is about to be removed.
const sectionHelperRoot = new THREE.Group();
sectionHelperRoot.name = "section-plane-helper";
sectionHelperRoot.visible = false;
scene.add(sectionHelperRoot);
// CAD axis colors are attached to the IFC/model axis, not whichever scene
// axis it happens to map onto after coordination.
const SECTION_HELPER_COLORS = { x: 0xe56b70, y: 0x67b886, z: 0x6fa8d8 };
const sectionHelpers = {};
for (const axis of AXES) {
  const geometry = new THREE.PlaneGeometry(1, 1);
  const fill = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
    color: SECTION_HELPER_COLORS[axis],
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.16,
    depthTest: false,
    depthWrite: false,
  }));
  const border = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry),
    new THREE.LineBasicMaterial({
      color: SECTION_HELPER_COLORS[axis], transparent: true, opacity: 0.9,
      depthTest: false, depthWrite: false,
    }),
  );
  fill.renderOrder = 990;
  border.renderOrder = 991;
  const group = new THREE.Group();
  group.add(fill, border);
  group.visible = false;
  sectionHelperRoot.add(group);
  sectionHelpers[axis] = group;
}
let sectionHelperAxis = null;
let sectionHelperTimer = 0;

function axisRange(axis) {
  const k = AXIS_INDEX[axis];
  if (!modelBox) return [0, 1];
  const pad = Math.max((modelBox[k + 3] - modelBox[k]) * 0.001, 1e-4);
  return [modelBox[k] - pad, modelBox[k + 3] + pad];
}

function showSectionHelper(axis) {
  if (!modelBox || !section[axis]?.on) return;
  clearTimeout(sectionHelperTimer);
  sectionHelperAxis = axis;
  const spans = [
    Math.max(modelBox[3] - modelBox[0], 1e-3),
    Math.max(modelBox[4] - modelBox[1], 1e-3),
    Math.max(modelBox[5] - modelBox[2], 1e-3),
  ];
  const centres = [
    (modelBox[0] + modelBox[3]) / 2,
    (modelBox[1] + modelBox[4]) / 2,
    (modelBox[2] + modelBox[5]) / 2,
  ];
  const [low, high] = axisRange(axis);
  const at = low + (high - low) * section[axis].t;
  for (const [name, helper] of Object.entries(sectionHelpers)) {
    helper.visible = name === axis;
  }
  const helper = sectionHelpers[axis];
  const modelAxis = MODEL_OF_SCENE[axis] || axis;
  const color = SECTION_HELPER_COLORS[modelAxis];
  helper.children[0].material.color.setHex(color);
  helper.children[1].material.color.setHex(color);
  helper.position.set(centres[0], centres[1], centres[2]);
  helper.rotation.set(0, 0, 0);
  if (axis === "x") {
    helper.position.x = at;
    helper.rotation.y = Math.PI / 2;
    helper.scale.set(spans[2] * 1.04, spans[1] * 1.04, 1);
  } else if (axis === "y") {
    helper.position.y = at;
    helper.rotation.x = -Math.PI / 2;
    helper.scale.set(spans[0] * 1.04, spans[2] * 1.04, 1);
  } else {
    helper.position.z = at;
    helper.scale.set(spans[0] * 1.04, spans[1] * 1.04, 1);
  }
  sectionHelperRoot.visible = true;
  invalidate();
}

function hideSectionHelper(delay = 0) {
  clearTimeout(sectionHelperTimer);
  const hide = () => {
    sectionHelperAxis = null;
    sectionHelperRoot.visible = false;
    invalidate();
  };
  if (delay > 0) sectionHelperTimer = setTimeout(hide, delay);
  else hide();
}

function updateClipping() {
  // A cached measurement preview was sampled against the old cut surfaces.
  if (measureMode) clearSnapPreview();
  activeClipPlanes.length = 0;
  for (const axis of AXES) {
    const state = section[axis];
    if (!state.on) continue;
    const [low, high] = axisRange(axis);
    const at = low + (high - low) * state.t;
    const plane = clipPlanes[axis];
    const sign = state.flip ? -1 : 1;
    // three.js keeps fragments where normal . p + constant >= 0, so the
    // unflipped normal (-1 on the axis) keeps everything below the cut.
    plane.normal.copy(AXIS_NORMALS[axis]).multiplyScalar(sign);
    plane.constant = sign * at;
    activeClipPlanes.push(plane);
    if (sliceDepth > 0) {
      // The same cut from the other side, one slice further along, which is
      // what turns a half-space into a floor plan.
      const back = clipPlanesBack[axis];
      back.normal.copy(AXIS_NORMALS[axis]).multiplyScalar(-sign);
      back.constant = -sign * (at - sign * sliceDepth);
      activeClipPlanes.push(back);
    }
  }
  const count = activeClipPlanes.length;
  renderer.localClippingEnabled = count > 0;
  // the 1x1 pick passes clip too, so a cut-away face is neither selectable
  // nor measurable
  for (const mat of [pickMaterial, depthMaterial, ...patchedMaterials]) {
    if (mat.clippingPlanes !== activeClipPlanes) mat.clippingPlanes = activeClipPlanes;
    // the plane count is part of the shader, so a change means a recompile
    if (mat.userData.ifcClipCount !== count) {
      mat.userData.ifcClipCount = count;
      mat.needsUpdate = true;
    }
  }
  if (sectionHelperAxis) showSectionHelper(sectionHelperAxis);
  updateVisibilityInfo();
  scheduleViewerContext("section");
  invalidate();
}

/** The cuts as numbers in the model's own axes, not slider fractions. */
function sectionState() {
  const out = { slice: sliceDepth, axes: {} };
  for (const axis of AXES) {
    const state = section[axis];
    const [low, high] = axisRange(axis);
    const scenePosition = low + (high - low) * state.t;
    const name = MODEL_OF_SCENE[axis];
    // A cut that keeps the lower side of a scene axis keeps the upper side of
    // a model axis that runs the other way.
    const below = axisFrame[name].sign < 0 ? state.flip : !state.flip;
    out.axes[name] = {
      on: state.on,
      at: toModelAxis(axis, scenePosition),
      keep: below ? "below" : "above",
    };
  }
  return out;
}

function sectionActive() {
  return AXES.some((axis) => section[axis].on);
}

// ---------------------------------------------------------------- measurement
const measureGroup = new THREE.Group();
measureGroup.name = "measurements";
scene.add(measureGroup);

const measurements = [];
let measureMode = false;
let measureCardDismissed = false;
let measurePanelPinned = false;
// Distance wants two clicks, angle three, area as many as the outline has.
// One pending list serves all three; the tool says when it is satisfied.
const MEASURE_KINDS = { distance: 2, path: 0, angle: 3, area: 0 };
// Two outline points closer than a micrometre are one point clicked twice.
const AREA_MIN_EDGE_SQ = 1e-12;
const AREA_CLOSE_PX = 13;
const MAX_MEASURE_POINTS = 200;
let measureKind = "distance";
const pending = [];
let measureProblem = "";
// Toggled while measuring (press X, Y or Z). IFC is Z-up and three.js is
// Y-up, so the lock names are the model's axes and axisFrame maps them.
let axisLock = "";
// CAD convention: X red, Y green, Z blue, in the model's axes.
const AXIS_COLORS = { x: 0xe0645c, y: 0x69b56b, z: 0x5a8fd6 };

/** The point a length click or preview lands on, with only a visible lock applied. */
function constrainedMeasurePoint(anchor, raw) {
  if (!anchor || !["distance", "path"].includes(measureKind)) return raw;
  return axisLock ? constrainToAxis(anchor, raw, axisLock) : raw;
}
// Snapping is on by default: a measurement that lands a centimetre inside the
// face it was aimed at is worse than no measurement, because it looks right.
let snapEnabled = true;
let lastSnapKind = "";
const raycaster = new THREE.Raycaster();
const _ndc = new THREE.Vector2();

/** Project `point` onto the locked world axis through `origin`. */
function constrainToAxis(origin, point, lock) {
  const frame = axisFrame[lock];
  if (!frame) return point.clone();
  const out = origin.clone();
  out[frame.axis] = point[frame.axis];
  return out;
}

// Markers hold a constant on-screen size. A radius derived from the model
// span filled the viewport the moment anyone zoomed close to a small element;
// deriving it from the camera distance every frame makes a dot a dot at any
// zoom on any model.
const MARKER_PX = 4.5;
const MEASURE_COLOR = 0xffb454;
const EMPHASIS_COLOR = 0x5ad1ff;

function screenScaledDot(px, color) {
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(1, 12, 8),
    new THREE.MeshBasicMaterial({ color, depthTest: true, depthWrite: false }));
  dot.userData.px = px;
  dot.renderOrder = 999;
  return dot;
}

function syncMarkerScale(marker) {
  const perPixel = Math.max(worldPerPixel(marker.position), 1e-9);
  if (marker.isSprite) {
    marker.scale.set(
      perPixel * marker.userData.pxW, perPixel * marker.userData.pxH, 1);
  } else {
    marker.scale.setScalar(Math.max(perPixel * marker.userData.px, 1e-6));
  }
}

function syncScreenMarkers() {
  measureGroup.traverse((child) => {
    if (child.userData.px || child.userData.pxW) syncMarkerScale(child);
  });
  for (const child of snapGroup.children) {
    if (child.userData.px || child.userData.pxW) syncMarkerScale(child);
  }
}

// Snap glyphs follow the CAD convention people already read: square for a
// corner, triangle for a midpoint, circle for a face centre, diamond for a
// point along an edge, and a plain dot for a bare surface hit.
const GLYPH_PX = { corner: 13, midpoint: 13, centre: 12, edge: 12, axis: 13, surface: 7 };
const _glyphTextures = new Map();

function snapGlyphTexture(kind) {
  let texture = _glyphTextures.get(kind);
  if (texture) return texture;
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.strokeStyle = "#ffffff";
  ctx.fillStyle = "#ffffff";
  ctx.lineWidth = 7;
  ctx.lineJoin = "miter";
  const m = 10;
  if (kind === "corner") {
    ctx.strokeRect(m, m, size - 2 * m, size - 2 * m);
  } else if (kind === "midpoint") {
    ctx.beginPath();
    ctx.moveTo(size / 2, m);
    ctx.lineTo(size - m, size - m);
    ctx.lineTo(m, size - m);
    ctx.closePath();
    ctx.stroke();
  } else if (kind === "centre") {
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size / 2 - m, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, 5, 0, Math.PI * 2);
    ctx.fill();
  } else if (kind === "edge") {
    ctx.beginPath();
    ctx.moveTo(size / 2, m);
    ctx.lineTo(size - m, size / 2);
    ctx.lineTo(size / 2, size - m);
    ctx.lineTo(m, size / 2);
    ctx.closePath();
    ctx.stroke();
  } else if (kind === "axis") {
    ctx.beginPath();
    ctx.moveTo(m, size / 2);
    ctx.lineTo(size - m, size / 2);
    ctx.moveTo(size / 2, m);
    ctx.lineTo(size / 2, size - m);
    ctx.stroke();
  } else {
    ctx.beginPath();
    ctx.arc(size / 2, size / 2, size / 2 - m * 2, 0, Math.PI * 2);
    ctx.fill();
  }
  texture = new THREE.CanvasTexture(canvas);
  _glyphTextures.set(kind, texture);
  return texture;
}

/** A floating dimension tag, drawn once and screen-scaled every frame. */
function labelSprite(text) {
  const scale = 2;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const font = `600 ${12 * scale}px "Segoe UI Variable Text", "Segoe UI", Arial, sans-serif`;
  ctx.font = font;
  const pad = 7 * scale;
  canvas.width = Math.ceil(ctx.measureText(text).width) + pad * 2;
  canvas.height = 21 * scale;
  ctx.font = font;
  ctx.fillStyle = "rgba(18, 25, 33, 0.92)";
  ctx.strokeStyle = "rgba(111, 168, 216, 0.65)";
  ctx.lineWidth = scale;
  if (ctx.roundRect) {
    ctx.beginPath();
    ctx.roundRect(scale, scale, canvas.width - 2 * scale, canvas.height - 2 * scale, 5 * scale);
    ctx.fill();
    ctx.stroke();
  } else {
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.fillStyle = "#eaf1f7";
  ctx.textBaseline = "middle";
  ctx.fillText(text, pad, canvas.height / 2 + scale);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: texture, depthTest: false, transparent: true,
  }));
  sprite.userData.pxW = canvas.width / scale;
  sprite.userData.pxH = canvas.height / scale;
  sprite.userData.isLabel = true;
  sprite.renderOrder = 1002;
  return sprite;
}

// In-progress clicks collect here; a commit adopts them into its own group,
// so one measurement is one deletable, highlightable object.
let pendingGroup = null;

function ensurePendingGroup() {
  if (!pendingGroup) {
    pendingGroup = new THREE.Group();
    measureGroup.add(pendingGroup);
  }
  return pendingGroup;
}

function disposeVisual(object) {
  for (const child of [...object.children]) disposeVisual(child);
  if (object.geometry) object.geometry.dispose();
  if (object.material) {
    if (object.material.map) object.material.map.dispose();
    object.material.dispose();
  }
}

/** Adopt every pending visual into one group, with an optional dimension tag. */
function adoptPending(labelText, labelPoint) {
  const group = new THREE.Group();
  const bucket = ensurePendingGroup();
  while (bucket.children.length) group.add(bucket.children[0]);
  if (labelText && labelPoint) {
    const tag = labelSprite(labelText);
    tag.position.copy(labelPoint);
    syncMarkerScale(tag);
    group.add(tag);
  }
  measureGroup.add(group);
  return group;
}

function addMarker(point) {
  const dot = screenScaledDot(MARKER_PX, MEASURE_COLOR);
  dot.position.copy(point);
  syncMarkerScale(dot);
  ensurePendingGroup().add(dot);
  return dot;
}

const _depthForward = new THREE.Vector3();
const _depthCorner = new THREE.Vector3();

/** Tight view-depth bounds keep the packed 24-bit point precise on large sites. */
function depthPickRange() {
  camera.getWorldDirection(_depthForward);
  if (!modelBox) {
    return {
      near: camera.near,
      far: Math.max(camera.near + 1e-3, camera.far),
      forward: _depthForward.clone(),
    };
  }
  let near = Infinity;
  let far = -Infinity;
  for (let corner = 0; corner < 8; corner++) {
    _depthCorner.set(
      corner & 1 ? modelBox[3] : modelBox[0],
      corner & 2 ? modelBox[4] : modelBox[1],
      corner & 4 ? modelBox[5] : modelBox[2],
    );
    const depth = _depthCorner.sub(camera.position).dot(_depthForward);
    near = Math.min(near, depth);
    far = Math.max(far, depth);
  }
  near = Math.max(near, camera.near);
  far = Math.min(far, camera.far);
  if (!Number.isFinite(near) || !Number.isFinite(far)) {
    near = camera.near;
    far = camera.far;
  }
  const padding = Math.max((far - near) * 1e-5, 1e-5);
  near = Math.max(camera.near, near - padding);
  far = Math.max(near + 1e-3, Math.min(camera.far, far + padding));
  return { near, far, forward: _depthForward.clone() };
}

function beginSceneProbe(scaled) {
  const state = {
    scaled,
    background: scene.background,
    override: scene.overrideMaterial,
    gridWasVisible: grid.visible,
    axesWasVisible: axes ? axes.visible : false,
    measureWasVisible: measureGroup.visible,
    snapWasVisible: snapGroup.visible,
    edgesWereVisible: edgeRoot.visible,
    sectionHelperWasVisible: sectionHelperRoot.visible,
    target: renderer.getRenderTarget(),
    clearColor: renderer.getClearColor(new THREE.Color()).clone(),
    clearAlpha: renderer.getClearAlpha(),
  };
  scene.background = null;
  grid.visible = false;
  if (axes) axes.visible = false;
  measureGroup.visible = false;
  snapGroup.visible = false;
  edgeRoot.visible = false;
  sectionHelperRoot.visible = false;
  return state;
}

function endSceneProbe(state) {
  renderer.setRenderTarget(state.target);
  renderer.setClearColor(state.clearColor, state.clearAlpha);
  camera.clearViewOffset();
  scene.overrideMaterial = state.override;
  scene.background = state.background;
  grid.visible = state.gridWasVisible;
  if (axes) axes.visible = state.axesWasVisible;
  measureGroup.visible = state.measureWasVisible;
  snapGroup.visible = state.snapWasVisible;
  edgeRoot.visible = state.edgesWereVisible;
  sectionHelperRoot.visible = state.sectionHelperWasVisible;
  if (state.scaled) invalidate();
}

/**
 * Draw the depth of one pixel into pickTarget, and remember what to put back.
 *
 * A click can afford to block for the pixel and a hover cannot, so the two
 * readbacks share this setup rather than keeping two copies of it. Everything
 * the decode depends on is captured here: the camera may well have moved by
 * the time an asynchronous read comes back.
 */
function beginDepthProbe(clientX, clientY) {
  if (!elements.size) return null;
  const rect = canvas.getBoundingClientRect();
  if (clientX < rect.left || clientX >= rect.right
    || clientY < rect.top || clientY >= rect.bottom) return null;
  const scaled = ensureFullResolution();
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const x = Math.min(size.x - 1, Math.max(0,
    Math.floor(((clientX - rect.left) / rect.width) * size.x)));
  const y = Math.min(size.y - 1, Math.max(0,
    Math.floor(((clientY - rect.top) / rect.height) * size.y)));
  // The render samples the centre of the chosen drawing-buffer pixel. Decode
  // through that exact ray, not the fractional CSS coordinate that selected it.
  const sampleClientX = rect.left + ((x + 0.5) / size.x) * rect.width;
  const sampleClientY = rect.top + ((y + 0.5) / size.y) * rect.height;
  _ndc.x = ((sampleClientX - rect.left) / rect.width) * 2 - 1;
  _ndc.y = -((sampleClientY - rect.top) / rect.height) * 2 + 1;
  camera.updateMatrixWorld();
  raycaster.setFromCamera(_ndc, camera);
  const range = depthPickRange();
  const state = {
    ...beginSceneProbe(scaled),
    rect,
    near: range.near,
    far: range.far,
    forward: range.forward,
    cameraPosition: camera.position.clone(),
    rayOrigin: raycaster.ray.origin.clone(),
    rayDirection: raycaster.ray.direction.clone(),
    serial: cameraSerial,
  };
  // the preview glyph would otherwise be the nearest surface to itself
  // A line drawn on a surface would be measured instead of the surface.
  depthMaterial.uniforms.uStateTex.value = stateTex;
  depthMaterial.uniforms.uStateSize.value.set(STATE_W, stateH);
  depthMaterial.uniforms.uNear.value = state.near;
  depthMaterial.uniforms.uFar.value = state.far;
  depthMaterial.clippingPlanes = activeClipPlanes;
  scene.overrideMaterial = depthMaterial;
  camera.setViewOffset(size.x, size.y, x, y, 1, 1);
  renderer.setRenderTarget(pickTarget);
  renderer.setClearColor(0x000000, 0);
  renderer.clear();
  renderer.render(scene, camera);
  return state;
}

/** The encoded view depth as a point on the exact sampled ray. */
function depthPointFrom(state, buffer) {
  if (!buffer[3]) return null;
  const normalized = buffer[0] / 255 + buffer[1] / 65025 + buffer[2] / 16581375;
  const depth = state.near + normalized * (state.far - state.near);
  if (!Number.isFinite(depth)) return null;
  const originDepth = _depthCorner
    .copy(state.rayOrigin).sub(state.cameraPosition).dot(state.forward);
  const rayDepth = state.rayDirection.dot(state.forward);
  if (!(rayDepth > 1e-8)) return null;
  const along = (depth - originDepth) / rayDepth;
  if (!Number.isFinite(along)) return null;
  return state.rayOrigin.clone().addScaledVector(state.rayDirection, along);
}

function surfacePointAt(clientX, clientY) {
  const state = beginDepthProbe(clientX, clientY);
  if (!state) return null;
  try {
    renderer.readRenderTargetPixels(pickTarget, 0, 0, 1, 1, pickBuffer);
  } finally {
    endSceneProbe(state);
  }
  return depthPointFrom(state, pickBuffer);
}

const probeBuffer = new Uint8Array(4);
let asyncProbeWorks = true;

/**
 * The same probe, without the stall.
 *
 * readRenderTargetPixels flushes the command stream and blocks JavaScript
 * until the GPU has caught up, which is why the hover preview could never
 * exceed 25 Hz. three.js issues the asynchronous read straight away and only
 * waits on a fence, so the scene goes back before the wait and just the decode
 * happens after it.
 */
async function surfacePointAsync(clientX, clientY) {
  if (!asyncProbeWorks || typeof renderer.readRenderTargetPixelsAsync !== "function") {
    return surfacePointAt(clientX, clientY);
  }
  const state = beginDepthProbe(clientX, clientY);
  if (!state) return null;
  let read;
  try {
    read = renderer.readRenderTargetPixelsAsync(pickTarget, 0, 0, 1, 1, probeBuffer);
  } finally {
    endSceneProbe(state);
  }
  try {
    await read;
  } catch {
    // Never again on this context: a preview that silently stopped appearing
    // would be worse than one that blocks for its pixel.
    asyncProbeWorks = false;
    return surfacePointAt(clientX, clientY);
  }
  // A camera that moved during the wait would put the ray somewhere the pixel
  // was never measured from.
  if (state.serial !== cameraSerial) return null;
  return depthPointFrom(state, probeBuffer);
}

// The reference viewer asks the GPU which visible elements occupy a small
// patch around the cursor before it scans CPU-side feature edges. This avoids
// snapping to a hidden wall or to the wrong part of a multi-part product.
const SNAP_PATCH = 81; // enough for an 18 CSS-px reach at the capped 2x DPR
const SNAP_CANDIDATE_LIMIT = 16;
const snapCandidateTarget = new THREE.WebGLRenderTarget(SNAP_PATCH, SNAP_PATCH);
const snapCandidateBuffer = new Uint8Array(SNAP_PATCH * SNAP_PATCH * 4);
const snapCandidateAsyncBuffer = new Uint8Array(SNAP_PATCH * SNAP_PATCH * 4);
let asyncCandidateWorks = true;

function beginSnapCandidateProbe(clientX, clientY) {
  if (!elements.size) return null;
  const rect = canvas.getBoundingClientRect();
  if (clientX < rect.left || clientX >= rect.right
    || clientY < rect.top || clientY >= rect.bottom) return null;
  const scaled = ensureFullResolution();
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const px = Math.min(size.x - 1, Math.max(0,
    Math.floor(((clientX - rect.left) / rect.width) * size.x)));
  const py = Math.min(size.y - 1, Math.max(0,
    Math.floor(((clientY - rect.top) / rect.height) * size.y)));
  const scale = Math.min(size.x / rect.width, size.y / rect.height);
  const half = Math.min(
    Math.ceil(SNAP_REACH_PX * scale), Math.floor((SNAP_PATCH - 1) / 2));
  const span = half * 2 + 1;
  const state = {
    ...beginSceneProbe(scaled),
    rect,
    drawingWidth: size.x,
    drawingHeight: size.y,
    px,
    py,
    half,
    span,
    serial: cameraSerial,
  };
  scene.overrideMaterial = pickMaterial;
  camera.setViewOffset(size.x, size.y, px - half, py - half, span, span);
  renderer.setRenderTarget(snapCandidateTarget);
  renderer.setClearColor(0x000000, 0);
  renderer.clear();
  renderer.render(scene, camera);
  return state;
}

function decodeSnapCandidates(buffer, state) {
  const ids = [];
  const seen = new Set();
  const centre = (SNAP_PATCH - 1) / 2;
  let centreId = null;
  let nearest = null;
  let nearestDistance = Infinity;
  const at = (x, y) => {
    if (x < 0 || y < 0 || x >= SNAP_PATCH || y >= SNAP_PATCH) return;
    const pixel = (y * SNAP_PATCH + x) * 4;
    const encoded = buffer[pixel] * 65536 + buffer[pixel + 1] * 256 + buffer[pixel + 2];
    if (!encoded) return;
    const id = elementsByIndex[encoded - 1];
    if (id === undefined) return;
    if (x === centre && y === centre) centreId = id;
    const distance = (x - centre) ** 2 + (y - centre) ** 2;
    if (state && distance < nearestDistance) {
      // WebGL readback rows run bottom-up, while setViewOffset's Y is measured
      // from the top. Map the occupied patch pixel back to the CSS position
      // whose exact surface depth can guard a just-outside-silhouette snap.
      const sourceX = state.px - state.half
        + ((x + 0.5) / SNAP_PATCH) * state.span;
      const sourceY = state.py - state.half
        + ((SNAP_PATCH - y - 0.5) / SNAP_PATCH) * state.span;
      nearest = {
        id,
        clientX: state.rect.left
          + (sourceX / state.drawingWidth) * state.rect.width,
        clientY: state.rect.top
          + (sourceY / state.drawingHeight) * state.rect.height,
      };
      nearestDistance = distance;
    }
    if (seen.has(id) || ids.length >= SNAP_CANDIDATE_LIMIT) return;
    seen.add(id);
    ids.push(id);
  };
  at(centre, centre);
  // Scan the complete patch even after the id budget is full: the nearest
  // occupied pixel supplies the depth reference and must be truly nearest.
  for (let radius = 1; radius <= centre; radius++) {
    for (let delta = -radius; delta <= radius; delta++) {
      at(centre + delta, centre - radius);
      at(centre + delta, centre + radius);
      at(centre - radius, centre + delta);
      at(centre + radius, centre + delta);
    }
  }
  return { ids, centreId, nearest };
}

function snapCandidatesAt(clientX, clientY) {
  const state = beginSnapCandidateProbe(clientX, clientY);
  if (!state) return { ids: [], centreId: null, nearest: null };
  try {
    renderer.readRenderTargetPixels(
      snapCandidateTarget, 0, 0, SNAP_PATCH, SNAP_PATCH, snapCandidateBuffer);
  } finally {
    endSceneProbe(state);
  }
  return decodeSnapCandidates(snapCandidateBuffer, state);
}

async function snapCandidatesAsync(clientX, clientY) {
  if (!asyncCandidateWorks || typeof renderer.readRenderTargetPixelsAsync !== "function") {
    return snapCandidatesAt(clientX, clientY);
  }
  const state = beginSnapCandidateProbe(clientX, clientY);
  if (!state) return { ids: [], centreId: null, nearest: null };
  let read;
  try {
    read = renderer.readRenderTargetPixelsAsync(
      snapCandidateTarget, 0, 0, SNAP_PATCH, SNAP_PATCH, snapCandidateAsyncBuffer);
  } finally {
    endSceneProbe(state);
  }
  try {
    await read;
  } catch {
    asyncCandidateWorks = false;
    return snapCandidatesAt(clientX, clientY);
  }
  if (state.serial !== cameraSerial) return { ids: [], centreId: null, nearest: null };
  return decodeSnapCandidates(snapCandidateAsyncBuffer, state);
}

// ---------------------------------------------------------------- snapping
// Screen pixels a real mesh feature reaches for the cursor. Surface remains
// the fallback, so a snap never has to invent a box face-centre away from the
// geometry somebody can see.
const SNAP_RADIUS = { corner: 16, midpoint: 14, edge: 12 };
const SNAP_BIAS = { corner: 0, midpoint: 0.55, edge: 1.1 };
const SNAP_REACH_PX = 18;
// A candidate is allowed a little either side of the exact cursor surface.
// Bounding the front as well as the back stops a near OBB/feature from pulling
// the endpoint through a thin part after the camera is orbited.
const SNAP_DEPTH_SLACK_PX = 8;
const SNAP_FRONT_SLACK_PX = 10;

const _snapVP = new THREE.Matrix4();
const _snapA = new THREE.Vector3();
const _snapB = new THREE.Vector3();
const _snapHit = new THREE.Vector3();
const _snapDir = new THREE.Vector3();
const _snapDepth = new THREE.Vector3();

/** How much world one screen pixel covers at `point`. */
function worldPerPixel(point) {
  const height = canvas.clientHeight || canvas.height || 1;
  if (isOrtho()) return (camera.top - camera.bottom) / camera.zoom / height;
  camera.getWorldDirection(_snapDir);
  const distance = Math.abs(_snapDepth.copy(point).sub(camera.position).dot(_snapDir));
  return (2 * Math.tan((camera.fov * Math.PI) / 360) * distance) / height;
}

function transformSnapPoint(source, at, matrix, out) {
  const x = source[at];
  const y = source[at + 1];
  const z = source[at + 2];
  return out.set(
    matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
    matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
    matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
  );
}

/**
 * The feature the cursor is asking for, or null.
 *
 * Judged in screen space, because that is how the person holding the mouse
 * judges it: a corner ten pixels away is the one they mean, not one three
 * centimetres away in a direction the screen cannot show. Candidate ids come
 * from the GPU patch around the cursor, so only geometry actually visible in
 * this view gets to offer its retained crease and boundary edges.
 *
 * Screen space alone cannot tell a near corner from a far one: in a plan view
 * a wall's top and bottom corners land on the same pixel, so the depth of the
 * surface under the cursor is what decides between them.
 */
function snapAt(clientX, clientY, surface, candidateIds) {
  // A visible product id is not a depth reference: one product can contain
  // several distant parts. Without an exact surface at or next to the cursor,
  // offering one of all its retained edges can pull the point across space.
  if (!snapEnabled || !elements.size || !surface) return null;
  const rect = canvas.getBoundingClientRect();
  camera.updateMatrixWorld();
  _snapVP.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
  const projection = _snapVP.elements;

  // Depth along the view axis, which both projections agree on.
  const view = camera.getWorldDirection(_snapDir);
  const surfaceDepth = _snapDepth.copy(surface).sub(camera.position).dot(view);
  const depthSlack = worldPerPixel(surface) * SNAP_DEPTH_SLACK_PX;
  const frontSlack = worldPerPixel(surface) * SNAP_FRONT_SLACK_PX;

  let best = null;
  let bestId = null;
  let projectedX = 0;
  let projectedY = 0;
  const project = (point) => {
    const x = point.x, y = point.y, z = point.z;
    const w = projection[3] * x + projection[7] * y
      + projection[11] * z + projection[15];
    if (!(w > 1e-8)) return 0;
    const ndcZ = (projection[2] * x + projection[6] * y
      + projection[10] * z + projection[14]) / w;
    if (ndcZ < -1 || ndcZ > 1) return 0;
    projectedX = rect.left + ((projection[0] * x + projection[4] * y
      + projection[8] * z + projection[12]) / w * 0.5 + 0.5) * rect.width;
    projectedY = rect.top + (0.5 - (projection[1] * x + projection[5] * y
      + projection[9] * z + projection[13]) / w * 0.5) * rect.height;
    return w;
  };

  const offer = (kind, distance, point, id) => {
    const reach = SNAP_RADIUS[kind];
    if (!(distance >= 0) || distance >= reach * reach) return;
    // Screen distance is what the user sees. A small bias helps exact corners
    // win a close tie without letting a distant corner pull a point off the
    // edge directly under the cursor.
    const score = Math.sqrt(distance) + SNAP_BIAS[kind];
    if (best && best.score <= score) return;
    for (const plane of activeClipPlanes) {
      if (plane.distanceToPoint(point) < 0) return;
    }
    const depth = _snapDepth.copy(point).sub(camera.position).dot(view) - surfaceDepth;
    if (depth > depthSlack || depth < -frontSlack) return;
    best = { kind, distance, score, point: point.clone(), depth };
    bestId = id;
  };

  for (const id of candidateIds || []) {
    const rec = elements.get(id);
    if (!rec || !isElementPickable(id) || !rec.snapParts) continue;
    for (const part of rec.snapParts) {
      const source = part.segments;
      const matrix = part.matrix;
      for (let segment = 0; segment < part.count; segment++) {
        const at = Math.floor((segment * part.sourceCount) / part.count) * 6;
        transformSnapPoint(source, at, matrix, _snapA);
        const aw = project(_snapA);
        if (!aw) continue;
        const ax = projectedX;
        const ay = projectedY;
        offer("corner", (ax - clientX) ** 2 + (ay - clientY) ** 2, _snapA, id);

        transformSnapPoint(source, at + 3, matrix, _snapB);
        const bw = project(_snapB);
        if (!bw) continue;
        const bx = projectedX;
        const by = projectedY;
        offer("corner", (bx - clientX) ** 2 + (by - clientY) ** 2, _snapB, id);

        _snapHit.addVectors(_snapA, _snapB).multiplyScalar(0.5);
        if (project(_snapHit)) {
          offer("midpoint", (projectedX - clientX) ** 2
            + (projectedY - clientY) ** 2, _snapHit, id);
        }

        // Closest point in screen space, with perspective-correct interpolation
        // back along the 3D segment. This is where the cursor appears to be.
        const dx = bx - ax;
        const dy = by - ay;
        const lengthSq = dx * dx + dy * dy;
        if (lengthSq < 1e-9) continue;
        const t = Math.min(1, Math.max(0,
          ((clientX - ax) * dx + (clientY - ay) * dy) / lengthSq));
        const amount = (t * aw) / (bw + t * (aw - bw));
        _snapHit.copy(_snapA).lerp(_snapB, amount);
        const sx = ax + dx * t - clientX;
        const sy = ay + dy * t - clientY;
        offer("edge", sx * sx + sy * sy, _snapHit, id);
      }
    }
  }
  if (!best) return null;
  best.express_id = bestId;
  return best;
}

/**
 * Where a measured point goes: a feature if one is in reach, else the surface.
 *
 * `needId` is the click path only. Naming the element behind a plain surface
 * point costs a second depth pass, and the hover preview redraws far too
 * often to pay for a number nobody reads until the click lands.
 */
function measurePointAt(clientX, clientY, { needId = true } = {}) {
  const surface = surfacePointAt(clientX, clientY);
  const candidates = snapCandidatesAt(clientX, clientY);
  const reference = surface || (candidates.nearest
    ? surfacePointAt(candidates.nearest.clientX, candidates.nearest.clientY) : null);
  return snapOnSurface(clientX, clientY, surface, candidates, needId, reference);
}

/** The same answer for the hover preview, off the probe that does not block. */
async function measurePointAsync(clientX, clientY) {
  const [surface, candidates] = await Promise.all([
    surfacePointAsync(clientX, clientY),
    snapCandidatesAsync(clientX, clientY),
  ]);
  const reference = surface || (candidates.nearest
    ? await surfacePointAsync(candidates.nearest.clientX, candidates.nearest.clientY) : null);
  return snapOnSurface(clientX, clientY, surface, candidates, false, reference);
}

function snapOnSurface(clientX, clientY, surface, candidates, needId, reference = surface) {
  const snapped = snapAt(clientX, clientY, reference, candidates.ids);
  if (snapped) {
    return {
      point: snapped.point,
      kind: snapped.kind,
      depth: snapped.depth,
      express_id: snapped.express_id,
    };
  }
  if (!surface) return null;
  return {
    point: surface,
    kind: "surface",
    depth: 0,
    express_id: needId ? (candidates.centreId ?? pickElementAt(clientX, clientY)) : null,
  };
}

// -------------------------------------------------------------- snap preview
// What the click would do, shown before the click. Every pass through here
// renders the scene once into a 1x1 buffer, so it is rate limited by what
// that actually costs on this model rather than by a number picked in advance.
const snapGroup = new THREE.Group();
snapGroup.name = "snap-preview";
scene.add(snapGroup);
let snapPreview = null;
let previewLine = null;
let previewLinePosition = null;
let snapPreviewAt = 0;
let snapPreviewCost = 8;
let snapPreviewBusy = false;
let snapPreviewQueued = null;
let snapPreviewTimer = 0;
let snapPreviewHit = null;
// Bumped whenever the preview is taken down, so a probe still in flight when
// the pointer leaves or the camera moves cannot put the glyph back.
let snapPreviewGen = 0;

function clearSnapPreview({ keepQueue = false } = {}) {
  snapPreviewGen++;
  snapPreviewHit = null;
  if (!keepQueue) {
    snapPreviewQueued = null;
    clearTimeout(snapPreviewTimer);
    snapPreviewTimer = 0;
  }
  if (snapPreview) snapPreview.visible = false;
  if (previewLine) previewLine.visible = false;
  $("snap-hint").hidden = true;
  updateMeasureLive();
  invalidate();
}

/** Reuse a fresh hover answer for the click made on that same screen point. */
function cachedMeasurePoint(clientX, clientY) {
  const cached = snapPreviewHit;
  if (!cached || cached.serial !== cameraSerial
    || performance.now() - cached.at > 300
    || Math.hypot(clientX - cached.clientX, clientY - cached.clientY) > 4) return null;
  return { ...cached.hit, point: cached.hit.point.clone() };
}

/** Keep only the newest pointer sample while the GPU readback is in flight. */
function queueSnapPreview(clientX, clientY) {
  snapPreviewQueued = [clientX, clientY];
  if (snapPreviewBusy || snapPreviewTimer) return;
  const wait = Math.max(0, Math.max(16, snapPreviewCost * 2)
    - (performance.now() - snapPreviewAt));
  if (wait > 0) {
    snapPreviewTimer = setTimeout(runQueuedSnapPreview, wait);
  } else {
    void runQueuedSnapPreview();
  }
}

async function runQueuedSnapPreview() {
  snapPreviewTimer = 0;
  if (snapPreviewBusy || !snapPreviewQueued || !measureMode) return;
  const request = snapPreviewQueued;
  snapPreviewQueued = null;
  await showSnapPreview(request[0], request[1]);
  if (snapPreviewQueued && measureMode) queueSnapPreview(...snapPreviewQueued);
}

/**
 * What the click would do, shown before the click: the point it would land
 * on, the rubber band from the anchor, and the live length beside the cursor.
 */
async function showSnapPreview(clientX, clientY) {
  const now = performance.now();
  snapPreviewAt = now;
  snapPreviewBusy = true;
  const generation = snapPreviewGen;
  let hit = null;
  try {
    hit = await measurePointAsync(clientX, clientY);
  } finally {
    snapPreviewBusy = false;
  }
  snapPreviewCost = performance.now() - now;
  if (generation !== snapPreviewGen) return;
  if (!hit) {
    clearSnapPreview({ keepQueue: true });
    return;
  }
  snapPreviewHit = {
    clientX,
    clientY,
    serial: cameraSerial,
    at: performance.now(),
    hit: { ...hit, point: hit.point.clone() },
  };
  const anchor = pending.length ? pending[pending.length - 1].point : null;
  const point = constrainedMeasurePoint(anchor, hit.point);
  const movedByLock = point.distanceToSquared(hit.point) > 1e-16;
  const previewKind = movedByLock ? "axis" : hit.kind;
  if (!snapPreview) {
    snapPreview = new THREE.Sprite(new THREE.SpriteMaterial({
      map: snapGlyphTexture(previewKind), depthTest: false, transparent: true,
    }));
    snapPreview.renderOrder = 1001;
    snapGroup.add(snapPreview);
  }
  snapPreview.visible = true;
  snapPreview.material.map = snapGlyphTexture(previewKind);
  snapPreview.material.color.set(
    movedByLock ? AXIS_COLORS[axisLock] : hit.kind === "surface" ? 0x9fb2c4 : 0x5ad1ff);
  const px = GLYPH_PX[previewKind] || GLYPH_PX.surface;
  snapPreview.userData.pxW = px;
  snapPreview.userData.pxH = px;
  snapPreview.position.copy(point);
  syncMarkerScale(snapPreview);

  const lockAxis = axisLock;
  if (anchor) {
    if (!previewLine) {
      const geometry = new THREE.BufferGeometry();
      previewLinePosition = new THREE.BufferAttribute(new Float32Array(6), 3);
      previewLinePosition.setUsage(THREE.DynamicDrawUsage);
      geometry.setAttribute("position", previewLinePosition);
      previewLine = new THREE.Line(
        geometry,
        new THREE.LineBasicMaterial({
          color: MEASURE_COLOR, transparent: true, opacity: 0.9, depthTest: false,
        }));
      previewLine.renderOrder = 998;
      snapGroup.add(previewLine);
    }
    previewLine.visible = true;
    previewLine.material.color.set(lockAxis ? AXIS_COLORS[lockAxis] : MEASURE_COLOR);
    previewLinePosition.setXYZ(0, anchor.x, anchor.y, anchor.z);
    previewLinePosition.setXYZ(1, point.x, point.y, point.z);
    previewLinePosition.needsUpdate = true;
    previewLine.geometry.computeBoundingSphere();
  } else if (previewLine) {
    previewLine.visible = false;
  }

  const hint = $("snap-hint");
  const feature = movedByLock
    ? `${axisLock.toUpperCase()} axis` : hit.kind === "surface" ? "" : hit.kind;
  // A silhouette snap sits off the face the cursor is over, so the point is
  // not where the pixel says it is. Say by how much rather than let the
  // number arrive as a surprise.
  const offSurface = !movedByLock && hit.depth < -1e-3
    ? `${formatLength(-hit.depth)} in front` : "";
  if (anchor && measureKind !== "angle") {
    const lock = axisLock ? `${axisLock.toUpperCase()} locked` : "";
    hint.textContent = [formatLength(anchor.distanceTo(point)), lock, feature, offSurface]
      .filter(Boolean).join(" · ");
  } else {
    hint.textContent = [feature || "surface", offSurface].filter(Boolean).join(" · ");
  }
  hint.style.transform = `translate(${clientX + 14}px, ${clientY + 14}px)`;
  hint.hidden = false;
  updateMeasureLive(point);
  invalidate();
}

/**
 * The size of one element, measured on the element's own axes.
 *
 * A wall at forty degrees has a thickness; the world-axis box around it does
 * not know that and reports the diagonal instead. Length, width and thickness
 * therefore come from the oriented box, and `size` keeps the world-axis
 * extents for anyone who wants the footprint on the grid. Area and volume are
 * the tessellation itself, not a box drawn round it.
 */
/** A scene point as {x, y, z} in the model's axes. */
function modelCentre(point) {
  const [x, y, z] = toModelPoint(point);
  return { x, y, z };
}

/** A scene-space triple from measure_math back as a THREE point. */
function toVector(triple) {
  return new THREE.Vector3(triple[0], triple[1], triple[2]);
}

function elementDimensions(id) {
  const rec = elements.get(id);
  if (!rec || !Number.isFinite(rec.box[0])) return null;
  const sized = boxExtents(rec.box, rec.obb, axisFrame);
  return {
    express_id: id,
    guid: guidOf.get(id) || null,
    size: sized.size,
    length: sized.length,
    width: sized.width,
    thickness: sized.thickness,
    diagonal: sized.diagonal,
    box_volume: sized.box_volume,
    // Area and volume are the tessellation itself, not a box drawn round it.
    area: rec.area,
    volume: rec.volume,
    centre: modelCentre(toVector(sized.centre)),
    method: sized.method,
    approximate: rec.scaled === true,
  };
}

/**
 * Cast both ways along each world axis from a point, against element boxes.
 *
 * This is the clearance question: how far to the next thing above me, beside
 * me, in front of me. The ray is tested against each element's world bounding
 * box, which the viewer already keeps for culling, so the answer does not
 * depend on how the geometry was batched. Hidden and isolated-away elements
 * are skipped, and the element that owns the origin is skipped too, or every
 * axis would report a distance of zero to itself.
 */
function laserFrom(origin, { maxDistance = 0, ignore = null } = {}) {
  const span = sceneSpan();
  const reach = maxDistance > 0 ? maxDistance : span * 2;
  const skin = Math.max(span * 1e-5, 1e-5);
  const boxes = [];
  for (const [id, rec] of elements) {
    if (id === ignore || !isElementShown(id)) continue;
    boxes.push([id, rec.box]);
  }
  const axes = clearanceAxes(
    boxes, [origin.x, origin.y, origin.z], axisFrame, reach, skin);
  for (const axis of Object.values(axes)) {
    for (const way of ["negative", "positive"]) {
      const hit = axis[way];
      if (hit) hit.guid = guidOf.get(hit.express_id) || null;
    }
  }
  return {
    origin: toModelPoint(origin),
    reach,
    method: "element bounding boxes",
    axes,
  };
}

/**
 * Area, perimeter and flatness of a clicked outline, in the model's axes.
 *
 * The area is only meaningful if the points lie in a plane, so how far they
 * miss one by travels with the answer instead of being assumed away.
 */
function polygonMeasure(points) {
  const measured = polygonCore(points);
  // A direction, so the origin shift must not come with it.
  const normal = toVector(measured.normal).transformDirection(sceneToModel);
  return {
    points: points.map(toModelPoint),
    area: measured.area,
    perimeter: measured.perimeter,
    flatness: measured.flatness,
    normal: [normal.x, normal.y, normal.z],
    centre: modelCentre(toVector(measured.centre)),
  };
}

/** Total and per-segment lengths of an open clicked route. */
function polylineMeasure(points) {
  const measured = polylineCore(points);
  return {
    points: points.map(toModelPoint),
    distance: measured.distance,
    segments: measured.segments,
  };
}

/** The angle at `at`, between the directions to `from` and `to`. */
function angleMeasure(from, at, to) {
  const measured = angleCore(from, at, to);
  return {
    at: toModelPoint(at),
    from: toModelPoint(from),
    to: toModelPoint(to),
    degrees: measured.degrees,
    legs: measured.legs,
  };
}

// ------------------------------------------------------ measurement anchors
// A rebuild renumbers every express id and re-batches every triangle, so a
// measurement held as scene coordinates is worth nothing the moment the
// assistant writes one property set. Each end therefore remembers the element
// it landed on and where it sat inside that element's own box; the model-axis
// point is the fallback for a click that landed on nothing.
const ANCHOR_REACH_TOLERANCE = 0.01;
const _anchorM = new THREE.Matrix4();

/** Where `point` sits, told in a way a rebuilt scene can still read. */
function anchorAt(point, expressId) {
  const rec = expressId === null || expressId === undefined
    ? null : elements.get(expressId);
  const guid = rec ? guidOf.get(expressId) || null : null;
  const anchor = { guid, world: toModelPoint(point), local: null, reach: 0 };
  if (guid && rec.obb) {
    _anchorM.fromArray(rec.obb.m).invert();
    const local = point.clone().applyMatrix4(_anchorM);
    anchor.local = [local.x, local.y, local.z];
    anchor.reach = rec.obbReach;
  }
  return anchor;
}

/** The anchor a whole-element measurement hangs on: its centre, in its box. */
function centreAnchor(dimensions) {
  const centre = toScenePoint(
    [dimensions.centre.x, dimensions.centre.y, dimensions.centre.z]);
  return [anchorAt(centre, dimensions.express_id)];
}

/**
 * Put an anchor back on the scene, and say what happened to it.
 *
 * An element that moved carries its dimension with it, which is the whole
 * point. An element that is gone, or that came back a different shape, leaves
 * the point where it was clicked and the list row says so rather than moving
 * it somewhere nobody chose.
 */
function placeAnchor(anchor) {
  if (!anchor || !Array.isArray(anchor.world)) return null;
  const loose = (drift, id) => ({ point: toScenePoint(anchor.world), drift, id });
  if (!anchor.guid) return loose(null, undefined);
  const id = expressOf.get(anchor.guid);
  if (id === undefined) return loose("gone", undefined);
  const rec = elements.get(id);
  if (!anchor.local || !rec || !rec.obb) return loose(null, id);
  const reach = rec.obbReach;
  if (Math.abs(reach - anchor.reach)
    > Math.max(reach, anchor.reach, 1e-9) * ANCHOR_REACH_TOLERANCE) {
    return loose("changed", id);
  }
  const point = new THREE.Vector3(anchor.local[0], anchor.local[1], anchor.local[2])
    .applyMatrix4(_anchorM.fromArray(rec.obb.m));
  return { point, drift: null, id };
}

// Replaying a carried set commits many measurements in a row, and neither the
// server nor the browser store should ever see the half-replayed list.
let measureQuiet = 0;
let measuredModelKey = null;

/** Which model the measurements on screen were taken on. */
function currentModelKey() {
  const row = currentModelRow();
  if (row) return `${row.id}:${row.name}`;
  return $("model-name")?.textContent || null;
}

/** Every measurement as plain data: no express ids, no scene coordinates. */
function measurementItems() {
  return measurements.map((m) => {
    // No drift flag: replay recomputes it from what the anchors resolve to,
    // so a stored one could only ever disagree with the model on screen.
    const item = { kind: m.kind, anchors: m.anchors || [], label: m.label || "" };
    if (m.kind === "distance") {
      item.axis = m.axis || null;
      item.ends = m.ends || null;
    } else if (m.kind === "dimensions") {
      item.data = m.data;
    } else if (m.kind === "laser") {
      item.reach = m.data.reach;
    }
    return item;
  });
}

// Measurements used to be the only viewer state with no persistence at all.
// They live beside the saved views, keyed by the model they were taken on.
function saveMeasurements() {
  const items = measurementItems();
  if (items.length) uiState.measurements = { model: measuredModelKey, items };
  else delete uiState.measurements;
  saveUi();
}

/** The one place every commit, delete and clear reports through. */
function publishMeasurements() {
  if (measureQuiet) return;
  measuredModelKey = measurements.length ? currentModelKey() : null;
  saveMeasurements();
  sendMeasurements();
}

/**
 * The measurements a rebuild should carry over.
 *
 * On the first build of the session nothing is on screen yet, so the browser's
 * own copy is what survives F5. A set taken on another model is not carried:
 * its GlobalIds mean nothing here and its fallback points would land in space.
 */
function measurementCarry() {
  const key = currentModelKey();
  if (measurements.length) {
    // Carry unless both keys are known and disagree: a model this tab could
    // not name is not evidence that it is a different one.
    const known = measuredModelKey !== null && key !== null;
    return !known || measuredModelKey === key ? measurementItems() : [];
  }
  const saved = uiState.measurements;
  if (!isPlainObject(saved) || !Array.isArray(saved.items)) return [];
  return saved.model === key ? saved.items : [];
}

function replayMeasurement(item) {
  const placed = (item.anchors || []).map(placeAnchor).filter(Boolean);
  const drift = placed.find((entry) => entry.drift)?.drift || null;
  const anchors = item.anchors || [];
  if (item.kind === "distance") {
    if (placed.length !== 2) return;
    addMarker(placed[0].point);
    addMarker(placed[1].point);
    const held = axisLock;
    axisLock = item.axis || "";
    commitMeasurement(
      placed[0].point, placed[1].point, item.ends || ["surface", "surface"], anchors);
    axisLock = held;
  } else if (item.kind === "path") {
    if (placed.length < 2) return;
    const points = placed.map((entry) => entry.point);
    for (const point of points) addMarker(point);
    for (let i = 1; i < points.length; i++) drawPath([points[i - 1], points[i]], false);
    const measured = polylineMeasure(points);
    recordMeasurement(
      "path", measured, `${points.length} points`, formatLength(measured.distance),
      points[points.length - 1], anchors);
  } else if (item.kind === "angle") {
    if (placed.length !== 3) return;
    const points = placed.map((entry) => entry.point);
    for (const point of points) addMarker(point);
    drawPath(points, false);
    const measured = angleMeasure(points[0], points[1], points[2]);
    recordMeasurement(
      "angle", measured, "angle", `${measured.degrees.toFixed(1)}°`, points[1], anchors);
  } else if (item.kind === "area") {
    const points = areaOutline(placed.map((entry) => entry.point));
    if (points.length < 3) return;
    for (const point of points) addMarker(point);
    drawPath(points, true);
    const measured = polygonMeasure(points);
    const centroid = points
      .reduce((sum, p) => sum.add(p), new THREE.Vector3())
      .multiplyScalar(1 / points.length);
    recordMeasurement(
      "area", measured, `${points.length} points`,
      formatArea(measured.area), centroid, anchors);
  } else if (item.kind === "dimensions") {
    const id = placed[0] ? placed[0].id : undefined;
    // An element still in the file is re-measured; one that is gone keeps the
    // size it had, marked, instead of vanishing out of the report.
    const data = (id === undefined ? null : elementDimensions(id)) || item.data;
    if (!data) return;
    recordMeasurement("dimensions", data, item.label || "element", "", null, anchors);
  } else if (item.kind === "laser") {
    if (!placed[0]) return;
    const laser = laserFrom(placed[0].point, {
      maxDistance: item.reach || 0,
      ignore: placed[0].id ?? null,
    });
    recordMeasurement("laser", laser, item.label || "clearance", "", null, anchors);
  } else {
    return;
  }
  const last = measurements[measurements.length - 1];
  if (last && drift) last.drift = drift;
}

/** Put a carried set back on the rebuilt scene, as one visible step. */
function restoreMeasurements(items) {
  measureQuiet += 1;
  try {
    clearMeasurements();
    for (const item of Array.isArray(items) ? items : []) {
      // one row that cannot be placed is not a reason to lose the rest
      try { replayMeasurement(item); } catch { /* dropped with its row */ }
    }
  } finally {
    measureQuiet -= 1;
  }
  renderMeasurements();
  publishMeasurements();
  invalidate();
}

// The MCP side reads these back (get_viewer_measurements), so the server
// hears about every commit and clear. Everything crosses in the model's own
// axes: the tools on the other side speak IFC, not three.js.
const SCENE_DELTA = { x: 0, y: 1, z: 2 };

function sendMeasurements() {
  const row = currentModelRow();
  wsSend({
    type: "measurements",
    items: measurements.map((m) => (
      m.kind === "distance"
        ? {
            kind: "distance",
            from: toModelPoint(m.from),
            to: toModelPoint(m.to),
            distance: m.distance,
            horizontal: m.horizontal,
            vertical: m.vertical,
            slope_percent: m.slopePercent,
            slope_angle: m.slopeAngle,
            delta: ["x", "y", "z"].map((name) => m.delta[SCENE_DELTA[axisFrame[name].axis]]),
            axis: m.axis || null,
            ends: m.ends || null,
          }
        : { kind: m.kind, ...m.data }
    )),
    // The hub keeps one list per client and ignores a frame from a tab showing
    // some other model, so a second tab cannot erase the first tab's set.
    model_id: row ? row.id : null,
  });
}

function commitMeasurement(from, to, ends = ["surface", "surface"], anchors = null) {
  measureCardDismissed = false;
  const line = drawPath([from, to], false);
  const measured = spanMeasure(from, to, axisFrame.z.axis);
  const distance = measured.distance;
  const mid = from.clone().add(to).multiplyScalar(0.5);
  const group = adoptPending(formatLength(distance), mid);
  measurements.push({
    kind: "distance",
    from, to, line, group,
    ends,
    anchors: anchors || [anchorAt(from, null), anchorAt(to, null)],
    axis: axisLock || null,
    distance,
    horizontal: measured.horizontal,
    vertical: measured.vertical,
    slopePercent: measured.slopePercent,
    slopeAngle: measured.slopeAngle,
    delta: [
      Math.abs(to.x - from.x), Math.abs(to.y - from.y), Math.abs(to.z - from.z),
    ],
  });
  renderMeasurements();
  publishMeasurements();
}

/** Draw a polyline through `points` into the pending bucket. */
function drawPath(points, close) {
  const path = close ? [...points, points[0]] : points;
  const line = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(path),
    new THREE.LineBasicMaterial({ color: MEASURE_COLOR, depthTest: true, depthWrite: false }));
  line.renderOrder = 998;
  ensurePendingGroup().add(line);
  return line;
}

/** Record a result, adopting any pending visuals as one deletable group. */
function recordMeasurement(
  kind, data, label, labelText = "", labelPoint = null, anchors = null,
) {
  measureCardDismissed = false;
  const group = adoptPending(labelText, labelPoint);
  measurements.push({
    kind, data, label,
    anchors: anchors || [],
    group: group.children.length ? group : null,
  });
  if (!group.children.length) measureGroup.remove(group);
  renderMeasurements();
  publishMeasurements();
  return data;
}

function deleteMeasurement(measurement) {
  const index = measurements.indexOf(measurement);
  if (index < 0) return;
  if (measurement.group) {
    measureGroup.remove(measurement.group);
    disposeVisual(measurement.group);
  }
  measurements.splice(index, 1);
  renderMeasurements();
  publishMeasurements();
  invalidate();
}

/** Point at a row, see the measurement; the list and the scene are one thing. */
function emphasizeMeasurement(measurement, on) {
  if (!measurement.group) return;
  measurement.group.traverse((child) => {
    if (child.userData.isLabel || !child.material || !child.material.color) return;
    child.material.color.set(on ? EMPHASIS_COLOR : MEASURE_COLOR);
  });
  invalidate();
}

/** Drop the newest unclicked point, with its marker and segment. */
function undoPendingPoint() {
  measureProblem = "";
  const entry = pending.pop();
  if (!entry) return false;
  for (const visual of [entry.marker, entry.line]) {
    if (visual) {
      visual.parent?.remove(visual);
      disposeVisual(visual);
    }
  }
  clearSnapPreview();
  renderMeasurements();
  invalidate();
  return true;
}

function clearPending() {
  while (undoPendingPoint()) { /* each pop removes its own visuals */ }
}

function clearMeasurements() {
  for (const child of [...measureGroup.children]) {
    measureGroup.remove(child);
    disposeVisual(child);
  }
  pendingGroup = null;
  measurements.length = 0;
  pending.length = 0;
  renderMeasurements();
  publishMeasurements();
  invalidate();
}

function renderMeasurements() {
  if (measureQuiet) return;
  const card = $("measure-card");
  const list = $("measure-list");
  if (!card || !list) return;
  // Remember focus before rebuilding the ledger: deleting its last row
  // disconnects the focused button before we can test where focus used to be.
  const hadCardFocus = card.contains(document.activeElement);
  list.textContent = "";
  for (let i = measurements.length - 1; i >= 0; i--) {
    const m = measurements[i];
    const row = el("li", "measure-row");
    row.addEventListener("mouseenter", () => emphasizeMeasurement(m, true));
    row.addEventListener("mouseleave", () => emphasizeMeasurement(m, false));
    const drop = el("button", "measure-drop", "×");
    drop.title = "Remove this measurement";
    drop.setAttribute("aria-label", "Remove this measurement");
    drop.addEventListener("click", () => deleteMeasurement(m));
    row.appendChild(drop);
    if (m.kind === "dimensions") {
      const d = m.data;
      row.appendChild(el("span", "measure-dist", formatLength(d.thickness)));
      row.appendChild(el("span", "measure-delta",
        `${m.label || "element"} · ${formatLength(d.length)} × ${formatLength(d.width)}`
        + ` × ${formatLength(d.thickness)}`
        + (d.volume > 0 ? ` · ${formatVolume(d.volume)}` : "")));
    } else if (m.kind === "path") {
      row.appendChild(el("span", "measure-dist", formatLength(m.data.distance)));
      row.appendChild(el("span", "measure-delta",
        `${m.data.points.length} points · ${m.data.segments.length} segments`));
    } else if (m.kind === "angle") {
      row.appendChild(el("span", "measure-dist", `${m.data.degrees.toFixed(1)}°`));
      row.appendChild(el("span", "measure-delta",
        `legs ${formatLength(m.data.legs[0])} · ${formatLength(m.data.legs[1])}`));
    } else if (m.kind === "area") {
      row.appendChild(el("span", "measure-dist", formatArea(m.data.area)));
      row.appendChild(el("span", "measure-delta",
        `${m.data.points.length} points · perimeter ${formatLength(m.data.perimeter)}`
        + (m.data.flatness > m.data.perimeter * 0.002
          ? ` · off-plane ${formatLength(m.data.flatness)}` : "")));
    } else if (m.kind === "laser") {
      const parts = ["x", "y", "z"].map((axis) => {
        const value = m.data.axes[axis];
        return `${axis.toUpperCase()} ${value.span == null ? "-" : formatLength(value.span)}`;
      });
      row.appendChild(el("span", "measure-dist", "clearance"));
      row.appendChild(el("span", "measure-delta", parts.join(" · ")));
    } else {
      row.appendChild(el("span", "measure-dist", formatLength(m.distance)));
      // three.js is Y-up while IFC is Z-up, so report the model's own axes
      const snapped = (m.ends || []).filter((end) => end && end !== "surface");
      const slope = m.vertical > 1e-9 && m.horizontal > 1e-9
        ? ` · slope ${m.slopePercent.toFixed(1)}%` : "";
      row.appendChild(el("span", "measure-delta",
        (m.axis ? `${m.axis.toUpperCase()} locked · ` : "")
        + (snapped.length ? `${snapped.join("/")} · ` : "")
        + `X ${formatLength(m.delta[SCENE_DELTA[axisFrame.x.axis]])}`
        + ` · Y ${formatLength(m.delta[SCENE_DELTA[axisFrame.y.axis]])}`
        + ` · Z ${formatLength(m.delta[SCENE_DELTA[axisFrame.z.axis]])}`
        + slope));
    }
    // A carried measurement whose element did not come back sits where it was
    // clicked, which is a guess. Say so rather than let it read as measured.
    if (m.drift) {
      row.appendChild(el("span", "measure-drift",
        m.drift === "gone"
          ? "element gone · point kept where it was taken"
          : "element changed shape · point kept where it was taken"));
    }
    list.appendChild(row);
  }
  card.hidden = !measurePanelPinned && (
    (!measurements.length && !measureMode)
    || (measureCardDismissed && !measureMode)
  );
  if (card.hidden && hadCardFocus) canvas.focus({ preventScroll: true });
  const launcher = $("tool-open-measure");
  if (launcher) launcher.setAttribute("aria-expanded", String(!card.hidden));
  const railLauncher = $("btn-tool-measure");
  if (railLauncher) railLauncher.setAttribute("aria-expanded", String(!card.hidden));
  const hint = $("measure-hint");
  if (hint) {
    hint.hidden = !measureMode;
    const snap = snapEnabled
      ? (lastSnapKind && lastSnapKind !== "surface" ? `snapped to ${lastSnapKind}` : "snap on")
      : "snap off (S)";
    hint.textContent = measureHint(snap);
  }
  const count = $("measure-count");
  if (count) count.textContent = String(measurements.length);
  const railCount = $("measure-rail-count");
  if (railCount) {
    railCount.textContent = String(measurements.length);
    railCount.hidden = measurements.length === 0;
  }
  renderViewerFilters();
  const finish = $("measure-finish");
  if (finish) {
    const canFinish = measureMode
      && ((measureKind === "path" && pending.length >= 2)
        || (measureKind === "area" && pending.length >= 3));
    finish.hidden = !(measureMode && (measureKind === "path" || measureKind === "area"));
    finish.disabled = !canFinish;
    const label = $("measure-finish-label");
    if (label) label.textContent = measureKind === "area" ? "Finish area" : "Finish path";
  }
  for (const button of document.querySelectorAll("[data-measure-axis]")) {
    const active = measureMode && button.dataset.measureAxis === axisLock;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  const axis = $("measure-axis");
  if (axis) axis.hidden = !measureMode || !["distance", "path"].includes(measureKind);
  updateMeasureLive();
  invalidate();
}

/** What to do next, in the words of whichever tool is running. */
const MEASURE_BUTTONS = {
  distance: $("tool-measure"),
  path: $("tool-measure-path"),
  angle: $("tool-measure-angle"),
  area: $("tool-measure-area"),
};

/** The one place snapping turns on and off, so the key and the switch agree. */
function setSnap(on) {
  snapEnabled = on === true;
  lastSnapKind = "";
  const box = $("tool-snap");
  if (box) box.checked = snapEnabled;
  clearSnapPreview();
  renderMeasurements();
}

function measureHint(snap) {
  if (measureProblem) return `${measureProblem} · choose another point or press Backspace`;
  if (measureKind === "angle") {
    if (!pending.length) return `${snap} · click one end of the angle`;
    if (pending.length === 1) return `${snap} · click the corner the angle sits at`;
    return `${snap} · click the other end`;
  }
  if (measureKind === "area") {
    if (pending.length < 3) {
      return `${snap} · click the outline, ${3 - pending.length} more before it closes`;
    }
    return `${snap} · ${pending.length} points · click the first point or Finish`;
  }
  if (measureKind === "path") {
    if (!pending.length) return `${snap} · click the first point of the route`;
    if (pending.length === 1) return `${snap} · click the next point`;
    return `${snap} · ${pending.length} points · Finish or press Enter`;
  }
  if (!pending.length) return `${snap} · click to start, or Alt-click an element for its size`;
  return axisLock
    ? `${snap} · locked to ${axisLock.toUpperCase()}`
    : `${snap} · click the second point; X, Y or Z locks an axis`;
}

/** Prominent value in the card, including the latest uncommitted preview. */
function updateMeasureLive(preview = null) {
  const live = $("measure-live");
  if (!live) return;
  const points = pending.map((entry) => entry.point);
  if (preview) points.push(preview);
  let value = "-";
  if (measureKind === "distance" && points.length >= 2) {
    value = formatLength(points[0].distanceTo(points[1]));
  } else if (measureKind === "path" && points.length >= 2) {
    value = formatLength(polylineCore(points).distance);
  } else if (measureKind === "angle" && points.length >= 3) {
    value = `${angleCore(points[0], points[1], points[2]).degrees.toFixed(1)}°`;
  } else if (measureKind === "area" && points.length >= 3) {
    value = formatArea(polygonCore(points).area);
  }
  live.textContent = value;
}

const ORBIT_LEFT_MOUSE = controls.mouseButtons.LEFT;
const ORBIT_MIDDLE_MOUSE = controls.mouseButtons.MIDDLE;
const ORBIT_RIGHT_MOUSE = controls.mouseButtons.RIGHT;
const ORBIT_ONE_TOUCH = controls.touches.ONE;

function setMeasurePanelOpen(open) {
  measurePanelPinned = Boolean(open);
  measureCardDismissed = !open;
  if (!open && measureMode) {
    setMeasureMode(false);
    return;
  }
  renderMeasurements();
}

function setMeasureMode(on, kind) {
  const wasOn = measureMode;
  measureMode = on;
  measureProblem = "";
  if (kind) measureKind = kind;
  if (on) {
    measureCardDismissed = false;
    measurePanelPinned = true;
  }
  clearPending();
  if (!on || !["distance", "path"].includes(measureKind)) axisLock = "";
  // A measurement click must not also be an orbit gesture. While the tool is
  // open right-drag orbits, middle-drag pans, and the wheel still zooms.
  controls.mouseButtons.LEFT = on ? null : ORBIT_LEFT_MOUSE;
  controls.mouseButtons.MIDDLE = on ? THREE.MOUSE.PAN : ORBIT_MIDDLE_MOUSE;
  controls.mouseButtons.RIGHT = on ? THREE.MOUSE.ROTATE : ORBIT_RIGHT_MOUSE;
  controls.touches.ONE = on ? null : ORBIT_ONE_TOUCH;
  if (on && !wasOn && controls.enableDamping) {
    // Flush the last orbit step and clear its residual glide before aiming.
    controls.enableDamping = false;
    controls.update();
    applyMotionPreference();
  }
  clearSnapPreview();
  canvas.classList.toggle("is-measuring", on);
  for (const [name, button] of Object.entries(MEASURE_BUTTONS)) {
    const active = on && measureKind === name;
    button.setAttribute("aria-pressed", String(active));
    button.classList.toggle("is-active", active);
  }
  renderMeasurements();
}

/** An outline with repeated points collapsed, including the closing one. */
function areaOutline(points) {
  return outlinePoints(points, AREA_MIN_EDGE_SQ);
}

/** Close an area outline early, on Enter or a double-click. */
function finishArea() {
  if (measureKind !== "area" || pending.length < 3) return false;
  // A zero-length edge adds nothing to the area and everything to the
  // perimeter, the flatness and the point count the report shows.
  // areaOutline keeps the point objects it was handed, so the element each
  // surviving corner was clicked on is still reachable by identity.
  const owner = new Map(pending.map((entry) => [entry.point, entry.express_id]));
  const points = areaOutline(pending.map((entry) => entry.point));
  if (points.length < 3) return false;
  const measured = polygonMeasure(points);
  if (measured.area <= Math.max(1e-12, measured.perimeter ** 2 * 1e-10)) {
    measureProblem = "Area needs non-collinear points";
    renderMeasurements();
    return false;
  }
  // Each click already drew the preceding edge; add only the closing edge so
  // the completed outline is not rendered twice over itself.
  drawPath([points[points.length - 1], points[0]], false);
  const centroid = points
    .reduce((sum, p) => sum.add(p), new THREE.Vector3())
    .multiplyScalar(1 / points.length);
  recordMeasurement(
    "area", measured, `${points.length} points`, formatArea(measured.area), centroid,
    points.map((point) => anchorAt(point, owner.get(point) ?? null)));
  pending.length = 0;
  return true;
}

/** Commit an open route without adding an artificial closing segment. */
function finishPath() {
  if (measureKind !== "path" || pending.length < 2) return false;
  const points = pending.map((entry) => entry.point);
  const measured = polylineMeasure(points);
  if (measured.distance <= 1e-9) return false;
  recordMeasurement(
    "path", measured, `${points.length} points`, formatLength(measured.distance),
    points[points.length - 1], pendingAnchors());
  pending.length = 0;
  return true;
}

function finishOpenMeasurement() {
  return measureKind === "path" ? finishPath() : finishArea();
}

/** One anchor per pending click, each naming the element it landed on. */
function pendingAnchors() {
  return pending.map((entry) => anchorAt(entry.point, entry.express_id));
}

/** Whether the pointer is visibly back on the first corner of an area. */
function areaClosesAt(clientX, clientY) {
  if (measureKind !== "area" || pending.length < 3) return false;
  const ndc = pending[0].point.clone().project(camera);
  if (ndc.z < -1 || ndc.z > 1) return false;
  const rect = canvas.getBoundingClientRect();
  const x = rect.left + (ndc.x * 0.5 + 0.5) * rect.width;
  const y = rect.top + (0.5 - ndc.y * 0.5) * rect.height;
  return Math.hypot(clientX - x, clientY - y) <= AREA_CLOSE_PX;
}

function handleMeasureClick(clientX, clientY, prefetched = null) {
  const hit = prefetched || measurePointAt(clientX, clientY);
  if (!hit) return;
  lastSnapKind = hit.kind;
  // The click lands exactly where the preview said it would: only the visible
  // axis lock can move it away from the raw surface hit.
  const anchor = pending.length ? pending[pending.length - 1].point : null;
  const point = constrainedMeasurePoint(anchor, hit.point);
  const movedByLock = point.distanceToSquared(hit.point) > 1e-16;
  if (areaClosesAt(clientX, clientY)) {
    if (finishArea()) renderMeasurements();
    clearSnapPreview();
    return;
  }
  if ((measureKind === "area" || measureKind === "path")
    && pending.length >= MAX_MEASURE_POINTS) {
    measureProblem = `Maximum ${MAX_MEASURE_POINTS} points reached; finish this measurement`;
    clearSnapPreview();
    renderMeasurements();
    return;
  }
  if (anchor && point.distanceToSquared(anchor) <= AREA_MIN_EDGE_SQ) {
    measureProblem = "That point is the same as the previous point";
    clearSnapPreview();
    renderMeasurements();
    return;
  }
  measureProblem = "";
  // measurePointAt already resolved which element is under the cursor; keeping
  // it is what lets the measurement survive the next rebuild.
  const entry = {
    point,
    kind: movedByLock ? "axis" : hit.kind,
    express_id: movedByLock ? null : hit.express_id,
    marker: addMarker(point), line: null,
  };
  pending.push(entry);

  const wanted = MEASURE_KINDS[measureKind];
  if (!wanted || pending.length < wanted) {
    if ((measureKind === "area" || measureKind === "path") && pending.length > 1) {
      entry.line = drawPath([pending[pending.length - 2].point, point], false);
    }
    renderMeasurements();
    return;
  }
  if (measureKind === "angle") {
    const [a, b, c] = pending.map((item) => item.point);
    drawPath([a, b, c], false);
    const measured = angleMeasure(a, b, c);
    // the tag sits a step inside the corner, along the angle's bisector
    const bisector = a.clone().sub(b).normalize()
      .add(c.clone().sub(b).normalize());
    const offset = bisector.lengthSq() > 1e-9
      ? bisector.normalize().multiplyScalar(worldPerPixel(b) * 34)
      : new THREE.Vector3();
    recordMeasurement(
      "angle", measured, "angle",
      `${measured.degrees.toFixed(1)}°`, b.clone().add(offset), pendingAnchors());
  } else {
    commitMeasurement(
      pending[0].point, pending[1].point, [pending[0].kind, pending[1].kind],
      pendingAnchors());
  }
  pending.length = 0;
  clearSnapPreview();
}

// ---------------------------------------------------------------- properties
let propertiesRequest = 0;
async function showProperties(guid) {
  const request = ++propertiesRequest;
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "loading…"));
  let detail;
  try {
    const res = await api(`/api/elements/${encodeURIComponent(guid)}${modelQuery()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    detail = await res.json();
  } catch (err) {
    if (request !== propertiesRequest) return;
    panel.textContent = "";
    panel.appendChild(el("p", "hint", `could not load properties (${err.message})`));
    return;
  }
  if (request !== propertiesRequest) return;
  panel.textContent = "";
  const title = detail.attributes && detail.attributes.Name
    ? String(detail.attributes.Name) : detail.class;
  panel.appendChild(el("h3", null, title));
  panel.appendChild(el("div", "guid", `${detail.class} · ${detail.global_id}`));

  if (detail.container && detail.container.length) {
    const crumb = detail.container.map((c) => c.name || c.class).reverse().join(" / ");
    panel.appendChild(el("div", "crumb", crumb));
  }

  if (detail.attributes) {
    panel.appendChild(sectionTable("Attributes", detail.attributes));
  }
  if (detail.type && detail.type.name) {
    panel.appendChild(sectionTable("Type", { class: detail.type.class, name: detail.type.name }));
  }
  const materials = materialRows(detail.materials);
  if (materials) panel.appendChild(sectionTable("Material", materials));
  if (detail.decomposition && detail.decomposition.length) {
    panel.appendChild(partsList("Parts", detail.decomposition));
  }
  for (const [pset, props] of Object.entries(detail.psets || {})) {
    if (props && typeof props === "object") {
      const { id: _id, ...rest } = props;
      panel.appendChild(sectionTable(pset, rest));
    }
  }
  for (const [qto, props] of Object.entries(detail.qtos || {})) {
    if (props && typeof props === "object") {
      const { id: _id, ...rest } = props;
      panel.appendChild(sectionTable(`${qto} (quantities)`, rest));
    }
  }
}

// element_detail returns one of several material shapes; flatten whichever
// arrived into plain key/value rows.
function materialRows(material) {
  if (!material) return null;
  if (material.kind === "material") return material.name ? { Name: material.name } : null;
  if (material.kind === "layer_set") {
    const rows = {};
    if (material.name) rows["Layer set"] = material.name;
    (material.layers || []).forEach((layer, i) => {
      const thickness = typeof layer.thickness === "number"
        ? ` · ${Number(layer.thickness.toFixed(4))}` : "";
      rows[`Layer ${i + 1}`] = `${layer.name || "?"}${thickness}`;
    });
    return Object.keys(rows).length ? rows : null;
  }
  const list = material.constituents || material.profiles || material.materials;
  if (Array.isArray(list) && list.length) {
    return Object.fromEntries(
      list.filter(Boolean).map((name, i) => [`Material ${i + 1}`, String(name)]));
  }
  return null;
}

// Parts are navigable, so they are links into the model rather than a table.
function partsList(titleText, parts) {
  const details = el("details");
  details.open = false;
  details.appendChild(el("summary", null, `${titleText} (${parts.length})`));
  const list = el("div", "part-list");
  for (const part of parts) {
    const id = expressOf.get(part.global_id);
    const row = el(id !== undefined ? "button" : "div", "part-row", `${part.name || part.class}`);
    row.appendChild(el("span", "cls", ` ${part.class}`));
    if (id !== undefined) {
      row.type = "button";
      row.classList.add("clickable");
      row.title = "Select this part";
      row.addEventListener("click", () => setSelection([id], false));
    }
    list.appendChild(row);
  }
  details.appendChild(list);
  return details;
}

function sectionTable(titleText, obj) {
  // each section folds, so long property lists stay scannable
  const details = el("details");
  details.open = true;
  details.appendChild(el("summary", null, titleText));
  const table = el("table");
  for (const [key, value] of Object.entries(obj)) {
    if (value === null || value === undefined || value === "") continue;
    const tr = el("tr");
    tr.appendChild(el("td", null, key));
    tr.appendChild(el("td", null,
      typeof value === "object" ? JSON.stringify(value) : String(value)));
    table.appendChild(tr);
  }
  details.appendChild(table);
  return details;
}

function clearProperties() {
  propertiesRequest++;
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "Select an element to inspect its IFC data."));
}

// ---------------------------------------------------------------- websocket
let ws = null;
let wsAttempts = 0;
let reloadTimer = null;

function wsSend(frame) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
}

// buildScene disposes the model and then awaits the parse for seconds, and
// through that window expressOf is empty: every id-addressed command reports
// "None of those elements are in this model" and a screenshot returns an empty
// viewport as evidence. The hub holds commands while this says "rebuilding".
let sceneState = "ready";

function sendSceneState(state) {
  sceneState = state;
  try {
    const row = viewerDocumentOpen ? currentModelRow() : null;
    wsSend({ type: "scene_state", state, model_id: row?.id ?? null });
  } catch { /* a build must never fail because the socket did */ }
}

function scheduleReload() {
  // Bursts of edits collapse into one refetch (2 s debounce).
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(loadModel, 2000);
}

function setConnectionState(connected, label) {
  const status = $("live");
  status.classList.toggle("off", !connected);
  status.querySelector(".connection-label").textContent = label;
  status.setAttribute("aria-label", `Server ${label.toLowerCase()}`);
  status.title = connected ? "Live connection to the local server" : label;
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/ws`);

  ws.addEventListener("open", () => {
    wsAttempts = 0;
    setConnectionState(true, "Live");
    wsSend({ type: "hello", token });
    // a tab that reconnects mid-rebuild would otherwise be taken for ready
    sendSceneState(sceneState);
  });

  ws.addEventListener("message", (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    handleFrame(frame);
  });

  ws.addEventListener("close", (event) => {
    const reconnecting = event.code !== 4401 && event.code !== 4404;
    setConnectionState(false, reconnecting ? "Reconnecting" : "Offline");
    // 4401 (bad token) and 4404 (viewer switched off) are verdicts, not
    // outages: retrying cannot change either answer.
    if (event.code === 4401) {
      forgetStaleToken();
      showOverlay(STALE_TOKEN_TITLE, STALE_TOKEN_BODY, null, "error");
      return;
    }
    if (event.code === 4404) {
      showOverlay(
        "Viewer turned off",
        "Type /viewer in the ifc-console terminal to start it again.",
      );
      return;
    }
    wsAttempts += 1;
    const delay = Math.min(15000, 1000 * 2 ** Math.min(wsAttempts, 4));
    setTimeout(connect, delay);
  });
  ws.addEventListener("error", () => ws.close());
}

function handleFrame(frame) {
  switch (frame.type) {
    case "status": {
      setModelInfo(frame);
      if (frame.theme) applyTheme(frame.theme);
      // Compare against the model this tab shows, not always the active one:
      // a pinned model would otherwise reload on every status frame.
      const want = viewModelId && frame.models
        ? (frame.models.find((m) => m.id === viewModelId) || {}).etag
        : frame.etag;
      // The tab owns the selection. Resending it on every (re)connect stops
      // the LLM reading a selection the user cannot see, or missing one they
      // can. While a reload is pending the selection belongs to the old model,
      // so buildScene resends it instead.
      if (viewerDocumentOpen && want && want !== currentEtag) scheduleReload();
      else sendSelection();
      break;
    }
    case "model_updated":
      if (frame.dirty !== undefined) $("dirty").hidden = !frame.dirty;
      if (frame.reason === "loaded") applyColorThemeFrame({ clear: true });
      // These describe the active model; a pinned one is read-only and only
      // changes through a status frame (attach, detach, active switch).
      if (viewModelId || !viewerDocumentOpen) break;
      if (!frame.etag || frame.etag !== currentEtag) scheduleReload();
      break;
    case "mode_changed":
      setMode(frame.mode);
      break;
    case "theme":
      applyTheme(frame.theme);
      break;
    case "highlight":
      if (frameTargetsCurrentModel(frame)) applyHighlightFrame(frame);
      break;
    case "color_theme":
      if (frameTargetsCurrentModel(frame)) applyColorThemeFrame(frame);
      break;
    case "camera":
      if (frame.view && frame.view !== "current") setView(frame.view, fitTargetIds(frame.fit));
      else if (frame.fit) fitTo(fitTargetIds(frame.fit));
      break;
    case "screenshot_request":
      handleScreenshot(frame);
      break;
    case "command": {
      const answer = (ok, result = null, error = null) => wsSend({
        type: "command_result",
        id: frame.id,
        action: frame.action || "",
        ok,
        result,
        error,
      });
      try {
        const result = runViewerCommand(frame);
        if (result && typeof result.then === "function") {
          result.then((value) => answer(true, value)).catch(
            (error) => answer(false, null, commandFailure(error)),
          );
        } else {
          answer(true, result);
        }
      } catch (err) {
        answer(false, null, commandFailure(err));
      }
      break;
    }
    case "ping":
      wsSend({ type: "pong" });
      break;
    default:
      break;
  }
}

function frameTargetsCurrentModel(frame) {
  return viewerDocumentOpen
    && (!frame.model_id || frame.model_id === currentModelRow()?.id);
}

function applyHighlightFrame(frame) {
  const touched = new Set(highlightSet);
  if (frame.clear) {
    highlightSet.clear();
    isolateSet = null;
  } else {
    highlightColor = frame.color || "#ff3b30";
    highlightSet = new Set(
      (frame.guids || []).map((g) => expressOf.get(g)).filter((id) => id !== undefined));
    isolateSet = frame.isolate ? new Set(highlightSet) : null;
  }
  for (const id of highlightSet) touched.add(id);
  applyAppearanceTo(touched);
  applyVisibility();
  updateHighlightInfo();
  if (!frame.clear && frame.fit && highlightSet.size) fitTo([...highlightSet]);
}

function applyColorThemeFrame(frame) {
  themeByGuid.clear();
  themeLegend = [];
  themeTitle = "";
  if (!frame.clear) {
    themeTitle = frame.title || "";
    for (const group of frame.groups || []) {
      let count = 0;
      for (const guid of group.guids || []) {
        themeByGuid.set(guid, group.color);
        count++;
      }
      themeLegend.push({ label: group.label || "", color: group.color, count });
    }
  }
  applyAppearance();
  renderLegend();
  renderViewerFilters();
}

function renderLegend() {
  const legend = $("legend");
  if (!themeLegend.length) {
    legend.hidden = true;
    return;
  }
  $("legend-title").textContent = themeTitle || "Color theme";
  const list = $("legend-items");
  list.textContent = "";
  for (const entry of themeLegend) {
    const item = document.createElement("li");
    const chip = document.createElement("span");
    chip.className = "legend-chip";
    chip.style.background = entry.color;
    const text = document.createElement("span");
    text.className = "legend-label";
    text.textContent = entry.label;
    const count = document.createElement("span");
    count.className = "legend-count";
    count.textContent = String(entry.count);
    item.append(chip, text, count);
    list.append(item);
  }
  legend.hidden = false;
}

// ---------------------------------------------------------------- screenshots
function captureViewerEvidence(options = {}) {
  const row = currentModelRow();
  if (options.modelId && options.modelId !== row?.id) {
    throw new Error("Evidence request does not match the currently viewed model");
  }
  if (options.view && options.view !== "current") {
    setView(options.view, fitTargetIds(options.fit));
  } else if (options.fit) {
    fitTo(fitTargetIds(options.fit));
  }
  ensureFullResolution();
  controls.update();
  renderNow();

  const source = renderer.domElement;
  const maxSize = Math.max(64, Math.min(2048, options.maxSize || 800));
  const scale = Math.min(1, maxSize / Math.max(source.width, source.height));
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));
  const target = document.createElement("canvas");
  target.width = width;
  target.height = height;
  target.getContext("2d").drawImage(source, 0, 0, width, height);

  const mime = options.format === "png" ? "image/png" : "image/jpeg";
  const dataUrl = target.toDataURL(mime, (options.quality || 85) / 100);
  return {
    kind: "viewer-screenshot",
    modelId: row?.id ?? null,
    modelName: row?.name || "",
    selectionGuids: selectedGuids(),
    capturedAt: new Date().toISOString(),
    camera: {
      position: camera.position.toArray(),
      target: controls.target.toArray(),
    },
    mime,
    dataUrl,
    width,
    height,
  };
}

function handleScreenshot(frame) {
  const modelId = currentModelRow()?.id ?? null;
  try {
    if (frame.model_id !== modelId) {
      throw new Error("Screenshot request does not match the currently viewed model");
    }
    const evidence = captureViewerEvidence({
      modelId,
      view: frame.view,
      fit: frame.fit,
      maxSize: frame.max_size,
      format: frame.format,
      quality: frame.quality,
    });
    wsSend({
      type: "screenshot_response",
      id: frame.id,
      model_id: modelId,
      data_b64: evidence.dataUrl.slice(evidence.dataUrl.indexOf(",") + 1),
      width: evidence.width,
      height: evidence.height,
    });
  } catch (err) {
    console.error("[ifc-console] screenshot failed", err);
    wsSend({
      type: "screenshot_response",
      id: frame.id,
      model_id: modelId,
      error: String(err),
    });
  }
}

// ---------------------------------------------------------------- status bar
function setMode(mode) {
  const chip = $("mode");
  // A colored status dot (drawn in CSS) carries the meaning; the text stays clean.
  chip.textContent = mode;
  chip.dataset.mode = mode;
  scheduleViewerContext("mode");
}

function syncModelSelectionCounts() {
  for (const badge of document.querySelectorAll("[data-model-selection]")) {
    const count = selectionGuidsForModel(badge.dataset.modelSelection).length;
    badge.textContent = String(count);
    badge.hidden = count === 0;
    badge.title = count
      ? `${count} selected element${count === 1 ? "" : "s"} kept in chat context`
      : "";
  }
}

function renderModelTabs() {
  const tabs = $("model-tabs");
  const choices = $("model-tab-choices");
  if (!tabs || !choices) return;
  const currentId = viewerDocumentOpen ? currentModelRow()?.id || null : null;
  for (const id of [...closedModelTabs]) {
    if (!modelRows.some((row) => row.id === id)) closedModelTabs.delete(id);
  }
  tabs.textContent = "";
  const openRows = modelRows.filter((row) => !closedModelTabs.has(row.id));
  for (const row of openRows) {
    const tab = el("div", `model-tab${row.id === currentId ? " active" : ""}`);
    const selected = selectionGuidsForModel(row.id).length;
    tab.title = `${row.name}${row.active ? " · active in console" : ""}`
      + `${selected ? ` · ${selected} selected` : ""}`;
    const open = el("button", "model-tab-main");
    open.type = "button";
    open.dataset.modelId = row.id;
    open.setAttribute("role", "tab");
    open.setAttribute("aria-selected", String(row.id === currentId));
    open.setAttribute("aria-controls", "canvas-wrap");
    open.setAttribute("aria-label", `View ${row.name}`);
    open.appendChild(el("span", "model-tab-label", row.name));
    const selectionCount = el("span", "model-tab-selection-count", String(selected));
    selectionCount.dataset.modelSelection = row.id;
    selectionCount.hidden = selected === 0;
    open.appendChild(selectionCount);
    open.addEventListener("click", () => selectViewerModel(row.id));
    const close = el("button", "model-tab-close", "\u00d7");
    close.type = "button";
    close.title = `Close ${row.name}`;
    close.setAttribute("aria-label", close.title);
    close.addEventListener("click", (event) => {
      event.stopPropagation();
      closedModelTabs.add(row.id);
      if (row.id === currentId) {
        const replacement = openRows.find((candidate) => candidate.id !== row.id);
        if (replacement) selectViewerModel(replacement.id);
        else closeViewerSurface({ openAgent: true });
      } else {
        renderModelTabs();
      }
    });
    tab.append(open, close);
    tabs.appendChild(tab);
  }

  choices.textContent = "";
  for (const row of modelRows) {
    const isCurrent = row.id === currentId;
    const isOpen = !closedModelTabs.has(row.id);
    const open = el("button", `model-tab-choice${isCurrent ? " current" : ""}`);
    open.type = "button";
    open.title = isCurrent ? `${row.name} is being viewed` : `View ${row.name}`;
    open.setAttribute("aria-pressed", String(isCurrent));
    open.appendChild(el("span", null, row.name));
    const selected = selectionGuidsForModel(row.id).length;
    const state = isCurrent
      ? "Viewing"
      : row.active ? (isOpen ? "Active · open" : "Active") : isOpen ? "Open" : "Closed";
    open.appendChild(el(
      "span",
      "model-tab-choice-state",
      `${state}${selected ? ` · ${selected} selected` : ""}`,
    ));
    open.addEventListener("click", () => {
      closedModelTabs.delete(row.id);
      $("model-tab-menu").hidden = true;
      $("model-tab-add").setAttribute("aria-expanded", "false");
      selectViewerModel(row.id);
    });
    choices.appendChild(open);
  }
  $("model-tab-empty").hidden = modelRows.length > 0;
  $("viewer-rail").classList.toggle("single-model", modelRows.length <= 1);
  syncViewerSurface();
}

const modelTabAdd = $("model-tab-add");
modelTabAdd.addEventListener("click", (event) => {
  event.stopPropagation();
  const menu = $("model-tab-menu");
  menu.hidden = !menu.hidden;
  modelTabAdd.setAttribute("aria-expanded", String(!menu.hidden));
});
$("model-tab-menu").addEventListener("click", (event) => event.stopPropagation());

function activeModelRow() {
  return modelRows.find((row) => row.active) || modelRows[0] || null;
}

function syncViewerSurface() {
  document.body.classList.toggle("viewer-closed", !viewerDocumentOpen);
  const active = activeModelRow();
  const openActive = $("model-tab-open-active");
  openActive.hidden = viewerDocumentOpen || !active;
  if (active) {
    openActive.title = `Open ${active.name}`;
    openActive.setAttribute("aria-label", `Open active IFC file ${active.name}`);
  }
  $("viewer-empty-open").disabled = !active;
  $("viewer-empty-open").textContent = active ? `Open ${active.name}` : "No IFC file attached";
  const chatVisible = typeof chatDock !== "undefined" && !chatDock.hidden;
  $("viewer-empty").hidden = viewerDocumentOpen || chatVisible;
  $("viewer-toolbar").inert = !viewerDocumentOpen;
  if (typeof chatResize !== "undefined") {
    chatResize.hidden = !chatVisible || !viewerDocumentOpen;
  }
  if (viewerDocumentOpen) {
    if (chatVisible && typeof closePanelsForChat === "function") closePanelsForChat(true);
    if (typeof applyChatWidthForViewport === "function") applyChatWidthForViewport();
    requestAnimationFrame(() => {
      resize();
      invalidate();
    });
  }
}

function closeViewerSurface({ openAgent = false } = {}) {
  if (viewerDocumentOpen) {
    const current = currentModelRow();
    if (current?.id) modelTabViews.set(current.id, captureView(current.name));
  }
  viewerDocumentOpen = false;
  pendingModelTabView = null;
  clearTimeout(reloadTimer);
  reloadQueued = false;
  setSelection([], false);
  clearProperties();
  if (measureMode) setMeasureMode(false);
  setToolPanel(null, { persist: false });
  closePopovers();
  $("model-tab-menu").hidden = true;
  modelTabAdd.setAttribute("aria-expanded", "false");
  renderModelTabs();
  scheduleViewerContext("closed");
  if (openAgent && !chatBtn.hidden) void setChat(true);
  else syncViewerSurface();
}

function openActiveViewerModel() {
  const active = activeModelRow();
  if (active) selectViewerModel(active.id);
}

$("model-tab-open-active").addEventListener("click", openActiveViewerModel);
$("viewer-empty-open").addEventListener("click", openActiveViewerModel);

// The console can hold more than one model; the picker appears only then.
// The active model is always the first entry and the default view.
function renderModelPicker(rows) {
  modelRows = rows || [];
  const residentIds = new Set(modelRows.map((row) => row.id));
  for (const store of [modelTabViews, modelSelections]) {
    for (const modelId of store.keys()) {
      if (!residentIds.has(modelId)) store.delete(modelId);
    }
  }
  for (const modelId of parsedModelCache.keys()) {
    if (!residentIds.has(modelId)) dropParsedModel(modelId);
  }
  const select = $("model-select");
  const many = modelRows.length > 1;
  select.hidden = true;
  if (!modelRows.length) viewerDocumentOpen = false;
  if (!many) {
    if (viewModelId !== null) {
      viewModelId = null;
      if (viewerDocumentOpen) scheduleReload();
    }
    renderModelTabs();
    scheduleViewerContext("models");
    return;
  }
  const activeId = (modelRows.find((m) => m.active) || {}).id;
  if (viewModelId && viewModelId === activeId) {
    viewModelId = null;  // a pinned model that became active should follow edits again
  }
  if (viewModelId && !modelRows.some((m) => m.id === viewModelId)) {
    setSelection([], false, { remember: false, publish: false });
    viewModelId = null;  // the pinned model was detached: follow the active one
    if (viewerDocumentOpen) scheduleReload();
  }
  const wanted = viewModelId || (modelRows.find((m) => m.active) || {}).id || "";
  const signature = modelRows.map((m) => `${m.id}:${m.active}`).join("|");
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.textContent = "";
    for (const row of modelRows) {
      const option = document.createElement("option");
      option.value = row.id;
      option.textContent = row.active ? `${row.name} (active)` : row.name;
      select.appendChild(option);
    }
  }
  select.value = wanted;
  renderModelTabs();
  scheduleViewerContext("models");
}

function currentModelRow() {
  const id = viewModelId || (modelRows.find((m) => m.active) || {}).id;
  return modelRows.find((m) => m.id === id) || null;
}

function setModelInfo(status) {
  const label = $("model-name");
  if (status) {
    if (status.models) renderModelPicker(status.models);
    const row = viewerDocumentOpen ? currentModelRow() : activeModelRow();
    // With the picker visible the select already names the model; showing it
    // twice in a one-line topbar just costs space.
    label.hidden = modelRows.length > 0 && viewerDocumentOpen;
    label.textContent = (row && row.name) || status.model || "no model";
    label.title = label.textContent;
    const schema = $("schema");
    schema.textContent = (row && row.schema) || status.schema || "";
    schema.hidden = !schema.textContent;
    if (status.mode) setMode(status.mode);
    // The unit belongs to the model on screen, not to the console's active
    // one, so a pinned second model labels its own numbers.
    setFileUnits((row && row.units) || status.units || null);
    $("dirty").hidden = !status.dirty;
    if (status.highlight && frameTargetsCurrentModel(status.highlight)) {
      applyHighlightFrame(status.highlight);
    }
    if (status.color_theme && frameTargetsCurrentModel(status.color_theme)) {
      applyColorThemeFrame(status.color_theme);
    }
    const extensionPanel = requestedPanel
      ? (status.browser_panels || []).find((panel) => panel.name === requestedPanel)
      : null;
    setChatAvailable(extensionPanel, Boolean(status.chat?.enabled));
  } else {
    label.textContent = "no model";
    label.title = "";
    $("schema").hidden = true;
    $("dirty").hidden = true;
  }
  document.title = status && status.model
    ? `${status.model} · ifc-console viewer` : "ifc-console viewer";
  // the chat header names the open file and the mode; keep it in step
  chatPanel?.refresh();
  scheduleViewerContext("status");
}

async function refreshStatus() {
  try {
    const res = await api("/api/status");
    if (res.ok) setModelInfo(await res.json());
  } catch { /* the next websocket status frame will resync */ }
}

// ---------------------------------------------------------------- toolbar
// Camera framing lives on the F key and the model auto-fits on load; view
// presets and fits are still driven by the assistant over the websocket.
// Switching model reloads the scene; the active model is the default view.
function selectViewerModel(picked) {
  if (!modelRows.some((row) => row.id === picked)) return false;
  const current = currentModelRow();
  const reopening = !viewerDocumentOpen;
  viewerDocumentOpen = true;
  closedModelTabs.delete(picked);
  if (reopening) clearProperties();
  if (current?.id === picked) {
    const saved = reopening ? modelTabViews.get(picked) : null;
    if (saved) {
      if (loading || !currentEtag) pendingModelTabView = { modelId: picked, view: saved };
      else restoreView(saved);
    }
    renderModelTabs();
    if (!currentEtag && !loading) loadModel();
    else sendSelection();
    scheduleViewerContext(reopening ? "opened" : "model");
    return true;
  }
  if (current?.id) modelTabViews.set(current.id, captureView(current.name));
  pendingModelTabView = { modelId: picked, view: modelTabViews.get(picked) || null };
  // Clear the old model's shared selection before the model id changes. The
  // snapshot above keeps it for this tab without leaking it to another file.
  setSelection([], false, { remember: false, publish: false });
  const active = (modelRows.find((m) => m.active) || {}).id;
  viewModelId = picked === active ? null : picked;
  currentEtag = null;  // a different model, not a newer revision of this one
  setFileUnits((currentModelRow() || {}).units || null);
  clearProperties();
  renderModelTabs();
  loadModel();
  scheduleViewerContext("model");
  return true;
}

$("model-select").addEventListener("change", (e) => {
  selectViewerModel(e.target.value);
});

$("btn-clear-hl").addEventListener("click", () => {
  applyHighlightFrame({ clear: true });
});
$("legend-clear").addEventListener("click", () => {
  applyColorThemeFrame({ clear: true });
});

// ---------------------------------------------------------------- view tools
function updateToolButtons() {
  const none = selection.size === 0;
  $("tool-isolate").disabled = none;
  $("tool-focus-sel").disabled = none;
  $("tool-hide").disabled = none;
  $("tool-fit-sel").disabled = none;
}

function activeViewerFilters() {
  const rows = [];
  if (selection.size) {
    rows.push({
      key: "selection",
      label: `${selection.size} selected`,
      clear: () => setSelection([], false),
    });
  }
  if (ghostContext && selection.size) {
    rows.push({
      key: "transparency",
      label: "Transparent context",
      clear: () => setGhostContext(false),
    });
  }
  if (userIsolateSet || isolateSet || hiddenManual.size || hiddenByTree.size) {
    rows.push({ key: "visibility", label: "Hidden or isolated elements", clear: showEverything });
  }
  const cuts = AXES.filter((axis) => section[axis].on).length;
  if (cuts) {
    rows.push({ key: "section", label: `${cuts} section ${cuts === 1 ? "plane" : "planes"}`, clear: clearSections });
  }
  if (measurements.length || pending.length) {
    rows.push({
      key: "measurements",
      label: `${measurements.length + (pending.length ? 1 : 0)} measurement${measurements.length === 1 && !pending.length ? "" : "s"}`,
      clear: clearMeasurements,
    });
  }
  if (highlightSet.size) {
    rows.push({ key: "highlights", label: `${highlightSet.size} assistant highlights`, clear: () => applyHighlightFrame({ clear: true }) });
  }
  if (themeByGuid.size) {
    rows.push({ key: "theme", label: themeTitle || "Color theme", clear: () => applyColorThemeFrame({ clear: true }) });
  }
  if (isOrtho()) {
    rows.push({ key: "projection", label: "Orthographic projection", clear: () => setProjection("perspective") });
  }
  return rows;
}

function renderViewerFilters() {
  const list = $("filter-list");
  if (!list) return;
  const rows = activeViewerFilters();
  list.textContent = "";
  if (!rows.length) {
    list.appendChild(el("div", "filter-empty", "Default IFC view · no filters"));
  } else {
    for (const row of rows) {
      const item = el("div", "filter-row");
      item.dataset.filter = row.key;
      item.appendChild(el("span", "filter-row-label", row.label));
      const remove = el("button", "filter-remove", "\u00d7");
      remove.type = "button";
      remove.title = `Remove ${row.label}`;
      remove.setAttribute("aria-label", remove.title);
      remove.addEventListener("click", row.clear);
      item.appendChild(remove);
      list.appendChild(item);
    }
  }
  $("tool-clear-filters").disabled = rows.length === 0;
  const badge = $("filter-count");
  badge.textContent = String(rows.length);
  badge.hidden = rows.length === 0;
  $("btn-tool-filters").classList.toggle("has-active", rows.length > 0);
  $("btn-tool-visibility").classList.toggle(
    "has-active", Boolean(userIsolateSet || isolateSet || hiddenManual.size || hiddenByTree.size
      || (ghostContext && selection.size)),
  );
  $("btn-tool-section").classList.toggle("has-active", sectionActive());
  $("btn-tool-views").classList.toggle("has-active", isOrtho());
  $("btn-tool-display").classList.toggle("has-active", edgesOn());
}

function clearAllViewerFilters() {
  setSelection([], false);
  setGhostContext(false);
  showEverything();
  clearSections();
  clearMeasurements();
  applyHighlightFrame({ clear: true });
  applyColorThemeFrame({ clear: true });
  setProjection("perspective");
  renderViewerFilters();
}

$("tool-clear-filters").addEventListener("click", clearAllViewerFilters);

/**
 * What is not on screen, and why, where the person looking at it can see it.
 *
 * The only indicator lived inside the View tools popover, so a search Isolate
 * or an assistant command could take two thirds of the model away with nothing
 * on screen to say so and no visible way back. The footer carries it with the
 * controls that undo it, beside the highlight chip that set the precedent.
 */
function updateVisibilityInfo() {
  // A ghost is on screen, so it is not one of the missing; it is counted as
  // its own thing rather than reported twice under two names.
  const gone = hiddenCount - ghostCount;
  const hasVisibilityFilter = Boolean(
    userIsolateSet || isolateSet || hiddenManual.size || hiddenByTree.size,
  );
  $("tool-hidden-info").textContent =
    gone ? `${gone} of ${elements.size} elements hidden` : "";
  $("tool-show-all").disabled = !hasVisibilityFilter;
  // Before a model lands there is nothing to be missing from, and the section
  // sliders have no real range to name a cut height in.
  const parts = [];
  if (elements.size) {
    const isolated = userIsolateSet || isolateSet;
    if (isolated) parts.push(`isolated to ${isolated.size}`);
    if (ghostCount) parts.push(`${ghostCount} ghosted`);
    if (gone) parts.push(`${gone} of ${elements.size} hidden`);
    for (const [name, cut] of Object.entries(sectionState().axes)) {
      if (cut.on) parts.push(`${name.toUpperCase()} cut at ${formatLength(cut.at)}`);
    }
    if (isOrtho()) parts.push("orthographic");
  }
  $("vis-info-text").textContent = parts.join(" · ");
  $("vis-show-all").hidden = !hasVisibilityFilter;
  $("vis-clear-section").hidden = !parts.length || !sectionActive();
  renderViewerFilters();
}

/**
 * Everything on screen again, whoever took it off.
 *
 * isElementShown gates on four sets, so releasing two of them leaves elements
 * hidden while the count says nothing is. The button and the command have to
 * mean the same thing or the assistant reports a restore it did not perform.
 */
function showEverything() {
  userIsolateSet = null;
  isolateSet = null;
  hiddenManual.clear();
  hiddenByTree.clear();
  for (const box of document.querySelectorAll('#tree input[type="checkbox"]')) {
    box.checked = true;
  }
  applyVisibility();
  updateToolButtons();
}

/**
 * Release whichever gate is hiding each of `ids`, and say how many that was.
 *
 * Isolation grows to admit them rather than being dropped: the caller asked to
 * show these elements, not to throw away the view around them. The caller
 * applies the result, so one command pays for one visibility pass.
 */
function unhide(ids) {
  const hidden = ids.filter((id) => !isElementShown(id));
  for (const id of hidden) {
    hiddenManual.delete(id);
    // the branch checkbox stays as the user left it; its next toggle re-syncs
    hiddenByTree.delete(id);
    if (isolateSet) isolateSet.add(id);
    if (userIsolateSet) userIsolateSet.add(id);
  }
  return hidden.length;
}

/** Isolate the selection directly; Show all is the single way back. */
function focusSelection(fit) {
  if (!selection.size) return;
  userIsolateSet = new Set(selection);
  applyVisibility();
  updateToolButtons();
  if (fit) fitTo([...selection]);
}

$("tool-isolate").addEventListener("click", () => focusSelection(false));
$("tool-focus-sel").addEventListener("click", () => focusSelection(true));
function hideSelection() {
  if (!selection.size) return;
  for (const id of selection) hiddenManual.add(id);
  setSelection([], false);
  applyVisibility();
}
$("tool-hide").addEventListener("click", hideSelection);
$("tool-show-all").addEventListener("click", showEverything);
$("tool-fit-sel").addEventListener("click", () => {
  if (selection.size) fitTo([...selection]);
});
$("tool-fit-all").addEventListener("click", () => fitTo(null));

// The measurement controls, beside the tool they belong to. Each one reports
// through the same command path the assistant uses, so a value produced from
// this popover and one produced from an answer are the same measurement.
$("tool-snap").addEventListener("change", (e) => setSnap(e.target.checked));
$("tool-ghost").addEventListener("change", (e) => setGhostContext(e.target.checked));
$("measure-unit").addEventListener("change", (e) => setLengthUnit(e.target.value));
$("measure-decimals").addEventListener("change", (e) => setLengthDecimals(e.target.value));
$("tool-measure-element").addEventListener("click", () => {
  if (!selection.size) {
    setMeasureMode(true);
    return;
  }
  let measured = 0;
  for (const id of selection) {
    const dimensions = elementDimensions(id);
    if (!dimensions) continue;
    recordMeasurement(
      "dimensions", dimensions, dimensions.guid || `#${id}`,
      "", null, centreAnchor(dimensions));
    measured += 1;
  }
  if (measured) $("measure-card").hidden = false;
});
$("tool-measure-laser").addEventListener("click", () => {
  const id = selection.size ? [...selection][0] : null;
  const dimensions = id === null ? null : elementDimensions(id);
  if (!dimensions) {
    setMeasureMode(true);
    return;
  }
  const centre = toScenePoint(
    [dimensions.centre.x, dimensions.centre.y, dimensions.centre.z]);
  recordMeasurement(
    "laser", laserFrom(centre, { ignore: id }), "clearance",
    "", null, [anchorAt(centre, id)]);
  $("measure-card").hidden = false;
});
$("tool-measure-clear").addEventListener("click", () => clearMeasurements());
for (const btn of document.querySelectorAll("#tools-panel [data-view]")) {
  btn.addEventListener("click", () => setView(btn.dataset.view, null));
}

// -- section controls
function syncSectionRow(axis) {
  const state = section[axis];
  const slider = document.querySelector(`.section-at[data-axis="${axis}"]`);
  const flip = document.querySelector(`.section-flip[data-axis="${axis}"]`);
  const box = document.querySelector(`.section-on[data-axis="${axis}"]`);
  const modelAxis = (MODEL_OF_SCENE[axis] || axis).toUpperCase();
  if (box) box.checked = state.on;
  const label = box?.closest(".section-head")?.querySelector(".section-label");
  if (label) label.textContent = modelAxis;
  if (slider) {
    slider.hidden = !state.on;
    slider.value = String(state.t);
    slider.setAttribute("aria-label", `${modelAxis} section position`);
  }
  if (flip) {
    flip.hidden = !state.on;
    flip.classList.toggle("is-active", state.flip);
    flip.title = `Flip ${modelAxis} cut side`;
    flip.setAttribute("aria-label", flip.title);
  }
  $("tool-section-clear").hidden = !sectionActive();
}

function saveSection() {
  uiState.section = {
    x: { ...section.x }, y: { ...section.y }, z: { ...section.z },
  };
  saveUi();
}

for (const box of document.querySelectorAll(".section-on")) {
  box.addEventListener("change", () => {
    const axis = box.dataset.axis;
    section[axis].on = box.checked;
    // a fresh cut starts at the far edge so nothing vanishes on the first click
    if (box.checked && section[axis].t >= 1) section[axis].t = 0.5;
    syncSectionRow(axis);
    updateClipping();
    if (box.checked) {
      showSectionHelper(axis);
      hideSectionHelper(700);
    } else {
      hideSectionHelper();
    }
    saveSection();
  });
}
for (const slider of document.querySelectorAll(".section-at")) {
  slider.addEventListener("input", () => {
    const axis = slider.dataset.axis;
    section[axis].t = Number(slider.value);
    showSectionHelper(axis);
    updateClipping();
  });
  slider.addEventListener("change", () => {
    saveSection();
    hideSectionHelper(500);
  });
}
for (const flip of document.querySelectorAll(".section-flip")) {
  flip.addEventListener("click", () => {
    const axis = flip.dataset.axis;
    section[axis].flip = !section[axis].flip;
    syncSectionRow(axis);
    updateClipping();
    showSectionHelper(axis);
    hideSectionHelper(700);
    saveSection();
  });
}
/**
 * The slice depth, held in metres and typed in whatever the reader chose.
 *
 * The field used to be metres whatever the file was drawn in, so a
 * millimetre-authored project needed 0.1 typed in to mean 100.
 */
function setSliceDepth(metres, { sync = true } = {}) {
  sliceDepth = Math.max(0, metres) || 0;
  if (sync) syncSliceInput();
}

function syncSliceInput() {
  const input = $("section-depth");
  if (!input) return;
  const unit = unitOf(activeLengthUnit());
  // A number field cannot take 3'-3", so the imperial slice is decimal feet.
  const label = unit.imperial ? "ft" : unit.label;
  input.value = String(Number((sliceDepth * unit.perMetre).toFixed(3)));
  input.step = String(Number((0.1 * unit.perMetre).toPrecision(2)));
  input.setAttribute("aria-label", `Section slice thickness in ${label}`);
  const note = $("section-depth-unit");
  if (note) note.textContent = label;
}

$("section-depth").addEventListener("input", (e) => {
  setSliceDepth((Number(e.target.value) || 0) / perMetre(), { sync: false });
  updateClipping();
});
$("section-depth").addEventListener("change", () => {
  uiState.slice = sliceDepth;
  saveUi();
});
$("tool-ortho").addEventListener("click", () => {
  setProjection(isOrtho() ? "perspective" : "orthographic");
});
/** Every cut off again, from the popover or from the footer chip. */
function clearSections() {
  hideSectionHelper();
  for (const axis of AXES) {
    section[axis].on = false;
    syncSectionRow(axis);
  }
  updateClipping();
  saveSection();
}

$("tool-section-clear").addEventListener("click", clearSections);
$("vis-show-all").addEventListener("click", showEverything);
$("vis-clear-section").addEventListener("click", clearSections);

// -- measurement controls
const measureQuickActions = document.querySelector(
  '#tools-panel [data-tool-panel="measure"] .tool-action-grid',
);
if (measureQuickActions) {
  measureQuickActions.classList.add("measure-quick-actions");
  $("measure-card").querySelector(".measure-readout").before(measureQuickActions);
}

$("btn-tool-measure").addEventListener("click", (event) => {
  event.stopPropagation();
  closePopovers();
  setToolPanel(null);
  setMeasurePanelOpen($("measure-card").hidden);
  if (!$("measure-card").hidden) {
    MEASURE_BUTTONS[measureKind]?.focus({ preventScroll: true });
  }
});

const openMeasure = $("tool-open-measure");
if (openMeasure) {
  openMeasure.addEventListener("click", () => {
    closePopovers();
    setToolPanel(null);
    setMeasureMode(true, measureKind || "distance");
    MEASURE_BUTTONS[measureKind]?.focus({ preventScroll: true });
  });
}
for (const [kind, button] of Object.entries(MEASURE_BUTTONS)) {
  // Clicking the tool that is already running turns measuring off; clicking
  // another switches to it without a stop in between.
  button.addEventListener("click", () => {
    setMeasureMode(!(measureMode && measureKind === kind), kind);
  });
}
for (const button of document.querySelectorAll("[data-measure-axis]")) {
  button.addEventListener("click", () => {
    const wanted = button.dataset.measureAxis || "";
    axisLock = wanted && axisLock === wanted ? "" : wanted;
    clearSnapPreview();
    renderMeasurements();
    if (latestMeasurePointer) {
      queueSnapPreview(latestMeasurePointer[0], latestMeasurePointer[1]);
    }
  });
}
const measureFinish = $("measure-finish");
if (measureFinish) {
  measureFinish.addEventListener("click", () => {
    if (finishOpenMeasurement()) renderMeasurements();
  });
}
const measureClear = $("measure-clear");
if (measureClear) measureClear.addEventListener("click", clearMeasurements);
const measureClose = $("measure-close");
if (measureClose) {
  measureClose.addEventListener("click", () => {
    setMeasurePanelOpen(false);
    canvas.focus({ preventScroll: true });
  });
}

updateToolButtons();
updateVisibilityInfo();

// ---------------------------------------------------------------- search
// The server does the matching: the client only ever learns GlobalIds and
// express ids for the geometry it drew, never names or types.
const SEARCH_DEBOUNCE = 250;
let searchTimer = null;
let searchRequest = 0;
let searchAbort = null;
let searchHits = [];  // expressIDs of the current result set, in row order

function searchIds() {
  return searchHits.filter((id) => elements.has(id));
}

function cancelPendingSearch() {
  searchRequest++;
  if (searchTimer !== null) clearTimeout(searchTimer);
  searchTimer = null;
  if (searchAbort) searchAbort.abort();
  searchAbort = null;
}

function resetSearchResults() {
  searchHits = [];
  const box = $("search-results");
  box.hidden = true;
  box.textContent = "";
  box.setAttribute("aria-busy", "false");
  $("tree").hidden = false;
}

function clearSearch(refocus) {
  cancelPendingSearch();
  $("search-input").value = "";
  $("search-clear").hidden = true;
  resetSearchResults();
  if (refocus) $("search-input").focus();
}

function renderSearch(payload) {
  const box = $("search-results");
  box.textContent = "";
  box.setAttribute("aria-busy", "false");
  searchHits = [];

  const head = el("div", "search-head");
  const found = payload.truncated
    ? `${payload.results.length} of ${payload.total}`
    : `${payload.total} match${payload.total === 1 ? "" : "es"}`;
  head.appendChild(el("span", null, found));
  head.appendChild(el("span", "spacer"));
  const selectAll = el("button", null, "Select");
  selectAll.title = "Select every element in the result list";
  const isolate = el("button", null, "Isolate");
  isolate.title = "Show only the elements in the result list";
  head.appendChild(selectAll);
  head.appendChild(isolate);
  box.appendChild(head);

  if (!payload.total) {
    box.appendChild(el("p", "hint", "No elements match. Try an IFC class such as IfcDoor."));
  }

  for (const row of payload.results) {
    const id = expressOf.get(row.global_id);
    if (id !== undefined) searchHits.push(id);
    const hit = el("button", "search-hit");
    hit.type = "button";
    hit.appendChild(el("span", "name", row.name || row.class));
    const detail = [row.class, row.storey, row.type_name].filter(Boolean).join(" · ");
    hit.appendChild(el("span", "meta-line", detail));
    if (id === undefined) {
      hit.disabled = true;
      hit.title = "No geometry in this model";
    } else {
      hit.dataset.expressId = id;
      hit.setAttribute("aria-pressed", "false");
      hit.title = "Select this element. Press Enter to select and zoom.";
      hit.addEventListener("click", () => setSelection([id], false));
      hit.addEventListener("dblclick", () => fitTo([id]));
      hit.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        setSelection([id], false);
        fitTo([id]);
      });
    }
    box.appendChild(hit);
  }

  const ids = searchIds();
  selectAll.disabled = isolate.disabled = ids.length === 0;
  selectAll.addEventListener("click", () => setSelection(searchIds(), false));
  isolate.addEventListener("click", () => {
    const targets = searchIds();
    if (!targets.length) return;
    userIsolateSet = new Set(targets);
    applyVisibility();
    updateToolButtons();
  });

  markSearchSelection();
  box.hidden = false;
  $("tree").hidden = true;
}

function markSearchSelection() {
  for (const hit of document.querySelectorAll(".search-hit")) {
    const id = Number(hit.dataset.expressId);
    const selected = selection.has(id);
    hit.classList.toggle("selected", selected);
    if (!hit.disabled) hit.setAttribute("aria-pressed", String(selected));
  }
}

async function runSearch(term) {
  if (searchAbort) searchAbort.abort();
  const request = ++searchRequest;
  const controller = new AbortController();
  searchAbort = controller;
  const box = $("search-results");
  box.textContent = "";
  box.setAttribute("aria-busy", "true");
  box.appendChild(el("p", "hint", "Searching elements…"));
  box.hidden = false;
  $("tree").hidden = true;
  let payload;
  try {
    const res = await api(
      `/api/search?q=${encodeURIComponent(term)}${modelQuery().replace("?", "&")}`,
      { signal: controller.signal },
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (err) {
    if (request !== searchRequest || err.name === "AbortError") return;
    box.textContent = "";
    box.setAttribute("aria-busy", "false");
    box.appendChild(el("p", "hint", `Search failed (${err.message}). Press Enter to try again.`));
    return;
  } finally {
    if (searchAbort === controller) searchAbort = null;
  }
  if (request !== searchRequest) return;
  renderSearch(payload);
}

$("search-input").addEventListener("input", () => {
  const term = $("search-input").value.trim();
  $("search-clear").hidden = !term;
  cancelPendingSearch();
  if (term.length < 2) {
    resetSearchResults();
    return;
  }
  searchTimer = setTimeout(() => {
    searchTimer = null;
    runSearch(term);
  }, SEARCH_DEBOUNCE);
});

$("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.stopPropagation();
    clearSearch(true);
  } else if (e.key === "Enter") {
    if (searchTimer !== null) clearTimeout(searchTimer);
    searchTimer = null;
    const term = $("search-input").value.trim();
    cancelPendingSearch();
    if (term.length >= 2) runSearch(term);
    else resetSearchResults();
  }
});

$("search-clear").addEventListener("click", () => clearSearch(true));

function refreshSearch() {
  cancelPendingSearch();
  const term = $("search-input").value.trim();
  if (term.length >= 2) runSearch(term);
  else resetSearchResults();
}

// ---------------------------------------------------------------- saved views
// Camera poses live in localStorage next to the panel layout: they belong to
// this browser, not to the model, and survive reloads and model edits.
const MAX_SAVED_VIEWS = 12;

function savedViews() {
  return Array.isArray(uiState.views) ? uiState.views : (uiState.views = []);
}

function captureView(name) {
  return {
    name,
    pos: camera.position.toArray(),
    target: controls.target.toArray(),
    near: camera.near,
    far: camera.far,
    projection: isOrtho() ? "orthographic" : "perspective",
    zoom: camera.zoom,
    // What was on screen is part of what the view was about.
    selection: selectedGuids(),
    isolated: userIsolateSet ? [...userIsolateSet].map((id) => guidOf.get(id)).filter(Boolean) : null,
    hidden: [...hiddenManual].map((id) => guidOf.get(id)).filter(Boolean),
    ghost: ghostContext,
    section: AXES.map((axis) => ({ ...section[axis] })),
    slice: sliceDepth,
    // The dimensions taken in a view are part of what the view was about, and
    // they only mean anything on the model they were taken on.
    model: currentModelKey(),
    measurements: measurementItems(),
  };
}

function restoreView(view) {
  if (view.projection) setProjection(view.projection);
  camera.position.fromArray(view.pos);
  controls.target.fromArray(view.target);
  if (typeof view.zoom === "number" && view.zoom > 0) camera.zoom = view.zoom;
  if (Array.isArray(view.selection)) {
    const ids = view.selection.map((guid) => expressOf.get(guid))
      .filter((id) => id !== undefined);
    setSelection(ids, false);
  }
  if (view.isolated === null || Array.isArray(view.isolated)) {
    const ids = (view.isolated || []).map((guid) => expressOf.get(guid))
      .filter((id) => id !== undefined);
    userIsolateSet = ids.length ? new Set(ids) : null;
    applyVisibility();
    updateToolButtons();
  }
  if (Array.isArray(view.hidden)) {
    hiddenManual.clear();
    for (const guid of view.hidden) {
      const id = expressOf.get(guid);
      if (id !== undefined) hiddenManual.add(id);
    }
    applyVisibility();
  }
  if (typeof view.ghost === "boolean") setGhostContext(view.ghost);
  if (Array.isArray(view.section)) {
    AXES.forEach((axis, index) => {
      const saved = view.section[index];
      if (saved) Object.assign(section[axis], saved);
      syncSectionRow(axis);
    });
    if (typeof view.slice === "number") setSliceDepth(view.slice);
    updateClipping();
  }
  // A view saved with dimensions on this model brings them back; one saved
  // without any, or on another model, leaves what is on screen alone rather
  // than replacing it with points nothing here can place.
  if (Array.isArray(view.measurements) && view.measurements.length
    && view.model === currentModelKey()) {
    restoreMeasurements(view.measurements);
  }
  if (typeof view.near === "number") camera.near = view.near;
  if (typeof view.far === "number") camera.far = view.far;
  camera.updateProjectionMatrix();
  controls.update();
  // A restored view is a deliberate choice; the next model load must not
  // silently fit over it.
  userMovedCamera = true;
  invalidate();
}

function renderSavedViews() {
  const box = $("saved-views");
  box.textContent = "";
  const views = savedViews();
  if (!views.length) {
    box.appendChild(el("div", "tool-note empty", "none saved yet"));
    return;
  }
  views.forEach((view, index) => {
    const row = el("div", "saved-view");
    const go = el("button", "tool-btn go", view.name);
    go.title = `Go to ${view.name}`;
    go.addEventListener("click", () => restoreView(view));
    const drop = el("button", "drop", "×");
    drop.title = `Delete ${view.name}`;
    drop.setAttribute("aria-label", `Delete ${view.name}`);
    drop.addEventListener("click", () => {
      views.splice(index, 1);
      saveUi();
      renderSavedViews();
    });
    row.appendChild(go);
    row.appendChild(drop);
    box.appendChild(row);
  });
}

function saveCurrentView() {
  const views = savedViews();
  const input = $("view-name");
  const name = input.value.trim() || `View ${views.length + 1}`;
  const existing = views.findIndex((v) => v.name === name);
  if (existing >= 0) {
    views[existing] = captureView(name);
  } else {
    views.push(captureView(name));
    if (views.length > MAX_SAVED_VIEWS) views.shift();
  }
  input.value = "";
  saveUi();
  renderSavedViews();
}

$("tool-save-view").addEventListener("click", saveCurrentView);
$("view-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveCurrentView();
});

// ---------------------------------------------------------------- ui state
// Panel widths/visibility and scene settings persist across sessions.
function isPlainObject(value) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

const uiState = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem("ifc-console-viewer-ui") || "{}");
    return isPlainObject(saved) ? saved : {};
  } catch {
    return {};
  }
})();
function saveUi() {
  try {
    localStorage.setItem("ifc-console-viewer-ui", JSON.stringify(uiState));
  } catch {
    // Storage can be unavailable in private mode or full.
  }
}
setThemePreference(uiState.themePreference || "system", { persist: false });
// The remembered reading choices, before anything is drawn with a number on it.
lengthUnitChoice = LENGTH_UNITS[uiState.lengthUnit] ? uiState.lengthUnit : "file";
lengthDecimals = Number.isInteger(uiState.lengthDecimals) ? uiState.lengthDecimals : null;
edgesWanted = uiState.edges === true;
ghostContext = uiState.ghost === true;

function applySceneSettings() {
  grid.visible = uiState.grid === true;
  if (axes) axes.visible = uiState.axes === true;
  invalidate();
}

// Restore the saved cuts before the first model lands; updateClipping runs
// again on load, when modelBox finally gives the sliders a real range.
if (uiState.section) {
  for (const axis of AXES) {
    const saved = uiState.section[axis];
    if (!saved) continue;
    section[axis].on = saved.on === true;
    section[axis].t = typeof saved.t === "number" ? saved.t : 1;
    section[axis].flip = saved.flip === true;
  }
}
if (typeof uiState.slice === "number" && uiState.slice > 0) setSliceDepth(uiState.slice);
for (const axis of AXES) syncSectionRow(axis);
syncSliceInput();

function effectiveViewerWidth() {
  const dock = document.getElementById("chat-dock");
  const chatWidth = dock && !dock.hidden ? dock.getBoundingClientRect().width : 0;
  return window.innerWidth - chatWidth;
}

function syncPanelScrim() {
  const compact = effectiveViewerWidth() <= 620;
  const sidePanelOpen = !$("tree-panel").classList.contains("collapsed")
    || !$("props-panel").classList.contains("collapsed");
  $("panel-scrim").hidden = !compact || !sidePanelOpen;
}

function closeOtherCompactPanel(openKey) {
  if (effectiveViewerWidth() > 620 || uiState[openKey] !== true) return;
  const dock = document.getElementById("chat-dock");
  if (dock && !dock.hidden) setChat(false);
  if (openKey === "treeOpen" && uiState.propsOpen === true) {
    uiState.propsOpen = false;
    applyPropsPanel();
  } else if (openKey === "propsOpen" && uiState.treeOpen === true) {
    uiState.treeOpen = false;
    applyTreePanel();
  }
}

function initSidePanel(
  panelId,
  splitId,
  tabId,
  closeId,
  widthKey,
  openKey,
  side,
  openByDefault,
) {
  const panel = $(panelId);
  const splitter = $(splitId);
  const tab = $(tabId);
  const close = $(closeId);
  const clampW = (w) => {
    // Keep a useful canvas visible when both side panels are open.
    const max = Math.max(
      160,
      Math.min(window.innerWidth * 0.45, (window.innerWidth - 280) / 2),
    );
    return Math.min(Math.max(Math.round(w), 160), max);
  };
  // Properties start closed: an empty panel should not cost the 3D view 320px.
  const isOpen = () => uiState[openKey] ?? openByDefault;
  const setWidth = (width) => {
    const value = clampW(width);
    uiState[widthKey] = value;
    panel.style.width = `${value}px`;
    splitter.setAttribute("aria-valuemin", "160");
    splitter.setAttribute("aria-valuemax", String(clampW(window.innerWidth)));
    splitter.setAttribute("aria-valuenow", String(value));
    splitter.setAttribute("aria-valuetext", `${value} pixels`);
  };
  const apply = () => {
    const open = isOpen();
    const chatDockElement = document.getElementById("chat-dock");
    const chatCoversLeftTab = panelId === "tree-panel"
      && window.innerWidth <= 1040
      && chatDockElement
      && !chatDockElement.hidden;
    panel.classList.toggle("collapsed", !open);
    panel.inert = !open;
    splitter.classList.toggle("collapsed", !open);
    tab.hidden = open || chatCoversLeftTab;
    tab.setAttribute("aria-expanded", String(open));
    if (uiState[widthKey]) setWidth(uiState[widthKey]);
    else {
      panel.style.width = "";
      const fallback = panelId === "tree-panel" ? 260 : 320;
      const value = clampW(fallback);
      splitter.setAttribute("aria-valuemin", "160");
      splitter.setAttribute("aria-valuemax", String(clampW(window.innerWidth)));
      splitter.setAttribute("aria-valuenow", String(value));
      splitter.setAttribute("aria-valuetext", `${value} pixels`);
    }
    syncPanelScrim();
    scheduleViewerContext("panels");
  };
  const setOpen = (open, { focus = true } = {}) => {
    uiState[openKey] = Boolean(open);
    if (open) closeOtherCompactPanel(openKey);
    saveUi();
    apply();
    if (focus) {
      if (open) close.focus({ preventScroll: true });
      else tab.focus({ preventScroll: true });
    }
  };
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    splitter.setPointerCapture(e.pointerId);
    splitter.classList.add("dragging");
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      setWidth(side === "left" ? startW + dx : startW - dx);
    };
    const up = (ev) => {
      splitter.classList.remove("dragging");
      splitter.releasePointerCapture(ev.pointerId);
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", up);
      saveUi();
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up);
  });
  splitter.addEventListener("dblclick", () => {
    delete uiState[widthKey];
    saveUi();
    apply();
  });
  splitter.addEventListener("keydown", (event) => {
    if (event.key === "Home") {
      event.preventDefault();
      delete uiState[widthKey];
      saveUi();
      apply();
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const current = uiState[widthKey]
      || panel.getBoundingClientRect().width
      || (panelId === "tree-panel" ? 260 : 320);
    const movement = event.key === "ArrowRight" ? 16 : -16;
    setWidth(current + (side === "left" ? movement : -movement));
    saveUi();
    resize();
  });
  tab.addEventListener("click", () => setOpen(true));
  close.addEventListener("click", () => setOpen(false));
  window.addEventListener("resize", apply);
  apply();
  return { apply, isOpen, setOpen };
}

const treePanelController =
  initSidePanel(
    "tree-panel",
    "split-tree",
    "tree-panel-tab",
    "tree-panel-close",
    "treeWidth",
    "treeOpen",
    "left",
    window.innerWidth > 620,
  );
const propsPanelController = initSidePanel(
  "props-panel",
  "split-props",
  "props-panel-tab",
  "props-panel-close",
  "propsWidth",
  "propsOpen",
  "right",
  false,
);
const applyTreePanel = treePanelController.apply;
const applyPropsPanel = propsPanelController.apply;

$("panel-scrim").addEventListener("click", () => {
  const restore = propsPanelController.isOpen() ? $("props-panel-tab") : $("tree-panel-tab");
  uiState.treeOpen = false;
  uiState.propsOpen = false;
  saveUi();
  applyTreePanel();
  applyPropsPanel();
  restore.focus({ preventScroll: true });
});

const POPOVERS = [
  ["btn-settings", "settings-panel"],
  ["btn-help", "help-panel"],
];
function setPopoverOpen(btnId, panelId, open) {
  const button = $(btnId);
  $(panelId).hidden = !open;
  button.setAttribute("aria-expanded", String(open));
}

const TOOL_PANEL_LABELS = {
  visibility: "Visibility",
  views: "Views & camera",
  section: "Section planes",
  display: "Display",
  filters: "Viewer filters",
};
let activeToolPanel = null;

function setToolPanel(name, { focus = false, persist = true } = {}) {
  const valid = Object.hasOwn(TOOL_PANEL_LABELS, name) ? name : null;
  // Measure occupies the same top-right instrument shelf as these panels.
  // Closing it first avoids two controls stacking over each other there.
  if (valid && !$("measure-card").hidden) setMeasurePanelOpen(false);
  activeToolPanel = valid;
  const panel = $("tools-panel");
  panel.hidden = !valid;
  panel.dataset.panel = valid || "";
  panel.querySelector(".popover-heading").textContent = valid
    ? TOOL_PANEL_LABELS[valid] : "Viewer tools";
  for (const sectionNode of panel.querySelectorAll(":scope > .tool-section")) {
    sectionNode.hidden = !valid || sectionNode.dataset.toolPanel !== valid;
  }
  $("tool-hidden-info").hidden = valid !== "visibility";
  panel.querySelector(".measure-launcher-note").hidden = true;
  for (const button of document.querySelectorAll("#viewer-toolbar [data-tool-panel]")) {
    button.setAttribute("aria-expanded", String(button.dataset.toolPanel === valid));
  }
  if (persist) {
    uiState.toolPanel = valid;
    saveUi();
  }
  if (valid) {
    closePopovers();
    if (focus) {
      panel.querySelector("button:not(:disabled), input:not(:disabled), select:not(:disabled)")
        ?.focus({ preventScroll: true });
    }
  }
}

function closePopovers(exceptId, restoreFocus = false) {
  for (const [btnId, panelId] of POPOVERS) {
    if (panelId === exceptId) continue;
    const wasOpen = !$(panelId).hidden;
    setPopoverOpen(btnId, panelId, false);
    if (restoreFocus && wasOpen) $(btnId).focus();
  }
}

function togglePopover(btnId, panelId) {
  const panel = $(panelId);
  const open = panel.hidden;
  if (open) closePopovers(panelId);
  if (open) {
    setMeasurePanelOpen(false);
    setToolPanel(null, { persist: false });
  }
  setPopoverOpen(btnId, panelId, open);
  if (open) {
    panel.querySelector("button:not(:disabled), input:not(:disabled), select:not(:disabled)")?.focus();
  }
}

for (const [btnId, panelId] of POPOVERS) {
  $(btnId).addEventListener("click", (e) => {
    e.stopPropagation();
    togglePopover(btnId, panelId);
  });
  $(panelId).addEventListener("click", (e) => e.stopPropagation());
}
for (const button of document.querySelectorAll("#viewer-toolbar [data-tool-panel]")) {
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const name = button.dataset.toolPanel;
    setToolPanel(activeToolPanel === name ? null : name);
  });
}
$("btn-tools").addEventListener("click", () => setToolPanel("visibility"));
$("tools-panel").addEventListener("click", (event) => event.stopPropagation());
$("tools-panel-close").addEventListener("click", () => {
  const trigger = document.querySelector(`#viewer-toolbar [data-tool-panel="${activeToolPanel}"]`);
  setToolPanel(null);
  trigger?.focus({ preventScroll: true });
});
document.addEventListener("click", () => {
  closePopovers();
  $("model-tab-menu").hidden = true;
  modelTabAdd.setAttribute("aria-expanded", "false");
});

const gridBox = $("set-grid");
const axesBox = $("set-axes");
const edgesBox = $("set-edges");
const themeSelect = $("set-theme");
gridBox.checked = uiState.grid === true;
axesBox.checked = uiState.axes === true;
$("tool-ghost").checked = ghostContext;
syncEdgeSwitch();
syncUnitControls();
edgesBox.addEventListener("change", () => {
  edgesWanted = edgesBox.checked;
  uiState.edges = edgesWanted;
  saveUi();
  syncEdgeVisibility();
  renderViewerFilters();
  invalidate();
});
themeSelect.value = themePreference === "system" ? resolvedTheme() : themePreference;
themeSelect.addEventListener("change", () => {
  setThemePreference(themeSelect.value);
});
gridBox.addEventListener("change", () => {
  uiState.grid = gridBox.checked;
  saveUi();
  applySceneSettings();
});
axesBox.addEventListener("change", () => {
  uiState.axes = axesBox.checked;
  saveUi();
  applySceneSettings();
});
$("set-reset-layout").addEventListener("click", () => {
  delete uiState.treeWidth;
  delete uiState.propsWidth;
  delete uiState.chatWidth;
  delete uiState.treeOpen;
  delete uiState.propsOpen;
  saveUi();
  applyTreePanel();
  applyPropsPanel();
  chatDock.style.width = "";
  syncChatResizeAria();
  resize();
});
applySceneSettings();
renderSavedViews();
setToolPanel(uiState.toolPanel, { persist: false });

let pendingGuidCommand = null;
let guidCommandGeneration = 0;

function cachedModelContainsGuid(entry, guid) {
  const guids = entry?.parsed?.maps?.guids;
  return Array.isArray(guids) && guids.includes(guid);
}

async function modelContainingGuid(guid, hint = null) {
  if (hint && modelRows.some((row) => row.id === hint)) return hint;
  if (expressOf.has(guid) && renderedModelId) return renderedModelId;
  for (const row of modelRows) {
    if (cachedModelContainsGuid(parsedModelCache.get(row.id), guid)) return row.id;
  }
  // A GUID in an answer may name an attached IFC the user has not viewed yet.
  // Probe the lightweight element route for each resident model so one click
  // can still find and open it without making the user hunt through tabs.
  const matches = await Promise.all(modelRows.map(async (row) => {
    try {
      const query = `?model=${encodeURIComponent(row.id)}`;
      const response = await api(`/api/elements/${encodeURIComponent(guid)}${query}`);
      return response.ok ? row.id : null;
    } catch {
      return null;
    }
  }));
  return matches.find(Boolean) || null;
}

function applyGuidCommand(command) {
  const ids = command.guids
    .map((guid) => expressOf.get(guid))
    // Spatial containers and other metadata entities can have a GlobalId and
    // appear in the model tree without contributing a mesh. Never put one of
    // those ids into an isolation set: every real mesh would be filtered out
    // and the view would look irrecoverably blank.
    .filter((id) => id !== undefined && Number.isFinite(elements.get(id)?.box?.[0]));
  if (!ids.length) throw new Error("That IFC element has no geometry to show in the 3D viewer");
  setSelection(ids, false);
  const unhidden = unhide(ids);
  if (command.isolate) {
    focusSelection(true);
  } else {
    if (unhidden) {
      applyVisibility();
      updateToolButtons();
    }
    fitTo(ids);
  }
  return {
    model_id: command.modelId,
    guids: command.guids,
    selected: ids.length,
    isolated: command.isolate ? ids.length : 0,
  };
}

function applyPendingGuidCommand(modelId, { reportFailure = false } = {}) {
  if (!pendingGuidCommand || pendingGuidCommand.modelId !== modelId) return null;
  const command = pendingGuidCommand;
  pendingGuidCommand = null;
  try {
    return applyGuidCommand(command);
  } catch (error) {
    if (!reportFailure) throw error;
    console.warn("[ifc-console] could not reveal GUID", error);
    // This can run after a model switch, after revealGuidCommand has already
    // reported that the request was queued. Send the eventual failure back to
    // the chat instead of covering the viewport with an undismissable overlay.
    sendViewerResult(command, false, null, commandFailure(error));
    return null;
  }
}

async function revealGuidCommand(command) {
  const guids = (Array.isArray(command.guids) ? command.guids : [])
    .filter((guid) => typeof guid === "string" && guid.length <= 128)
    .slice(0, SELECTION_WIRE_MAX);
  if (!guids.length) throw new Error("No IFC GlobalId was provided");
  const generation = ++guidCommandGeneration;
  pendingGuidCommand = null;
  const modelId = await modelContainingGuid(guids[0], command.model_id || command.modelId);
  if (generation !== guidCommandGeneration) return { superseded: true };
  if (!modelId) throw new Error("That GlobalId is not in any attached IFC file");
  pendingGuidCommand = {
    action: command.action,
    commandId: command.commandId,
    modelId,
    guids,
    isolate: command.action === "isolate-guids",
  };
  if (!selectViewerModel(modelId)) throw new Error("The IFC file is no longer attached");
  if (renderedModelId === modelId && !loading && currentEtag) {
    return applyPendingGuidCommand(modelId);
  }
  return { model_id: modelId, guids, queued: true };
}

/**
 * Run one viewer command and return its result, or throw with a reason.
 *
 * The panel reaches this through a DOM event and the server through the
 * socket. Both get the same behaviour because there is only one of it.
 */
function runViewerCommand(command) {
  {
    const passiveWhileClosed = new Set([
      "get-context", "set-theme", "set-model", "reveal-guids", "isolate-guids",
    ]);
    if (!viewerDocumentOpen && !passiveWhileClosed.has(command.action)) {
      throw new Error("No IFC view is open; open a model tab and retry");
    }
    if (
      command.action !== "set-model"
      && command.action !== "clear-model-selection"
      && command.action !== "reveal-guids"
      && command.action !== "isolate-guids"
      && command.model_id
      && command.model_id !== currentModelRow()?.id
    ) {
      throw new Error("The requested model is not currently shown in this viewer tab");
    }
    let result = null;
    if (command.action === "get-context") {
      result = viewerContext("request");
    } else if (command.action === "set-theme") {
      setThemePreference(command.theme);
      result = viewerContext("theme").theme;
    } else if (command.action === "set-model") {
      if (!selectViewerModel(command.modelId || command.model_id)) {
        throw new Error("The requested model is not attached");
      }
      result = viewerContext("model").model;
    } else if (command.action === "reveal-guids" || command.action === "isolate-guids") {
      return revealGuidCommand(command);
    } else if (command.action === "set-panel") {
      const panel = command.panel === "tree" || command.panel === "model"
        ? treePanelController
        : command.panel === "properties" ? propsPanelController : null;
      if (!panel) throw new Error("Unknown viewer panel");
      panel.setOpen(command.open !== false, { focus: false });
      result = viewerContext("panels").panels;
    } else if (command.action === "set-selection") {
      // The panel speaks GlobalIds; only this module knows the express ids
      // they map to in the scene currently on screen.
      const guids = Array.isArray(command.guids) ? command.guids : [];
      const ids = guids.map((guid) => expressOf.get(guid)).filter((id) => id !== undefined);
      if (guids.length && !ids.length) throw new Error("None of those elements are in this model");
      // An empty list means select nothing, which additive would otherwise
      // turn into a no-op that reports the old selection as the new one.
      setSelection(ids, ids.length > 0 && command.additive === true);
      // fitTo works off the boxes it was handed whether or not the elements are
      // on screen, so fitting to a hidden one flew the camera into empty space.
      // Bring it back rather than aim at nothing; a caller that asked not to
      // move the view has not asked to change what is visible either.
      const wantsFit = command.fit !== false && ids.length > 0;
      const unhidden = wantsFit ? unhide(ids) : 0;
      if (unhidden) {
        applyVisibility();
        updateToolButtons();
      }
      if (wantsFit) fitTo(ids);
      result = { ...viewerContext("selection").selection, matched: ids.length, unhidden };
    } else if (command.action === "clear-selection") {
      setSelection([], false);
      result = viewerContext("selection").selection;
    } else if (command.action === "clear-model-selection") {
      const modelId = command.model_id || currentModelRow()?.id;
      if (!modelRows.some((row) => row.id === modelId)) {
        throw new Error("The requested model is not attached");
      }
      if (modelId === currentModelRow()?.id) {
        setSelection([], false);
      } else {
        modelSelections.delete(modelId);
        const saved = modelTabViews.get(modelId);
        if (saved) saved.selection = [];
        syncModelSelectionCounts();
        sendSelection();
        scheduleViewerContext("selection");
      }
      result = modelSelectionRows();
    } else if (command.action === "focus-selection") {
      if (!selection.size) throw new Error("Nothing is selected");
      fitTo([...selection]);
      result = viewerContext("selection").selection;
    } else if (command.action === "isolate") {
      // Showing one thing is how you say "this one" about a model with a
      // hundred thousand parts in it.
      const guids = Array.isArray(command.guids) ? command.guids : [];
      const ids = guids.length
        ? guids.map((guid) => expressOf.get(guid)).filter((id) => id !== undefined)
        : [...selection];
      if (!ids.length) {
        throw new Error(
          guids.length
            ? "None of those elements are in this model"
            : "Nothing is selected to isolate",
        );
      }
      userIsolateSet = new Set(ids);
      // An element the tree or an earlier highlight is hiding would isolate to
      // an empty screen, and the count would still claim it worked.
      const unhidden = unhide(ids);
      // Ghosting is a property of the view, not of this call, so it is only
      // touched when the caller says so.
      if (command.ghost !== undefined) setGhostContext(command.ghost !== false);
      applyVisibility();
      updateToolButtons();
      if (command.fit !== false) fitTo(ids);
      result = {
        isolated: ids.length,
        requested: guids.length || selection.size,
        unhidden,
        ghosted: ghostCount,
      };
    } else if (command.action === "show-all") {
      showEverything();
      // Read back rather than assert: the caller is told what the screen shows,
      // not what this branch meant to do to it.
      result = {
        isolated: userIsolateSet ? userIsolateSet.size : 0,
        hidden: hiddenCount,
      };
    } else if (command.action === "hide") {
      // The complement of isolate: take some elements out of the way.
      const guids = Array.isArray(command.guids) && command.guids.length
        ? command.guids
        : selectedGuids();
      const ids = guids.map((guid) => expressOf.get(guid)).filter((id) => id !== undefined);
      if (!ids.length) {
        throw new Error(
          guids.length
            ? "None of those elements are in this model"
            : "Pass guids, or select elements to hide",
        );
      }
      for (const id of ids) hiddenManual.add(id);
      setSelection([...selection].filter((id) => !hiddenManual.has(id)), false);
      applyVisibility();
      updateToolButtons();
      result = { hidden: ids.length, total_hidden: hiddenManual.size };
    } else if (command.action === "focus") {
      // A direct analysis view: isolate the elements and frame them.
      const guids = Array.isArray(command.guids) && command.guids.length
        ? command.guids
        : selectedGuids();
      if (!guids.length) throw new Error("Pass guids, or select an element to focus");
      const known = guids.filter((guid) => expressOf.has(guid));
      if (!known.length) throw new Error("None of those elements are in this model");
      const ids = known.map((guid) => expressOf.get(guid));
      userIsolateSet = new Set(ids);
      applyVisibility();
      updateToolButtons();
      if (command.fit !== false) fitTo(ids);
      result = { focused: known.length };
    } else if (command.action === "unfocus") {
      userIsolateSet = null;
      applyVisibility();
      updateToolButtons();
      result = { focused: 0 };
    } else if (command.action === "set-view") {
      const view = String(command.view || "");
      if (!VIEW_DIRECTIONS[view]) {
        throw new Error(
          `Unknown view ${view || "(none)"}; use one of ${Object.keys(VIEW_DIRECTIONS).join(", ")}`,
        );
      }
      setView(view, command.selection === true && selection.size ? [...selection] : null);
      result = { view };
    } else if (command.action === "set-camera") {
      result = applyCameraCommand(command);
    } else if (command.action === "fit") {
      result = fitCommand(command);
    } else if (command.action === "measure-element") {
      // The size question, answered without anyone clicking anything.
      const guids = Array.isArray(command.guids)
        ? command.guids
        : command.guid ? [command.guid] : [];
      const ids = guids.length
        ? guids.map((guid) => expressOf.get(guid)).filter((id) => id !== undefined)
        : [...selection];
      if (!ids.length) throw new Error("Name an element, or select one first");
      const sized = ids.map((id) => elementDimensions(id)).filter(Boolean);
      if (!sized.length) throw new Error("Those elements have no geometry in this view");
      for (const item of sized) {
        recordMeasurement(
          "dimensions", item, item.guid || "element", "", null, centreAnchor(item));
      }
      result = { measured: sized.length, elements: sized };
    } else if (command.action === "measure-laser") {
      // Clearance: what is above, beside and in front of this point.
      let origin = null;
      let laserSource = null;
      if (Array.isArray(command.point) && command.point.length === 3) {
        // callers speak the model's axes; the scene is Y-up
        origin = toScenePoint(command.point);
      } else {
        const guid = command.guid || (selection.size ? guidOf.get([...selection][0]) : null);
        const id = guid ? expressOf.get(guid) : null;
        const centre = id === undefined || id === null ? null : elementDimensions(id);
        if (!centre) throw new Error("Give a point, or select an element to shoot from");
        laserSource = id;
        origin = toScenePoint([centre.centre.x, centre.centre.y, centre.centre.z]);
      }
      const laser = laserFrom(origin, {
        maxDistance: Number(command.maxDistance) || 0,
        ignore: laserSource,
      });
      recordMeasurement(
        "laser", laser, "clearance", "", null, [anchorAt(origin, laserSource)]);
      result = laser;
    } else if (command.action === "measure-points") {
      const from = command.from;
      const to = command.to;
      if (!Array.isArray(from) || !Array.isArray(to) || from.length !== 3 || to.length !== 3) {
        throw new Error("measure-points needs from and to as [x, y, z] in model axes");
      }
      const a = toScenePoint(from);
      const b = toScenePoint(to);
      const locked = command.axis ? constrainToAxis(a, b, String(command.axis).toLowerCase()) : b;
      addMarker(a);
      addMarker(locked);
      axisLock = command.axis ? String(command.axis).toLowerCase() : "";
      commitMeasurement(a, locked);
      axisLock = "";
      const last = measurements[measurements.length - 1];
      result = {
        distance: last.distance,
        delta: { x: last.delta[0], y: last.delta[2], z: last.delta[1] },
      };
    } else if (command.action === "measure-angle") {
      // Three points in the model's axes: an end, the corner, the other end.
      const points = ["from", "at", "to"].map((key) => {
        const raw = command[key];
        if (!Array.isArray(raw) || raw.length !== 3) {
          throw new Error("measure-angle needs from, at and to as [x, y, z] in model axes");
        }
        return toScenePoint(raw);
      });
      for (const point of points) addMarker(point);
      drawPath(points, false);
      const measuredAngle = angleMeasure(points[0], points[1], points[2]);
      result = recordMeasurement(
        "angle", measuredAngle, "angle",
        `${measuredAngle.degrees.toFixed(1)}°`, points[1],
        points.map((point) => anchorAt(point, null)));
    } else if (command.action === "measure-area") {
      const raw = Array.isArray(command.points) ? command.points : [];
      if (raw.length < 3) throw new Error("measure-area needs at least three points");
      const points = areaOutline(raw.map((entry) => {
        if (!Array.isArray(entry) || entry.length !== 3) {
          throw new Error("every point must be [x, y, z] in model axes");
        }
        return toScenePoint(entry);
      }));
      if (points.length < 3) {
        throw new Error("measure-area needs at least three distinct points");
      }
      for (const point of points) addMarker(point);
      drawPath(points, true);
      const measuredArea = polygonMeasure(points);
      const areaCentre = points
        .reduce((sum, p) => sum.add(p), new THREE.Vector3())
        .multiplyScalar(1 / points.length);
      result = recordMeasurement(
        "area", measuredArea, `${points.length} points`,
        formatArea(measuredArea.area), areaCentre,
        points.map((point) => anchorAt(point, null)));
    } else if (command.action === "set-projection") {
      result = { projection: setProjection(command.projection ?? command.kind ?? "perspective") };
    } else if (command.action === "set-section") {
      // Axes arrive in the model's own names, and positions as real heights
      // rather than as a fraction of a bounding box nobody can see.
      if (command.clear === true) {
        for (const axis of AXES) section[axis].on = false;
      }
      const axes = isPlainObject(command.axes) ? command.axes : {};
      for (const [rawName, value] of Object.entries(axes)) {
        const name = String(rawName).toLowerCase();
        const frame = axisFrame[name];
        if (!frame) throw new Error(`Unknown section axis ${rawName}; use x, y or z`);
        const axis = frame.axis;
        const state = section[axis];
        if (value === false || value === null) {
          state.on = false;
          continue;
        }
        const spec = isPlainObject(value) ? value : {};
        const [low, high] = axisRange(axis);
        if (typeof spec.at === "number") {
          const scenePosition = toSceneAxis(axis, spec.at);
          state.t = Math.min(1, Math.max(0, (scenePosition - low) / Math.max(high - low, 1e-9)));
        }
        if (spec.keep === "above" || spec.keep === "below") {
          const below = spec.keep === "below";
          state.flip = frame.sign < 0 ? below : !below;
        }
        state.on = spec.on !== false;
      }
      if (typeof command.slice === "number") setSliceDepth(command.slice);
      for (const axis of AXES) syncSectionRow(axis);
      updateClipping();
      saveSection();
      result = sectionState();
    } else if (command.action === "save-view") {
      const name = String(command.name || "").trim();
      if (!name) throw new Error("save-view needs a name");
      const views = savedViews();
      const found = views.findIndex((view) => view.name === name);
      if (found >= 0) views[found] = captureView(name);
      else {
        views.push(captureView(name));
        if (views.length > MAX_SAVED_VIEWS) views.shift();
      }
      saveUi();
      renderSavedViews();
      result = { name, saved: views.length };
    } else if (command.action === "restore-view") {
      const name = String(command.name || "").trim();
      const view = savedViews().find((entry) => entry.name === name);
      if (!view) throw new Error(`No saved view called ${name || "(none)"}`);
      restoreView(view);
      result = { name, projection: view.projection || "perspective" };
    } else if (command.action === "list-views") {
      result = {
        views: savedViews().map((view) => ({
          name: view.name,
          projection: view.projection || "perspective",
          selection: Array.isArray(view.selection) ? view.selection.length : 0,
        })),
      };
    } else if (command.action === "clear-measurements") {
      clearMeasurements();
      result = { measurements: 0 };
    } else if (command.action === "capture-evidence") {
      result = captureViewerEvidence({
        modelId: command.modelId || command.model_id,
        view: command.view,
        fit: command.fit,
        maxSize: command.maxSize || command.max_size,
        format: command.format,
        quality: command.quality,
      });
    } else {
      throw new Error("Unknown viewer command");
    }
    return result;
  }
}

/** Where a thrown viewer error came from, since nobody sees the console. */
function commandFailure(error) {
  const where = String(error?.stack || "").split(String.fromCharCode(10))[1]?.trim();
  return where ? `${error} (${where})` : String(error);
}

// ---------------------------------------------------------- extension panel
// The panel is a separate component and only loaded when its launcher names
// it. A /viewer tab therefore pays no Agent JavaScript, CSS, layout or memory.
const chatDock = $("chat-dock");
const chatResize = $("chat-dock-resize");
const chatBtn = $("btn-chat");
const extensionPanelPrimary = Boolean(requestedPanel);
const CHAT_DOCK_MIN_WIDTH = 520;
const CHAT_DOCK_DEFAULT_WIDTH = 720;
const CHAT_CANVAS_MIN_WIDTH = 420;
const CHAT_DOCK_RESIZE_WIDTH = 5;
const CHAT_DOCK_OVERLAY_WIDTH = 1040;
let chatPanel = null;
let chatLoadPromise = null;
let chatPanelDefinition = null;
let chatDesiredOpen = extensionPanelPrimary;
let chatRequestVersion = 0;

function loadPanelStylesheet(url) {
  if (!url || document.querySelector(`link[data-extension-style="${url}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  link.dataset.extensionStyle = url;
  document.head.append(link);
}

function visiblePanelFootprint(panelId, splitterId) {
  const panel = $(panelId);
  if (panel.classList.contains("collapsed")) return 0;
  return panel.getBoundingClientRect().width
    + $(splitterId).getBoundingClientRect().width;
}

function availableChatDockWidth() {
  const layoutWidth = $("layout").getBoundingClientRect().width || window.innerWidth;
  return Math.floor(
    layoutWidth
      - visiblePanelFootprint("tree-panel", "split-tree")
      - visiblePanelFootprint("props-panel", "split-props")
      - CHAT_CANVAS_MIN_WIDTH
      - CHAT_DOCK_RESIZE_WIDTH,
  );
}

function chatDockMaxWidth() {
  const viewportCap = Math.round(window.innerWidth * 0.68);
  // Below this breakpoint the dock overlays the viewer and therefore does not
  // consume a canvas flex track. On desktop, account for every visible viewer
  // panel before allowing the chat to grow.
  if (window.innerWidth <= CHAT_DOCK_OVERLAY_WIDTH) {
    return Math.max(CHAT_DOCK_MIN_WIDTH, viewportCap);
  }
  return Math.max(
    CHAT_DOCK_MIN_WIDTH,
    Math.min(viewportCap, availableChatDockWidth()),
  );
}

function currentChatWidth() {
  return uiState.chatWidth
    || chatDock.getBoundingClientRect().width
    || CHAT_DOCK_DEFAULT_WIDTH;
}

function syncChatResizeAria(width = currentChatWidth()) {
  const max = chatDockMaxWidth();
  const value = Math.min(Math.max(Math.round(width), CHAT_DOCK_MIN_WIDTH), max);
  chatResize.setAttribute("aria-valuemin", String(CHAT_DOCK_MIN_WIDTH));
  chatResize.setAttribute("aria-valuemax", String(max));
  chatResize.setAttribute("aria-valuenow", String(value));
  chatResize.setAttribute("aria-valuetext", `${value} pixels`);
  return value;
}

function setChatWidth(width) {
  const value = syncChatResizeAria(width);
  chatDock.style.width = `${value}px`;
  uiState.chatWidth = value;
}

function applyChatWidthForViewport() {
  if (window.innerWidth <= CHAT_DOCK_OVERLAY_WIDTH) {
    chatDock.style.width = "";
    syncChatResizeAria();
  } else if (uiState.chatWidth) {
    setChatWidth(uiState.chatWidth);
  } else {
    syncChatResizeAria();
  }
}

function setChatPanelVisible(visible) {
  if (chatPanel && typeof chatPanel.setVisible === "function") {
    chatPanel.setVisible(Boolean(visible));
  }
}

function applyChatChrome(open) {
  // Let the panel dismiss transient UI while it can still measure its host.
  if (!open) setChatPanelVisible(false);
  document.body.classList.toggle("chat-open", open);
  chatDock.hidden = !open;
  chatResize.hidden = !open || !viewerDocumentOpen;
  chatBtn.setAttribute("aria-pressed", String(open));
  if (viewerDocumentOpen) applyChatWidthForViewport();
  else chatDock.style.width = "";
  applyTreePanel();
  if (open) setChatPanelVisible(true);
  syncViewerSurface();
}

function closePanelsForChat(force = false) {
  let changed = false;
  const propsOpen = propsPanelController.isOpen();
  if (
    propsOpen
    && (force || availableChatDockWidth() < CHAT_DOCK_MIN_WIDTH)
  ) {
    uiState.propsOpen = false;
    applyPropsPanel();
    changed = true;
  }

  const treeOpen = treePanelController.isOpen();
  if (
    treeOpen
    && (force || availableChatDockWidth() < CHAT_DOCK_MIN_WIDTH)
  ) {
    uiState.treeOpen = false;
    applyTreePanel();
    changed = true;
  }
  return changed;
}

async function setChat(open, { force = false } = {}) {
  if (extensionPanelPrimary && !force) open = true;
  const requestVersion = ++chatRequestVersion;
  chatDesiredOpen = Boolean(open);
  uiState.chatOpen = chatDesiredOpen;
  if (chatDesiredOpen && !chatPanelDefinition) {
    applyChatChrome(false);
    return;
  }
  // three panels plus the 3D view do not fit a normal window; the properties
  // panel is the one the chat replaces, so fold it away rather than letterbox
  // the model.
  if (chatDesiredOpen && viewerDocumentOpen) closePanelsForChat(true);
  applyChatChrome(chatDesiredOpen);
  saveUi();
  resize();
  if (!chatDesiredOpen) return;

  try {
    if (!chatPanel) {
      loadPanelStylesheet(chatPanelDefinition.stylesheet_url);
      chatLoadPromise ||= import(chatPanelDefinition.module_url)
        .then((module) => {
          const mountPanel = module.mountPanel || module.mountChat;
          if (typeof mountPanel !== "function") {
            throw new TypeError("extension panel module has no mountPanel export");
          }
          chatPanel ||= mountPanel(chatDock, { viewer: viewerComponentHost.api });
          return chatPanel;
        })
        .finally(() => { chatLoadPromise = null; });
      await chatLoadPromise;
      // Opening and closing can race the lazy import. Reconcile the mounted
      // panel with the latest request before the stale caller returns.
      setChatPanelVisible(chatDesiredOpen);
    }
  } catch (error) {
    console.error("[ifc-console] chat module failed", error);
    if (requestVersion === chatRequestVersion && chatDesiredOpen) {
      chatDesiredOpen = false;
      uiState.chatOpen = false;
      applyChatChrome(false);
      saveUi();
      resize();
      showOverlay(
        "Could not open the assistant",
        "The local chat module did not load.",
        { label: "Try again", run: () => setChat(true) },
        "error",
      );
    }
    return;
  }
  if (requestVersion !== chatRequestVersion || !chatDesiredOpen || !chatPanel) return;
  chatPanel.focus();
}

function reconcileCompactLayout() {
  let changed = chatDesiredOpen && viewerDocumentOpen ? closePanelsForChat() : false;
  if (viewerDocumentOpen) applyChatWidthForViewport();
  else chatDock.style.width = "";
  if (window.innerWidth > 620) {
    if (changed) saveUi();
    syncPanelScrim();
    return;
  }
  const treeOpen = treePanelController.isOpen();
  const propsOpen = propsPanelController.isOpen();
  let compactChanged = false;
  if (chatDesiredOpen) {
    if (treeOpen) {
      uiState.treeOpen = false;
      compactChanged = true;
    }
    if (propsOpen) {
      uiState.propsOpen = false;
      compactChanged = true;
    }
  } else if (treeOpen && propsOpen) {
    uiState.propsOpen = false;
    compactChanged = true;
  }
  if (compactChanged) {
    applyTreePanel();
    applyPropsPanel();
    changed = true;
  }
  if (changed) saveUi();
  syncPanelScrim();
}

// A viewer session with no assistant extension should not offer a button that
// opens a dead panel. The extension manifest supplies its module and styling;
// core never needs to know where the companion package stores those assets.
function setChatAvailable(panel, enabled) {
  const available = Boolean(
    requestedPanel && panel?.name === requestedPanel && enabled,
  );
  chatPanelDefinition = available ? panel : null;
  chatBtn.hidden = !available;
  if (panel?.label) {
    chatBtn.title = `Toggle ${panel.label.toLowerCase()} panel (C)`;
    chatBtn.setAttribute("aria-label", `Toggle the ${panel.label.toLowerCase()} panel`);
  }
  if (!available && chatDesiredOpen) setChat(false, { force: true });
  else if (available && chatDesiredOpen) void setChat(true);
  syncViewerSurface();
}

chatBtn.addEventListener("click", () => {
  if (extensionPanelPrimary && !chatDock.hidden) chatPanel?.focus();
  else setChat(chatDock.hidden);
});

chatResize.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  chatResize.setPointerCapture(e.pointerId);
  const startX = e.clientX;
  const startWidth = chatDock.getBoundingClientRect().width;
  const move = (ev) => {
    setChatWidth(startWidth + (ev.clientX - startX));
    resize();
  };
  const up = () => {
    chatResize.removeEventListener("pointermove", move);
    chatResize.removeEventListener("pointerup", up);
    saveUi();
  };
  chatResize.addEventListener("pointermove", move);
  chatResize.addEventListener("pointerup", up);
});

chatResize.addEventListener("keydown", (event) => {
  if (event.key === "Home") {
    event.preventDefault();
    delete uiState.chatWidth;
    chatDock.style.width = "";
    syncChatResizeAria(CHAT_DOCK_DEFAULT_WIDTH);
    saveUi();
    resize();
    return;
  }
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  const movement = event.key === "ArrowLeft" ? -16 : 16;
  setChatWidth(currentChatWidth() + movement);
  saveUi();
  resize();
});

// Side-panel toggles and drags change the chat's safe maximum without a
// viewport resize. Re-clamp before paint so they cannot squeeze away the 3D
// canvas or leave the dock wider than its current workspace permits.
const chatLayoutObserver = new ResizeObserver(() => {
  if (!chatDesiredOpen || !viewerDocumentOpen) return;
  const changed = closePanelsForChat();
  applyChatWidthForViewport();
  if (changed) saveUi();
  resize();
});
chatLayoutObserver.observe($("tree-panel"));
chatLayoutObserver.observe($("props-panel"));

window.addEventListener("resize", reconcileCompactLayout);
reconcileCompactLayout();

window.addEventListener("keydown", (e) => {
  if (e.defaultPrevented) return;
  if (e.key === "Escape") {
    // an active tool owns Escape first, then the popovers; inside the tool,
    // the pending points and the axis lock go before the mode itself
    if (measureMode) {
      if (pending.length || axisLock) {
        clearPending();
        axisLock = "";
        renderMeasurements();
      } else {
        setMeasureMode(false);
      }
    } else if (!$("measure-card").hidden) {
      setMeasurePanelOpen(false);
      $("btn-tool-measure").focus({ preventScroll: true });
    } else if (activeToolPanel) {
      const trigger = document.querySelector(
        `#viewer-toolbar [data-tool-panel="${activeToolPanel}"]`,
      );
      setToolPanel(null);
      trigger?.focus({ preventScroll: true });
    } else {
      const trigger = POPOVERS.find(([, panelId]) => !$(panelId).hidden)?.[0];
      closePopovers();
      if (trigger) $(trigger).focus();
    }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!isShortcutSurface(e.target)) return;
  const key = e.key.toLowerCase();
  if (key === "m" || key === "a" || key === "r") {
    e.preventDefault();
    const kind = key === "m" ? "distance" : key === "a" ? "angle" : "area";
    setMeasureMode(!(measureMode && measureKind === kind), kind);
  } else if (key === "enter" && measureMode) {
    e.preventDefault();
    if (finishOpenMeasurement()) renderMeasurements();
  } else if (key === "p") {
    e.preventDefault();
    setProjection(isOrtho() ? "perspective" : "orthographic");
  } else if (key === "f") {
    e.preventDefault();
    // Shift focuses instead: isolate the selection and frame it.
    if (e.shiftKey) focusSelection(true);
    else fitTo(null);
  } else if (key === "h") {
    e.preventDefault();
    hideSelection();
  } else if (key === "i") {
    e.preventDefault();
    focusSelection(false);
  } else if (key === "u") {
    e.preventDefault();
    showEverything();
  } else if (key === "t") {
    e.preventDefault();
    setGhostContext(!ghostContext);
  } else if (["1", "2", "3", "4"].includes(key)) {
    e.preventDefault();
    setView({ "1": "top", "2": "front", "3": "right", "4": "iso" }[key], null);
  } else if (key === "v") {
    e.preventDefault();
    setToolPanel(activeToolPanel === "views" ? null : "views", { focus: true });
  } else if (key === "c") {
    e.preventDefault();
    if (!chatBtn.hidden) {
      if (agentWorkspacePrimary && !chatDock.hidden) chatPanel?.focus();
      else setChat(chatDock.hidden);
    }
  } else if (key === "g") {
    e.preventDefault();
    uiState.grid = uiState.grid !== true;
    gridBox.checked = uiState.grid;
    saveUi();
    applySceneSettings();
  } else if (e.key === "?") {
    e.preventDefault();
    togglePopover("btn-help", "help-panel");
  }
});

// ---------------------------------------------------------------- boot
async function boot() {
  if (!token) {
    showOverlay(
      "Missing session token",
      "Open the viewer from the ifc-console terminal with /viewer.",
      null,
      "error",
    );
    return;
  }
  try {
    const res = await api("/api/status");
    if (res.ok) setModelInfo(await res.json());
  } catch { /* the websocket status frame will resync */ }
  connect();
  await loadModel();
}

boot();

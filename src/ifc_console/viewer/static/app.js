/* ifc-console viewer application.
 *
 * Build-free by design: plain ES modules, vendored three.js + web-ifc, no
 * bundler. Everything talks to the ifc-console server on the same origin:
 *   GET /api/model.ifc      the live in-memory model (ETag = fingerprint-rev)
 *   GET /api/elements/{id}  properties for the panel
 *   WS  /ws                 selection out, highlight/camera/screenshot in
 *
 * The viewer is unprivileged: it renders, selects, and reports; it cannot
 * change the model or the session mode.
 */

import * as THREE from "./vendor/three.module.min.js";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { IfcAPI } from "./vendor/web-ifc-api.js";

// ---------------------------------------------------------------- token / api
// The token arrives in the URL fragment so it never reaches the server or its
// logs; keep it per-tab and scrub it from the address bar immediately.
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
const token = hashParams.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (token) sessionStorage.setItem("ifc-console-token", token);
if (hashParams.has("t")) history.replaceState(null, "", location.pathname);

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${token}`, ...(options.headers || {}) };
  return fetch(path, { ...options, headers });
}

// ---------------------------------------------------------------- dom helpers
const $ = (id) => document.getElementById(id);
const overlay = $("overlay");

function showOverlay(text) {
  overlay.textContent = "";
  const card = el("div", "overlay-card");
  card.textContent = text;
  overlay.appendChild(card);
  overlay.hidden = false;
}
function hideOverlay() {
  overlay.hidden = true;
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- three scene
const canvas = $("canvas");
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
} catch (err) {
  showOverlay("WebGL is unavailable in this browser, so the 3D view cannot start.\n"
    + "Enable hardware acceleration or try another browser.");
  throw err;
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x101720);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
camera.position.set(12, 10, 12);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.12;

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

// Theme: the console pushes dark/light over the WS; the chrome follows via
// CSS variables, the 3D canvas and grid via these colors.
const THEME_COLORS = {
  dark: { canvas: 0x101720, gridMinor: 0x24303d, gridMajor: 0x3c536a },
  light: { canvas: 0xe7edf3, gridMinor: 0xc7d2dd, gridMajor: 0x9fb0c0 },
};
let uiTheme = "dark";

function applyTheme(name) {
  const theme = THEME_COLORS[name] ? name : "dark";
  uiTheme = theme;
  document.documentElement.dataset.theme = theme;
  const colors = THEME_COLORS[theme];
  scene.background.set(colors.canvas);
  grid.material.uniforms.uMinor.value.set(colors.gridMinor);
  grid.material.uniforms.uMajor.value.set(colors.gridMajor);
}

function updateGround() {
  const box = new THREE.Box3().setFromObject(modelRoot);
  const empty = box.isEmpty();
  const radius = empty ? 20 : box.getBoundingSphere(new THREE.Sphere()).radius;
  // enough clearance below the model that the depth buffer reliably hides
  // the grid under floors (a few mm would z-fight at building distances)
  groundY = empty ? 0 : box.min.y - Math.max(0.05, radius * 0.002);
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
}

const modelRoot = new THREE.Group();
scene.add(modelRoot);

let viewportWidth = 0;
let viewportHeight = 0;
function resize() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  if (w === viewportWidth && h === viewportHeight) return;
  viewportWidth = w;
  viewportHeight = h;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  // Re-render in the same frame; the observer fires before paint, so the
  // resized buffer never appears blank or stretched while dragging a splitter.
  renderer.render(scene, camera);
}
new ResizeObserver(resize).observe(canvas.parentElement);
resize();

renderer.setAnimationLoop(() => {
  controls.update();
  // Keep the finite carrier plane centered under the view. Snapping changes
  // only plane coverage; shader coordinates remain world-locked and stable.
  grid.position.x = Math.round(controls.target.x / 100) * 100;
  grid.position.z = Math.round(controls.target.z / 100) * 100;
  renderer.render(scene, camera);
});

// ---------------------------------------------------------------- model state
let ifcApi = null;            // created once, reused across reloads
let currentEtag = null;
let loading = false;
let reloadQueued = false;

const groups = new Map();     // expressID -> THREE.Group (one per product)
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

const materialCache = new Map();
function materialFor(color, alpha) {
  const key = `${color.x.toFixed(3)}|${color.y.toFixed(3)}|${color.z.toFixed(3)}|${alpha.toFixed(3)}`;
  let mat = materialCache.get(key);
  if (!mat) {
    mat = new THREE.MeshLambertMaterial({
      color: new THREE.Color(color.x, color.y, color.z),
      side: THREE.DoubleSide,
    });
    if (alpha < 1) {
      mat.transparent = true;
      mat.opacity = alpha;
      mat.depthWrite = false;
    }
    materialCache.set(key, mat);
  }
  return mat;
}

const selectMaterial = new THREE.MeshLambertMaterial({
  color: 0x4f8ff7, emissive: 0x18345f, side: THREE.DoubleSide,
});
const highlightMaterials = new Map();
function highlightMaterialFor(hex) {
  let mat = highlightMaterials.get(hex);
  if (!mat) {
    const c = new THREE.Color(hex);
    mat = new THREE.MeshLambertMaterial({
      color: c, emissive: c.clone().multiplyScalar(0.35), side: THREE.DoubleSide,
    });
    highlightMaterials.set(hex, mat);
  }
  return mat;
}

const themeMaterials = new Map();
function themeMaterialFor(hex) {
  let mat = themeMaterials.get(hex);
  if (!mat) {
    mat = new THREE.MeshLambertMaterial({
      color: new THREE.Color(hex), side: THREE.DoubleSide,
    });
    themeMaterials.set(hex, mat);
  }
  return mat;
}

// ---------------------------------------------------------------- model load
async function ensureIfcApi() {
  if (ifcApi) return ifcApi;
  ifcApi = new IfcAPI();
  ifcApi.SetWasmPath("/viewer/static/vendor/", true);
  await ifcApi.Init();
  return ifcApi;
}

function disposeModel() {
  for (const group of groups.values()) {
    for (const mesh of group.children) mesh.geometry.dispose();
  }
  modelRoot.clear();
  groups.clear();
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
}

async function loadModel() {
  if (loading) {
    reloadQueued = true;
    return;
  }
  loading = true;
  try {
    showOverlay("fetching model…");
    const headers = currentEtag ? { "If-None-Match": currentEtag } : {};
    const res = await api("/api/model.ifc", { headers });
    if (res.status === 304) {
      hideOverlay();
      return;
    }
    if (res.status === 404) {
      disposeModel();
      renderTree(null);
      setModelInfo(null);
      showOverlay("no model loaded\npick one with /file in the ifc-console terminal");
      return;
    }
    if (res.status === 413) {
      const body = await res.json().catch(() => ({}));
      showOverlay(`model too large for the viewer\n${body.message || ""}`);
      return;
    }
    if (res.status === 401) {
      showOverlay("unauthorized\nre-open the viewer from the ifc-console terminal (/viewer)");
      return;
    }
    if (!res.ok) {
      showOverlay(`could not fetch the model (HTTP ${res.status})`);
      return;
    }
    currentEtag = res.headers.get("ETag");
    const buffer = new Uint8Array(await res.arrayBuffer());

    showOverlay("parsing IFC…");
    await buildScene(buffer);
    hideOverlay();
    fitTo(null);
    // the hub's change frames carry no name/schema; re-sync the top bar
    refreshStatus();
  } catch (err) {
    console.error("[ifc-console] model load failed", err);
    showOverlay(`viewer error: ${err.message || err}`);
  } finally {
    loading = false;
    if (reloadQueued) {
      reloadQueued = false;
      loadModel();
    }
  }
}

async function buildScene(buffer) {
  const api3 = await ensureIfcApi();
  // Live edits trigger rebuilds; carry selection and highlights across so a
  // refresh does not silently drop what the user (or the LLM) marked.
  const keepSelection = [...selection].map((id) => guidOf.get(id)).filter(Boolean);
  const keepHighlight = [...highlightSet].map((id) => guidOf.get(id)).filter(Boolean);
  const keepIsolate = isolateSet !== null;
  disposeModel();

  const modelID = api3.OpenModel(buffer, { COORDINATE_TO_ORIGIN: true });
  try {
    let meshCount = 0;
    api3.StreamAllMeshes(modelID, (flatMesh) => {
      const group = new THREE.Group();
      group.userData.expressID = flatMesh.expressID;
      const parts = flatMesh.geometries;
      for (let i = 0; i < parts.size(); i++) {
        const placed = parts.get(i);
        const geometry = api3.GetGeometry(modelID, placed.geometryExpressID);
        const verts = api3.GetVertexArray(
          geometry.GetVertexData(), geometry.GetVertexDataSize());
        const indices = api3.GetIndexArray(
          geometry.GetIndexData(), geometry.GetIndexDataSize());

        // web-ifc interleaves position(3) + normal(3), already Y-up.
        const buf = new THREE.InterleavedBuffer(new Float32Array(verts), 6);
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.InterleavedBufferAttribute(buf, 3, 0));
        geo.setAttribute("normal", new THREE.InterleavedBufferAttribute(buf, 3, 3));
        geo.setIndex(new THREE.BufferAttribute(new Uint32Array(indices), 1));

        const mesh = new THREE.Mesh(
          geo, materialFor(placed.color, placed.color.w ?? 1));
        mesh.applyMatrix4(new THREE.Matrix4().fromArray(placed.flatTransformation));
        mesh.userData.expressID = flatMesh.expressID;
        mesh.userData.baseMaterial = mesh.material;
        group.add(mesh);
        geometry.delete();
        meshCount++;
      }
      groups.set(flatMesh.expressID, group);
      modelRoot.add(group);
    });

    // GlobalId maps for selection sync and the properties panel.
    for (const id of groups.keys()) {
      try {
        const line = api3.GetLine(modelID, id);
        const guid = line && line.GlobalId && line.GlobalId.value;
        if (guid) {
          guidOf.set(id, guid);
          expressOf.set(guid, id);
        }
      } catch { /* products without GlobalId are unselectable, fine */ }
    }

    const spatial = await api3.properties.getSpatialStructure(modelID);
    annotateTreeNames(api3, modelID, spatial);
    renderTree(spatial);
    $("stats").textContent = `${groups.size} products, ${meshCount} meshes`;
    updateGround();

    for (const guid of keepSelection) {
      const id = expressOf.get(guid);
      if (id !== undefined) selection.add(id);
    }
    highlightSet = new Set(
      keepHighlight.map((g) => expressOf.get(g)).filter((id) => id !== undefined));
    if (keepIsolate && highlightSet.size) isolateSet = new Set(highlightSet);
    applyAppearance();
    applyVisibility();
    markTreeSelection();
    updateSelectionInfo();
    updateHighlightInfo();
  } finally {
    // Everything needed later is in three.js buffers and maps; free the WASM copy.
    api3.CloseModel(modelID);
  }
}

function annotateTreeNames(api3, modelID, node) {
  try {
    const line = api3.GetLine(modelID, node.expressID);
    node._name = (line && line.Name && line.Name.value) || null;
  } catch {
    node._name = null;
  }
  for (const child of node.children || []) annotateTreeNames(api3, modelID, child);
}

// ---------------------------------------------------------------- spatial tree
const SPATIAL_TYPES = new Set([
  "IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE",
  "IFCFACILITY", "IFCBRIDGE", "IFCROAD", "IFCRAILWAY", "IFCMARINEFACILITY",
]);

function isSpatial(node) {
  return SPATIAL_TYPES.has(String(node.type || "").toUpperCase());
}

function descendantElements(node, acc = []) {
  for (const child of node.children || []) {
    if (!isSpatial(child) && groups.has(child.expressID)) acc.push(child.expressID);
    descendantElements(child, acc);
  }
  return acc;
}

function renderTree(rootNode) {
  const container = $("tree");
  container.textContent = "";
  if (!rootNode) return;
  container.appendChild(buildTreeItem(rootNode, 0));
}

function buildTreeItem(node, depth) {
  const ul = el("ul");
  const li = el("li");
  const row = el("div", "tree-row");
  const children = node.children || [];
  const spatial = isSpatial(node);
  // Project / Site / Building / Storey come pre-expanded; elements collapsed.
  const expanded = depth < 4;

  const toggle = el("span", "tree-toggle", children.length ? (expanded ? "▾" : "▸") : " ");
  row.appendChild(toggle);

  if (spatial && children.length) {
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.title = "toggle visibility of this branch";
    checkbox.addEventListener("change", () => {
      for (const id of descendantElements(node)) {
        if (checkbox.checked) hiddenByTree.delete(id);
        else hiddenByTree.add(id);
      }
      applyVisibility();
    });
    row.appendChild(checkbox);
  }

  const cls = String(node.type || "?");
  const label = el("span", "tree-label");
  label.appendChild(el("span", null, node._name ? `${node._name} ` : ""));
  label.appendChild(el("span", "cls", node._name ? `(${cls})` : cls));
  label.dataset.expressId = node.expressID;
  label.title = node._name ? `${node._name} (${cls})` : cls;
  row.appendChild(label);
  li.appendChild(row);

  let kids = null;
  const setOpen = (open) => {
    if (!kids) return;
    kids.hidden = !open;
    toggle.textContent = kids.hidden ? "▸" : "▾";
  };
  if (children.length) {
    kids = el("div");
    for (const child of children) kids.appendChild(buildTreeItem(child, depth + 1));
    li.appendChild(kids);
    setOpen(expanded);
    toggle.addEventListener("click", () => setOpen(kids.hidden));
  }

  // Clicking a name only selects; framing stays on F or the view tools.
  label.addEventListener("click", () => {
    if (spatial) {
      setOpen(true); // the label is a much bigger target than the arrow
      setSelection(descendantElements(node), false);
    } else if (groups.has(node.expressID)) {
      setSelection([node.expressID], false);
    }
  });
  ul.appendChild(li);
  return ul;
}

function markTreeSelection() {
  for (const label of document.querySelectorAll(".tree-label.selected")) {
    label.classList.remove("selected");
  }
  for (const id of selection) {
    const label = document.querySelector(`.tree-label[data-express-id="${id}"]`);
    if (label) label.classList.add("selected");
  }
}

// ---------------------------------------------------------------- appearance
function applyAppearance() {
  for (const [id, group] of groups) {
    let material = null;
    if (highlightSet.has(id)) material = highlightMaterialFor(highlightColor);
    else if (selection.has(id)) material = selectMaterial;
    else {
      const themed = themeByGuid.get(guidOf.get(id));
      if (themed) material = themeMaterialFor(themed);
    }
    for (const mesh of group.children) {
      mesh.material = material || mesh.userData.baseMaterial;
    }
  }
}

function applyVisibility() {
  for (const [id, group] of groups) {
    const isolatedOut =
      (isolateSet !== null && !isolateSet.has(id))
      || (userIsolateSet !== null && !userIsolateSet.has(id));
    group.visible = !hiddenByTree.has(id) && !hiddenManual.has(id) && !isolatedOut;
  }
  updateVisibilityInfo();
}

// ---------------------------------------------------------------- selection
function setSelection(ids, additive) {
  if (!additive) selection.clear();
  for (const id of ids) {
    if (additive && selection.has(id)) selection.delete(id);
    else selection.add(id);
  }
  applyAppearance();
  markTreeSelection();
  updateSelectionInfo();
  sendSelection();
  const last = ids[ids.length - 1];
  if (last !== undefined && selection.has(last) && guidOf.has(last)) {
    showProperties(guidOf.get(last));
  } else if (!selection.size) {
    clearProperties();
  }
}

function updateSelectionInfo() {
  const n = selection.size;
  updateToolButtons();
  if (!n) {
    $("sel-info").textContent = "No selection";
    return;
  }
  const shown = [...selection].slice(0, 3)
    .map((id) => guidOf.get(id) || `#${id}`).join(", ");
  $("sel-info").textContent = `${n} selected · ${shown}${n > 3 ? ", …" : ""}`;
}

function updateHighlightInfo() {
  const n = highlightSet.size;
  $("hl-info").textContent = n ? `${n} highlighted · ${highlightColor}` : "";
  // The clear control only earns space when there is something to clear.
  $("btn-clear-hl").hidden = n === 0;
}

function sendSelection() {
  const guids = [...selection].map((id) => guidOf.get(id)).filter(Boolean);
  wsSend({ type: "selection", guids });
}

// Click-to-select with a small movement threshold so orbiting never selects.
const raycaster = new THREE.Raycaster();
const DRAG_THRESHOLD = 6;
let downAt = null;
canvas.addEventListener("pointerdown", (e) => {
  downAt = [e.clientX, e.clientY];
  canvas.classList.remove("is-dragging");
});
canvas.addEventListener("pointermove", (e) => {
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  if (moved > DRAG_THRESHOLD) canvas.classList.add("is-dragging");
});
canvas.addEventListener("pointerup", (e) => {
  canvas.classList.remove("is-dragging");
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]);
  downAt = null;
  if (moved > DRAG_THRESHOLD || e.button !== 0) return;

  const rect = canvas.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((e.clientX - rect.left) / rect.width) * 2 - 1,
    -((e.clientY - rect.top) / rect.height) * 2 + 1,
  );
  raycaster.setFromCamera(pointer, camera);
  const visible = [];
  for (const group of groups.values()) {
    if (group.visible) visible.push(...group.children);
  }
  const hits = raycaster.intersectObjects(visible, false);
  const hit = hits.find((h) => h.object.userData.expressID !== undefined);
  if (hit) setSelection([hit.object.userData.expressID], e.ctrlKey || e.metaKey);
  else if (!e.ctrlKey && !e.metaKey) setSelection([], false);
});
canvas.addEventListener("pointercancel", () => {
  downAt = null;
  canvas.classList.remove("is-dragging");
});
window.addEventListener("blur", () => {
  downAt = null;
  canvas.classList.remove("is-dragging");
});

// ---------------------------------------------------------------- camera
function boundsOf(ids) {
  const box = new THREE.Box3();
  let any = false;
  const wanted = ids ? new Set(ids) : null;
  for (const [id, group] of groups) {
    if (wanted && !wanted.has(id)) continue;
    if (!wanted && !group.visible) continue;
    box.expandByObject(group);
    any = true;
  }
  return any ? box : null;
}

function fitTo(ids) {
  const box = boundsOf(ids);
  if (!box) return;
  const center = box.getCenter(new THREE.Vector3());
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const distance = Math.max(sphere.radius, 1) * 2.2;
  const direction = camera.position.clone().sub(controls.target).normalize();
  camera.position.copy(center.clone().add(direction.multiplyScalar(distance)));
  controls.target.copy(center);
  grid.position.set(center.x, groundY, center.z);
  camera.near = Math.max(distance / 1000, 0.01);
  camera.far = distance * 100;
  camera.updateProjectionMatrix();
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
  const center = box.getCenter(new THREE.Vector3());
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const distance = Math.max(sphere.radius, 1) * 2.2;
  camera.position.copy(
    center.clone().add(new THREE.Vector3(...dir).normalize().multiplyScalar(distance)));
  controls.target.copy(center);
  grid.position.set(center.x, groundY, center.z);
  camera.near = Math.max(distance / 1000, 0.01);
  camera.far = distance * 100;
  camera.updateProjectionMatrix();
}

function fitTargetIds(fit) {
  if (fit === "selection" && selection.size) return [...selection];
  if (fit === "highlighted" && highlightSet.size) return [...highlightSet];
  return null;
}

// ---------------------------------------------------------------- properties
async function showProperties(guid) {
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "loading…"));
  let detail;
  try {
    const res = await api(`/api/elements/${encodeURIComponent(guid)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    detail = await res.json();
  } catch (err) {
    panel.textContent = "";
    panel.appendChild(el("p", "hint", `could not load properties (${err.message})`));
    return;
  }
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
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "click an element to inspect it"));
}

// ---------------------------------------------------------------- websocket
let ws = null;
let wsAttempts = 0;
let reloadTimer = null;

function wsSend(frame) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
}

function scheduleReload() {
  // Bursts of edits collapse into one refetch (2 s debounce).
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(loadModel, 2000);
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/ws`);

  ws.addEventListener("open", () => {
    wsAttempts = 0;
    $("live").classList.remove("off");
    $("live").setAttribute("aria-label", "Server connected");
    wsSend({ type: "hello", token });
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

  ws.addEventListener("close", () => {
    $("live").classList.add("off");
    $("live").setAttribute("aria-label", "Server disconnected");
    wsAttempts += 1;
    const delay = Math.min(15000, 1000 * 2 ** Math.min(wsAttempts, 4));
    setTimeout(connect, delay);
  });
  ws.addEventListener("error", () => ws.close());
}

function handleFrame(frame) {
  switch (frame.type) {
    case "status":
      setModelInfo(frame);
      if (frame.theme) applyTheme(frame.theme);
      if (frame.etag && frame.etag !== currentEtag) scheduleReload();
      break;
    case "model_updated":
      if (frame.dirty !== undefined) $("dirty").hidden = !frame.dirty;
      if (frame.reason === "loaded") applyColorThemeFrame({ clear: true });
      if (!frame.etag || frame.etag !== currentEtag) scheduleReload();
      break;
    case "mode_changed":
      setMode(frame.mode);
      break;
    case "theme":
      applyTheme(frame.theme);
      break;
    case "highlight":
      applyHighlightFrame(frame);
      break;
    case "color_theme":
      applyColorThemeFrame(frame);
      break;
    case "camera":
      if (frame.view && frame.view !== "current") setView(frame.view, fitTargetIds(frame.fit));
      else if (frame.fit) fitTo(fitTargetIds(frame.fit));
      break;
    case "screenshot_request":
      handleScreenshot(frame);
      break;
    case "ping":
      wsSend({ type: "pong" });
      break;
    default:
      break;
  }
}

function applyHighlightFrame(frame) {
  if (frame.clear) {
    highlightSet.clear();
    isolateSet = null;
  } else {
    highlightColor = frame.color || "#ff3b30";
    highlightSet = new Set(
      (frame.guids || []).map((g) => expressOf.get(g)).filter((id) => id !== undefined));
    isolateSet = frame.isolate ? new Set(highlightSet) : null;
  }
  applyAppearance();
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
function handleScreenshot(frame) {
  try {
    if (frame.view && frame.view !== "current") {
      setView(frame.view, fitTargetIds(frame.fit));
    } else if (frame.fit) {
      const ids = fitTargetIds(frame.fit);
      fitTo(ids); // fit "all" passes null and frames everything visible
    }
    // Render explicitly in this task so the buffer is fresh when read.
    controls.update();
    renderer.render(scene, camera);

    const source = renderer.domElement;
    const maxSize = Math.max(64, Math.min(2048, frame.max_size || 800));
    const scale = Math.min(1, maxSize / Math.max(source.width, source.height));
    const w = Math.max(1, Math.round(source.width * scale));
    const h = Math.max(1, Math.round(source.height * scale));

    const target = document.createElement("canvas");
    target.width = w;
    target.height = h;
    target.getContext("2d").drawImage(source, 0, 0, w, h);

    const mime = frame.format === "png" ? "image/png" : "image/jpeg";
    const dataUrl = target.toDataURL(mime, (frame.quality || 85) / 100);
    wsSend({
      type: "screenshot_response",
      id: frame.id,
      data_b64: dataUrl.slice(dataUrl.indexOf(",") + 1),
      width: w,
      height: h,
    });
  } catch (err) {
    console.error("[ifc-console] screenshot failed", err);
    wsSend({ type: "screenshot_response", id: frame.id, error: String(err) });
  }
}

// ---------------------------------------------------------------- status bar
function setMode(mode) {
  const chip = $("mode");
  // A colored status dot (drawn in CSS) carries the meaning; the text stays clean.
  chip.textContent = mode;
  chip.dataset.mode = mode;
}

function setModelInfo(status) {
  const label = $("model-name");
  if (status) {
    label.textContent = status.model || "no model";
    label.title = status.model || "";
    const schema = $("schema");
    schema.textContent = status.schema || "";
    schema.hidden = !status.schema;
    if (status.mode) setMode(status.mode);
    $("dirty").hidden = !status.dirty;
    if (status.highlight) applyHighlightFrame(status.highlight);
    if (status.color_theme) applyColorThemeFrame(status.color_theme);
  } else {
    label.textContent = "no model";
    label.title = "";
    $("schema").hidden = true;
    $("dirty").hidden = true;
  }
  document.title = status && status.model
    ? `${status.model} · ifc-console viewer` : "ifc-console viewer";
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
  $("tool-hide").disabled = none;
  $("tool-fit-sel").disabled = none;
}

function updateVisibilityInfo() {
  let hidden = 0;
  for (const group of groups.values()) {
    if (!group.visible) hidden++;
  }
  $("tool-hidden-info").textContent =
    hidden ? `${hidden} of ${groups.size} elements hidden` : "";
  $("tool-show-all").disabled = hidden === 0;
}

$("tool-isolate").addEventListener("click", () => {
  if (!selection.size) return;
  userIsolateSet = new Set(selection);
  applyVisibility();
});
$("tool-hide").addEventListener("click", () => {
  if (!selection.size) return;
  for (const id of selection) hiddenManual.add(id);
  setSelection([], false);
  applyVisibility();
});
$("tool-show-all").addEventListener("click", () => {
  userIsolateSet = null;
  isolateSet = null;
  hiddenManual.clear();
  hiddenByTree.clear();
  for (const box of document.querySelectorAll('#tree input[type="checkbox"]')) {
    box.checked = true;
  }
  applyVisibility();
});
$("tool-fit-sel").addEventListener("click", () => {
  if (selection.size) fitTo([...selection]);
});
$("tool-fit-all").addEventListener("click", () => fitTo(null));
for (const btn of document.querySelectorAll("#tools-panel [data-view]")) {
  btn.addEventListener("click", () => setView(btn.dataset.view, null));
}
updateToolButtons();
updateVisibilityInfo();

// ---------------------------------------------------------------- ui state
// Panel widths/visibility and scene settings persist across sessions.
const uiState = (() => {
  try {
    return JSON.parse(localStorage.getItem("ifc-console-viewer-ui") || "{}");
  } catch {
    return {};
  }
})();
function saveUi() {
  localStorage.setItem("ifc-console-viewer-ui", JSON.stringify(uiState));
}

function applySceneSettings() {
  grid.visible = uiState.grid === true;
  if (axes) axes.visible = uiState.axes === true;
}

function initSidePanel(panelId, splitId, btnId, widthKey, openKey, side) {
  const panel = $(panelId);
  const splitter = $(splitId);
  const btn = $(btnId);
  const clampW = (w) => {
    // Keep a useful canvas visible when both side panels are open.
    const max = Math.max(
      160,
      Math.min(window.innerWidth * 0.45, (window.innerWidth - 280) / 2),
    );
    return Math.min(Math.max(Math.round(w), 160), max);
  };
  const apply = () => {
    const open = uiState[openKey] !== false;
    panel.classList.toggle("collapsed", !open);
    splitter.classList.toggle("collapsed", !open);
    btn.setAttribute("aria-pressed", String(open));
    panel.style.width = uiState[widthKey] ? `${clampW(uiState[widthKey])}px` : "";
  };
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    splitter.setPointerCapture(e.pointerId);
    splitter.classList.add("dragging");
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      uiState[widthKey] = clampW(side === "left" ? startW + dx : startW - dx);
      panel.style.width = `${uiState[widthKey]}px`;
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
  btn.addEventListener("click", () => {
    const open = uiState[openKey] !== false;
    uiState[openKey] = !open;
    saveUi();
    apply();
  });
  window.addEventListener("resize", apply);
  apply();
}

initSidePanel("tree-panel", "split-tree", "btn-panel-tree", "treeWidth", "treeOpen", "left");
initSidePanel("props-panel", "split-props", "btn-panel-props", "propsWidth", "propsOpen", "right");

const POPOVERS = [
  ["btn-settings", "settings-panel"],
  ["btn-help", "help-panel"],
  ["btn-tools", "tools-panel"],
];
function closePopovers(exceptId) {
  for (const [btnId, panelId] of POPOVERS) {
    if (panelId === exceptId) continue;
    $(panelId).hidden = true;
    $(btnId).setAttribute("aria-expanded", "false");
  }
}
for (const [btnId, panelId] of POPOVERS) {
  $(btnId).addEventListener("click", (e) => {
    e.stopPropagation();
    const panel = $(panelId);
    panel.hidden = !panel.hidden;
    $(btnId).setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) closePopovers(panelId);
  });
  $(panelId).addEventListener("click", (e) => e.stopPropagation());
}
document.addEventListener("click", () => closePopovers());

const gridBox = $("set-grid");
const axesBox = $("set-axes");
gridBox.checked = uiState.grid === true;
axesBox.checked = uiState.axes === true;
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
applySceneSettings();

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closePopovers();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.target !== document.body) return;
  if (e.key === "f") {
    fitTo(null);
  } else if (e.key === "g") {
    uiState.grid = uiState.grid !== true;
    gridBox.checked = uiState.grid;
    saveUi();
    applySceneSettings();
  }
});

// ---------------------------------------------------------------- boot
async function boot() {
  if (!token) {
    showOverlay("missing session token\nopen the viewer from the ifc-console terminal (/viewer)");
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

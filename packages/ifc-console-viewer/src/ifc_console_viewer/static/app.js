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

// ---------------------------------------------------------------- token / api
// The token arrives in the URL fragment so it never reaches the server or its
// logs; keep it per-tab and scrub it from the address bar immediately.
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
// read the query before scrubbing; ?chat=1 opens the split view on load
const queryParams = new URLSearchParams(location.search);
const token = hashParams.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (token) sessionStorage.setItem("ifc-console-token", token);
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

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
camera.position.set(12, 10, 12);

const controls = new OrbitControls(camera, canvas);
controls.dampingFactor = 0.12;
controls.listenToKeyEvents(canvas);
const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
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
  invalidate();
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

function invalidate() {
  needsRender = true;
}

controls.addEventListener("start", () => {
  interacting = true;
  userMovedCamera = true;
});
controls.addEventListener("end", () => {
  interacting = false;
});
// OrbitControls applies wheel zoom synchronously. Its later update() can then
// report no movement, so listen to change directly and never miss that frame.
controls.addEventListener("change", invalidate);

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
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  // Re-render in the same frame; the observer fires before paint, so the
  // resized buffer never appears blank or stretched while dragging a splitter.
  renderer.render(scene, camera);
}
new ResizeObserver(resize).observe(canvas.parentElement);
resize();

function renderNow() {
  grid.position.x = Math.round(controls.target.x / 100) * 100;
  grid.position.z = Math.round(controls.target.z / 100) * 100;
  const t0 = performance.now();
  renderer.render(scene, camera);
  lastRenderMs = performance.now() - t0;
  needsRender = false;
}

renderer.setAnimationLoop(() => {
  const moved = controls.update();
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
});

// Screenshots and picking need a full-resolution buffer regardless of the
// adaptive scale the orbit interaction may have left behind.
function ensureFullResolution() {
  if (resScale !== 1) {
    resScale = 1;
    applyResolution();
  }
}

// ---------------------------------------------------------------- model state
let currentEtag = null;
let loading = false;
let reloadQueued = false;
// Which resident model this tab shows. null follows the console's active
// model; a model_id pins one of the attached ones (read-only, like the tools).
let viewModelId = null;
let modelRows = [];

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
const activeClipPlanes = [];
const section = {
  x: { on: false, t: 1, flip: false },
  y: { on: false, t: 1, flip: false },
  z: { on: false, t: 1, flip: false },
};

// Patched Lambert: forward the element index, discard hidden elements,
// substitute the override color, add the glow term.
const patchedMaterials = new Set();
function patchMaterial(mat) {
  patchedMaterials.add(mat);
  mat.clippingPlanes = activeClipPlanes;
  mat.onBeforeCompile = (shader) => {
    const previous = mat.userData.ifcShader;
    if (previous) liveShaders.delete(previous);
    mat.userData.ifcShader = shader;
    shader.uniforms.uStateTex = { value: stateTex };
    shader.uniforms.uOverrideTex = { value: overrideTex };
    shader.uniforms.uStateSize = { value: new THREE.Vector2(STATE_W, stateH) };
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>",
        "#include <common>\nattribute float aElementIndex;\nvarying float vIfcIndex;")
      .replace("#include <begin_vertex>",
        "vIfcIndex = aElementIndex;\n#include <begin_vertex>");
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>",
        "#include <common>\n"
        + "uniform sampler2D uStateTex;\n"
        + "uniform sampler2D uOverrideTex;\n"
        + "uniform vec2 uStateSize;\n"
        + "varying float vIfcIndex;")
      .replace("#include <clipping_planes_fragment>",
        "float ifcId = floor(vIfcIndex + 0.5);\n"
        + "vec2 ifcUv = vec2((mod(ifcId, uStateSize.x) + 0.5) / uStateSize.x,\n"
        + "                  (floor(ifcId / uStateSize.x) + 0.5) / uStateSize.y);\n"
        + "vec4 ifcState = texture2D(uStateTex, ifcUv);\n"
        + "if (ifcState.r < 0.5) discard;\n"
        + "vec4 ifcOverride = texture2D(uOverrideTex, ifcUv);\n"
        + "#include <clipping_planes_fragment>")
      .replace("#include <color_fragment>",
        "#include <color_fragment>\n"
        + "diffuseColor.rgb = mix(diffuseColor.rgb, ifcOverride.rgb, step(0.5, ifcOverride.a));")
      .replace("#include <emissivemap_fragment>",
        "#include <emissivemap_fragment>\n"
        + "totalEmissiveRadiance += ifcOverride.rgb * ifcState.g;");
    liveShaders.add(shader);
  };
  mat.customProgramCacheKey = () => "ifc-state";
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
      if (texture2D(uStateTex, uv).r < 0.5) discard;
      float enc = id + 1.0;
      gl_FragColor = vec4(
        floor(enc / 65536.0) / 255.0,
        floor(mod(enc, 65536.0) / 256.0) / 255.0,
        mod(enc, 256.0) / 255.0,
        1.0);
    }`,
});
// Same 1x1 trick for measuring: encode the distance from the camera to the
// fragment into 24 bits. Merged chunks free their CPU arrays after upload, so
// there is no vertex data left to raycast against; the GPU is the only source
// of a surface point. Clipping is compiled in so a sectioned-away face is not
// measurable and the surface behind it answers instead.
const depthMaterial = new THREE.ShaderMaterial({
  side: THREE.DoubleSide,
  clipping: true,
  uniforms: {
    uStateTex: { value: null },
    uStateSize: { value: new THREE.Vector2(STATE_W, stateH) },
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
    uniform float uFar;
    #include <clipping_planes_pars_fragment>
    void main() {
      #include <clipping_planes_fragment>
      float id = floor(vIfcIndex + 0.5);
      vec2 uv = vec2((mod(id, uStateSize.x) + 0.5) / uStateSize.x,
                     (floor(id / uStateSize.x) + 0.5) / uStateSize.y);
      if (texture2D(uStateTex, uv).r < 0.5) discard;
      float d = clamp(length(vMeasureViewPosition) / uFar, 0.0, 1.0);
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
// geometries register once; placements
// either bake into growing merged buffers (first occurrence) or promote the
// geometry to an InstancedMesh (second occurrence onward). Merged buffers
// finalize into meshes at a vertex limit and are bucketed into coarse spatial
// cells so frustum culling has something to cut.
const CHUNK_VERTEX_LIMIT = 500_000;
const SPATIAL_SPLIT_VERTS = 50_000;
const ORIGIN_THRESHOLD = 1e4;

const elements = new Map();       // expressID -> {index, box: number[6]}
const elementsByIndex = [];       // dense index -> expressID
const registry = new Map();       // geometryID -> {positions, normals, indices, box, radius}
const geomUse = new Map();        // `${gid}:${alphaKey}` -> "merged" | InstEntry
let accumulators = new Map();     // cellKey -> Accumulator
let modelBox = null;              // [minx,miny,minz,maxx,maxy,maxz] world f64
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
    };
    elements.set(expressID, rec);
    elementsByIndex.push(expressID);
  }
  return rec;
}

const _m4 = new THREE.Matrix4();
const _n3 = new THREE.Matrix3();

function unionBoxCorners(target, box, m) {
  for (let c = 0; c < 8; c++) {
    const x = box[c & 1 ? 3 : 0];
    const y = box[c & 2 ? 4 : 1];
    const z = box[c & 4 ? 5 : 2];
    const wx = m[0] * x + m[4] * y + m[8] * z + m[12] - origin[0];
    const wy = m[1] * x + m[5] * y + m[9] * z + m[13] - origin[1];
    const wz = m[2] * x + m[6] * y + m[10] * z + m[14] - origin[2];
    if (wx < target[0]) target[0] = wx;
    if (wy < target[1]) target[1] = wy;
    if (wz < target[2]) target[2] = wz;
    if (wx > target[3]) target[3] = wx;
    if (wy > target[4]) target[4] = wy;
    if (wz > target[5]) target[5] = wz;
  }
}

// COORDINATE_TO_ORIGIN already recentres typical files; this catches models
// whose placements still carry georeferenced offsets, shifting them in f64
// before anything is cast to f32 so vertices never jitter.
function decideOrigin(chunks) {
  origin = [0, 0, 0];
  const probe = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
  for (const chunk of chunks) {
    const placements = chunk.placements;
    const count = placements.expressIDs.length;
    for (let p = 0; p < count; p++) {
      const box = registry.get(placements.geometryIDs[p])?.box;
      if (!box) continue;
      unionBoxCorners(probe, box, placements.matrices.subarray(p * 16, p * 16 + 16));
    }
  }
  const maxAbs = Math.max(...probe.map((v) => Math.abs(v)).filter((v) => isFinite(v)), 0);
  if (maxAbs > ORIGIN_THRESHOLD) {
    origin = [
      (probe[0] + probe[3]) / 2,
      (probe[1] + probe[4]) / 2,
      (probe[2] + probe[5]) / 2,
    ];
  }
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
}

function cellKeyFor(rec, transparent, spread) {
  if (!spread || !modelBox) return transparent ? "t" : "o";
  // Coarse 2x2x2 bucketing over the running model bounds keeps merged
  // chunks spatially tight enough for per-chunk frustum culling to bite.
  const cx = rec.box[0] < (modelBox[0] + modelBox[3]) / 2 ? 0 : 1;
  const cy = rec.box[1] < (modelBox[1] + modelBox[4]) / 2 ? 0 : 1;
  const cz = rec.box[2] < (modelBox[2] + modelBox[5]) / 2 ? 0 : 1;
  return `${transparent ? "t" : "o"}${cx}${cy}${cz}`;
}

function bakeMerged(rec, geom, matrix, color, transparent, spread) {
  const key = cellKeyFor(rec, transparent, spread);
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
    const len = Math.hypot(tx, ty, tz) || 1;
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
  if (acc.vertexCount >= CHUNK_VERTEX_LIMIT) {
    finalizeAccumulator(acc);
    accumulators.delete(key);
  }
}

class InstEntry {
  constructor(gid, geom, alpha) {
    this.gid = gid;
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
    const capacity = old ? this.capacity * 2 : 16;
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
      Math.hypot(e[0], e[1], e[2]),
      Math.hypot(e[4], e[5], e[6]),
      Math.hypot(e[8], e[9], e[10]));
    if (scale > this.maxScale) this.maxScale = scale;
    this.sphere.center.set(
      (this.tMin[0] + this.tMax[0]) / 2,
      (this.tMin[1] + this.tMax[1]) / 2,
      (this.tMin[2] + this.tMax[2]) / 2);
    this.sphere.radius = Math.hypot(
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
    registry.set(g.ids[i], {
      positions: g.positions.subarray(po, po + vc * 3),
      normals: g.normals.subarray(po, po + vc * 3),
      indices: g.indices.subarray(io, io + ic),
      box: g.bounds.subarray(bo, bo + 6),
    });
    po += vc * 3;
    io += ic;
    bo += 6;
  }
}

function ingestChunk(chunk) {
  const p = chunk.placements;
  const P = p.expressIDs.length;
  let chunkVerts = 0;
  for (let i = 0; i < P; i++) {
    const geom = registry.get(p.geometryIDs[i]);
    if (geom) chunkVerts += geom.positions.length / 3;
  }
  const spread = chunkVerts >= SPATIAL_SPLIT_VERTS;

  for (let i = 0; i < P; i++) {
    const geom = registry.get(p.geometryIDs[i]);
    if (!geom) continue;
    const expressID = p.expressIDs[i];
    const matrix = p.matrices.subarray(i * 16, i * 16 + 16);
    const color = p.colors.subarray(i * 4, i * 4 + 4);
    const rec = elementRecord(expressID);
    unionBoxCorners(rec.box, geom.box, matrix);
    if (!modelBox) {
      modelBox = rec.box.slice();
    } else {
      for (let k = 0; k < 3; k++) {
        if (rec.box[k] < modelBox[k]) modelBox[k] = rec.box[k];
        if (rec.box[k + 3] > modelBox[k + 3]) modelBox[k + 3] = rec.box[k + 3];
      }
    }
    triangleCount += geom.indices.length / 3;

    const alpha = color[3];
    const transparent = alpha < 0.999;
    const useKey = transparent
      ? `${p.geometryIDs[i]}:${alpha.toFixed(3)}` : `${p.geometryIDs[i]}:o`;
    const use = geomUse.get(useKey);
    if (use === undefined) {
      bakeMerged(rec, geom, matrix, color, transparent, spread);
      geomUse.set(useKey, "merged");
    } else if (use === "merged") {
      // Second sighting: this geometry repeats, switch to instancing. The
      // first copy stays baked; instances carry the rest.
      const entry = new InstEntry(p.geometryIDs[i], geom, alpha);
      entry.add(rec, matrix, color);
      geomUse.set(useKey, entry);
    } else {
      use.add(rec, matrix, color);
    }
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
    ? `${elements.size} products · ${Math.round(triangleCount / 1000)}k tris · ${drawCount} draws`
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
let loadGen = 0;
let activeHandlers = null;

function routeParserMessage(msg) {
  if (!activeHandlers || msg.seq !== loadGen) return;
  const h = activeHandlers;
  if (msg.type === "chunk") h.onChunk(msg);
  else if (msg.type === "progress") h.onProgress(msg);
  else if (msg.type === "maps") h.onMaps(msg);
  else if (msg.type === "tree") h.onTree(msg);
  else if (msg.type === "done") h.onDone(msg);
  else if (msg.type === "error" && msg.init_failed) {
    if (worker) worker.terminate();
    worker = null;
    workerBusy = false;
    h.onWorkerLost();
  }
  else if (msg.type === "error") h.onError(new Error(msg.message));
}

function spawnWorker() {
  worker = new Worker("/viewer/static/worker.js", { type: "module" });
  worker.onmessage = (event) => routeParserMessage(event.data);
  worker.onerror = (event) => {
    // A worker that cannot boot (or crashed) fails the current load over to
    // the inline path; the next load will try a fresh worker again.
    console.warn("[ifc-console] parser worker failed", event.message || event);
    const h = activeHandlers;
    worker.terminate();
    worker = null;
    workerBusy = false;
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
  loadGen++;
  const seq = loadGen;
  if (workerBusy && worker) {
    // A parse is still running for a previous revision: wasm cannot be
    // interrupted, so drop the whole worker and start fresh.
    worker.terminate();
    worker = null;
    workerBusy = false;
  }
  return new Promise((resolve, reject) => {
    let finished = false;
    let fallbackStarted = false;
    let chunks = [];
    let maps = null;
    let tree = null;
    const handlers = {
      onChunk: (msg) => chunks.push(msg),
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
        resolve({ chunks, maps, tree });
      },
      onError: (err) => {
        finished = true;
        workerBusy = false;
        reject(err);
      },
      onWorkerLost: () => {
        if (finished || fallbackStarted || seq !== loadGen) return;
        fallbackStarted = true;
        chunks = [];
        maps = null;
        tree = null;
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
      if (worker) worker.terminate();
      worker = null;
      workerBusy = false;
      handlers.onWorkerLost();
    }
  });
}

async function fetchModelBytes(res) {
  const total = Number(res.headers.get("content-length")) || 0;
  if (!res.body || !res.body.getReader) {
    return new Uint8Array(await res.arrayBuffer());
  }
  const reader = res.body.getReader();
  const parts = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value);
    received += value.length;
    showProgress(
      `Downloading model: ${(received / 1_048_576).toFixed(1)} MB`,
      total ? received / total : null);
  }
  const buffer = new Uint8Array(received);
  let offset = 0;
  for (const part of parts) {
    buffer.set(part, offset);
    offset += part.length;
  }
  return buffer;
}

async function loadModel() {
  if (loading) {
    reloadQueued = true;
    return;
  }
  loading = true;
  try {
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
      showOverlay(
        "Viewer authorization expired",
        "Reopen the viewer from the ifc-console terminal with /viewer.",
        null,
        "error",
      );
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
    await buildScene(buffer);
    currentEtag = nextEtag;
    hideProgress();
    // the hub's change frames carry no name/schema; re-sync the top bar
    refreshStatus();
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
    }
  }
}

function nextFrame() {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

async function buildScene(buffer) {
  // Live edits trigger rebuilds; carry selection and highlights across so a
  // refresh does not silently drop what the user (or the LLM) marked.
  const keepSelection = [...selection].map((id) => guidOf.get(id)).filter(Boolean);
  const keepPropertyGuid = keepSelection.at(-1) || null;
  const keepHighlight = [...highlightSet].map((id) => guidOf.get(id)).filter(Boolean);
  const keepIsolate = isolateSet !== null;
  disposeModel();
  userMovedCamera = false;
  showProgress("Reading IFC model", null);

  const parsed = await parseBuffer(buffer);
  showProgress("Preparing complete model", null);
  // Yield once so the label above actually paints: everything below is one
  // long synchronous block on a big model.
  await nextFrame();
  for (const chunk of parsed.chunks) registerChunkGeometry(chunk);
  decideOrigin(parsed.chunks);
  let done = 0;
  for (const chunk of parsed.chunks) {
    ingestChunk(chunk);
    // Batching dominates the wait on a large model; give the browser a frame
    // every so often so the progress bar advances instead of freezing.
    if ((++done % 24) === 0) {
      showProgress("Preparing complete model", done / parsed.chunks.length);
      await nextFrame();
    }
  }
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
  // the geometry moved, so old measurements no longer point at anything
  clearMeasurements();
  updateClipping();

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
  if (keepPropertyGuid && expressOf.has(keepPropertyGuid)) {
    showProperties(keepPropertyGuid);
  }
  // Express ids are not stable across rebuilds, so any open result list is
  // stale; re-running it keeps the panel honest after an edit.
  refreshSearch();
  // A rebuild resets the server's selection; tell it what survived.
  sendSelection();
  if (!userMovedCamera) fitTo(null);
  invalidate();
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

function isElementShown(id) {
  const isolatedOut =
    (isolateSet !== null && !isolateSet.has(id))
    || (userIsolateSet !== null && !userIsolateSet.has(id));
  return !hiddenByTree.has(id) && !hiddenManual.has(id) && !isolatedOut;
}

let hiddenCount = 0;
function applyVisibility() {
  hiddenCount = 0;
  for (const [id, rec] of elements) {
    const shown = isElementShown(id);
    if (!shown) hiddenCount++;
    stateData[rec.index * 4] = shown ? 255 : 0;
  }
  commitStyles();
  updateVisibilityInfo();
}

// ---------------------------------------------------------------- selection
function setSelection(ids, additive) {
  const touched = new Set(selection);  // what leaves the selection restyles too
  if (!additive) selection.clear();
  for (const id of ids) {
    if (additive && selection.has(id)) selection.delete(id);
    else selection.add(id);
    touched.add(id);
  }
  applyAppearanceTo(touched);
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

// The hub keeps the first 500; sending more just inflates the frame.
const SELECTION_WIRE_MAX = 500;

function sendSelection() {
  const guids = [];
  for (const id of selection) {
    const guid = guidOf.get(id);
    if (guid) guids.push(guid);
    if (guids.length >= SELECTION_WIRE_MAX) break;
  }
  const row = currentModelRow();
  wsSend({ type: "selection", guids, model_id: row ? row.id : null });
}

// GPU pick: render the pixel under the cursor with the id-encoding override
// material and decode which element it belongs to.
function pickElementAt(clientX, clientY) {
  if (!elements.size) return null;
  ensureFullResolution();
  const rect = canvas.getBoundingClientRect();
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const x = Math.floor(((clientX - rect.left) / rect.width) * size.x);
  const y = Math.floor(((clientY - rect.top) / rect.height) * size.y);

  const prevBackground = scene.background;
  const prevOverride = scene.overrideMaterial;
  const gridWasVisible = grid.visible;
  const axesWasVisible = axes ? axes.visible : false;
  const measureWasVisible = measureGroup.visible;
  const prevTarget = renderer.getRenderTarget();
  const prevClearColor = renderer.getClearColor(new THREE.Color()).clone();
  const prevClearAlpha = renderer.getClearAlpha();
  try {
    scene.background = null;
    grid.visible = false;
    if (axes) axes.visible = false;
    // markers carry no element index, so leaving them in makes a click on one
    // decode as element 0
    measureGroup.visible = false;
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
    invalidate();
  }

  const encoded = pickBuffer[0] * 65536 + pickBuffer[1] * 256 + pickBuffer[2];
  if (!encoded) return null;
  const expressID = elementsByIndex[encoded - 1];
  return expressID === undefined ? null : expressID;
}

// Click-to-select with a small movement threshold so orbiting never selects.
const DRAG_THRESHOLD = 6;
let downAt = null;
canvas.addEventListener("pointerdown", (e) => {
  canvas.focus({ preventScroll: true });
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

  if (measureMode) {
    handleMeasureClick(e.clientX, e.clientY);
    return;
  }

  const hit = pickElementAt(e.clientX, e.clientY);
  if (hit !== null) setSelection([hit], e.ctrlKey || e.metaKey);
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

function frameBox(box, direction) {
  const center = box.getCenter(new THREE.Vector3());
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const verticalFov = THREE.MathUtils.degToRad(camera.fov);
  const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * camera.aspect);
  const halfFov = Math.max(0.01, Math.min(verticalFov, horizontalFov) / 2);
  const distance = Math.max(sphere.radius, 1) / Math.sin(halfFov) * 1.1;
  const dir = direction
    || camera.position.clone().sub(controls.target).normalize();
  camera.position.copy(center.clone().add(dir.multiplyScalar(distance)));
  controls.target.copy(center);
  grid.position.set(center.x, groundY, center.z);
  camera.near = Math.max(distance / 1000, 0.01);
  camera.far = distance * 100;
  camera.updateProjectionMatrix();
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

// ---------------------------------------------------------------- sectioning
const AXIS_INDEX = { x: 0, y: 1, z: 2 };

function axisRange(axis) {
  const k = AXIS_INDEX[axis];
  if (!modelBox) return [0, 1];
  const pad = Math.max((modelBox[k + 3] - modelBox[k]) * 0.001, 1e-4);
  return [modelBox[k] - pad, modelBox[k + 3] + pad];
}

function updateClipping() {
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
  invalidate();
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
let pendingPoint = null;
const raycaster = new THREE.Raycaster();
const _ndc = new THREE.Vector2();

function markerRadius() {
  if (!modelBox) return 0.05;
  const span = Math.max(
    modelBox[3] - modelBox[0],
    modelBox[4] - modelBox[1],
    modelBox[5] - modelBox[2]);
  return Math.max(span * 0.004, 0.01);
}

function addMarker(point) {
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(markerRadius(), 12, 8),
    new THREE.MeshBasicMaterial({ color: 0xffb454, depthTest: false }));
  dot.position.copy(point);
  dot.renderOrder = 999;
  measureGroup.add(dot);
  return dot;
}

function surfacePointAt(clientX, clientY) {
  if (!elements.size) return null;
  ensureFullResolution();
  const rect = canvas.getBoundingClientRect();
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const x = Math.floor(((clientX - rect.left) / rect.width) * size.x);
  const y = Math.floor(((clientY - rect.top) / rect.height) * size.y);

  const prevBackground = scene.background;
  const prevOverride = scene.overrideMaterial;
  const gridWasVisible = grid.visible;
  const axesWasVisible = axes ? axes.visible : false;
  const measureWasVisible = measureGroup.visible;
  const prevTarget = renderer.getRenderTarget();
  const prevClearColor = renderer.getClearColor(new THREE.Color()).clone();
  const prevClearAlpha = renderer.getClearAlpha();
  try {
    scene.background = null;
    grid.visible = false;
    if (axes) axes.visible = false;
    measureGroup.visible = false;
    depthMaterial.uniforms.uStateTex.value = stateTex;
    depthMaterial.uniforms.uStateSize.value.set(STATE_W, stateH);
    depthMaterial.uniforms.uFar.value = camera.far;
    depthMaterial.clippingPlanes = activeClipPlanes;
    scene.overrideMaterial = depthMaterial;
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
    invalidate();
  }

  if (!pickBuffer[3]) return null;
  const normalized =
    pickBuffer[0] / 255 + pickBuffer[1] / 65025 + pickBuffer[2] / 16581375;
  const distance = normalized * camera.far;
  if (!(distance > 0)) return null;

  _ndc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
  _ndc.y = -((clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(_ndc, camera);
  return raycaster.ray.origin
    .clone()
    .add(raycaster.ray.direction.clone().multiplyScalar(distance));
}

function formatLength(metres) {
  if (Math.abs(metres) >= 1) return `${metres.toFixed(3)} m`;
  return `${(metres * 1000).toFixed(0)} mm`;
}

function commitMeasurement(from, to) {
  const geometry = new THREE.BufferGeometry().setFromPoints([from, to]);
  const line = new THREE.Line(
    geometry,
    new THREE.LineBasicMaterial({ color: 0xffb454, depthTest: false }));
  line.renderOrder = 998;
  measureGroup.add(line);
  measurements.push({
    from, to, line,
    distance: from.distanceTo(to),
    delta: [
      Math.abs(to.x - from.x), Math.abs(to.y - from.y), Math.abs(to.z - from.z),
    ],
  });
  renderMeasurements();
}

function clearMeasurements() {
  for (const child of [...measureGroup.children]) {
    measureGroup.remove(child);
    if (child.geometry) child.geometry.dispose();
    if (child.material) child.material.dispose();
  }
  measurements.length = 0;
  pendingPoint = null;
  renderMeasurements();
  invalidate();
}

function renderMeasurements() {
  const card = $("measure-card");
  const list = $("measure-list");
  if (!card || !list) return;
  list.textContent = "";
  for (let i = measurements.length - 1; i >= 0; i--) {
    const m = measurements[i];
    const row = el("li", "measure-row");
    row.appendChild(el("span", "measure-dist", formatLength(m.distance)));
    // three.js is Y-up while IFC is Z-up, so report the model's own axes
    row.appendChild(el("span", "measure-delta",
      `X ${formatLength(m.delta[0])} · Y ${formatLength(m.delta[2])} · Z ${formatLength(m.delta[1])}`));
    list.appendChild(row);
  }
  card.hidden = !measurements.length && !measureMode;
  const hint = $("measure-hint");
  if (hint) {
    hint.hidden = !measureMode;
    hint.textContent = pendingPoint
      ? "click the second point"
      : "click a surface to start a measurement";
  }
  invalidate();
}

function setMeasureMode(on) {
  measureMode = on;
  pendingPoint = null;
  canvas.classList.toggle("is-measuring", on);
  const button = $("tool-measure");
  if (button) {
    button.setAttribute("aria-pressed", String(on));
    button.classList.toggle("is-active", on);
  }
  renderMeasurements();
}

function handleMeasureClick(clientX, clientY) {
  const point = surfacePointAt(clientX, clientY);
  if (!point) return;
  if (!pendingPoint) {
    pendingPoint = point;
    addMarker(point);
    renderMeasurements();
    return;
  }
  addMarker(point);
  commitMeasurement(pendingPoint, point);
  pendingPoint = null;
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
      showOverlay(
        "Viewer authorization expired",
        "Reopen the viewer from the ifc-console terminal with /viewer.",
        null,
        "error",
      );
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
      if (want && want !== currentEtag) scheduleReload();
      else sendSelection();
      break;
    }
    case "model_updated":
      if (frame.dirty !== undefined) $("dirty").hidden = !frame.dirty;
      if (frame.reason === "loaded") applyColorThemeFrame({ clear: true });
      // These describe the active model; a pinned one is read-only and only
      // changes through a status frame (attach, detach, active switch).
      if (viewModelId) break;
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
  const modelId = currentModelRow()?.id ?? null;
  try {
    if (frame.model_id !== modelId) {
      throw new Error("Screenshot request does not match the currently viewed model");
    }
    if (frame.view && frame.view !== "current") {
      setView(frame.view, fitTargetIds(frame.fit));
    } else if (frame.fit) {
      const ids = fitTargetIds(frame.fit);
      fitTo(ids); // fit "all" passes null and frames everything visible
    }
    // Render explicitly in this task so the buffer is fresh when read.
    ensureFullResolution();
    controls.update();
    renderNow();

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
      model_id: modelId,
      data_b64: dataUrl.slice(dataUrl.indexOf(",") + 1),
      width: w,
      height: h,
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
}

// The console can hold more than one model; the picker appears only then.
// The active model is always the first entry and the default view.
function renderModelPicker(rows) {
  modelRows = rows || [];
  const select = $("model-select");
  const many = modelRows.length > 1;
  select.hidden = !many;
  if (!many) {
    if (viewModelId !== null) {
      viewModelId = null;
      scheduleReload();
    }
    return;
  }
  const activeId = (modelRows.find((m) => m.active) || {}).id;
  if (viewModelId && viewModelId === activeId) {
    viewModelId = null;  // a pinned model that became active should follow edits again
  }
  if (viewModelId && !modelRows.some((m) => m.id === viewModelId)) {
    setSelection([], false);
    viewModelId = null;  // the pinned model was detached: follow the active one
    scheduleReload();
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
}

function currentModelRow() {
  const id = viewModelId || (modelRows.find((m) => m.active) || {}).id;
  return modelRows.find((m) => m.id === id) || null;
}

function setModelInfo(status) {
  const label = $("model-name");
  if (status) {
    if (status.models) renderModelPicker(status.models);
    const row = currentModelRow();
    // With the picker visible the select already names the model; showing it
    // twice in a one-line topbar just costs space.
    label.hidden = !$("model-select").hidden;
    label.textContent = (row && row.name) || status.model || "no model";
    label.title = label.textContent;
    const schema = $("schema");
    schema.textContent = (row && row.schema) || status.schema || "";
    schema.hidden = !schema.textContent;
    if (status.mode) setMode(status.mode);
    $("dirty").hidden = !status.dirty;
    if (status.highlight) applyHighlightFrame(status.highlight);
    if (status.color_theme) applyColorThemeFrame(status.color_theme);
    if (status.chat) setChatAvailable(status.chat.enabled);
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
$("model-select").addEventListener("change", (e) => {
  const picked = e.target.value;
  const active = (modelRows.find((m) => m.active) || {}).id;
  viewModelId = picked === active ? null : picked;
  currentEtag = null;  // a different model, not a newer revision of this one
  clearProperties();
  loadModel();
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
  $("tool-hide").disabled = none;
  $("tool-fit-sel").disabled = none;
}

function updateVisibilityInfo() {
  $("tool-hidden-info").textContent =
    hiddenCount ? `${hiddenCount} of ${elements.size} elements hidden` : "";
  $("tool-show-all").disabled = hiddenCount === 0;
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

// -- section controls
function syncSectionRow(axis) {
  const state = section[axis];
  const slider = document.querySelector(`.section-at[data-axis="${axis}"]`);
  const flip = document.querySelector(`.section-flip[data-axis="${axis}"]`);
  const box = document.querySelector(`.section-on[data-axis="${axis}"]`);
  if (box) box.checked = state.on;
  if (slider) {
    slider.hidden = !state.on;
    slider.value = String(state.t);
  }
  if (flip) {
    flip.hidden = !state.on;
    flip.classList.toggle("is-active", state.flip);
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
    saveSection();
  });
}
for (const slider of document.querySelectorAll(".section-at")) {
  slider.addEventListener("input", () => {
    section[slider.dataset.axis].t = Number(slider.value);
    updateClipping();
  });
  slider.addEventListener("change", saveSection);
}
for (const flip of document.querySelectorAll(".section-flip")) {
  flip.addEventListener("click", () => {
    const axis = flip.dataset.axis;
    section[axis].flip = !section[axis].flip;
    syncSectionRow(axis);
    updateClipping();
    saveSection();
  });
}
$("tool-section-clear").addEventListener("click", () => {
  for (const axis of AXES) {
    section[axis].on = false;
    syncSectionRow(axis);
  }
  updateClipping();
  saveSection();
});

// -- measurement controls
$("tool-measure").addEventListener("click", () => setMeasureMode(!measureMode));
$("measure-clear").addEventListener("click", () => {
  clearMeasurements();
  setMeasureMode(false);
});

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
  };
}

function restoreView(view) {
  camera.position.fromArray(view.pos);
  controls.target.fromArray(view.target);
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
for (const axis of AXES) syncSectionRow(axis);

function syncPanelScrim() {
  const compact = window.innerWidth <= 620;
  const sidePanelOpen = !$("tree-panel").classList.contains("collapsed")
    || !$("props-panel").classList.contains("collapsed");
  $("panel-scrim").hidden = !compact || !sidePanelOpen;
}

function closeOtherCompactPanel(openKey) {
  if (window.innerWidth > 620 || uiState[openKey] !== true) return;
  if (typeof chatDock !== "undefined" && !chatDock.hidden) setChat(false);
  if (openKey === "treeOpen" && uiState.propsOpen === true) {
    uiState.propsOpen = false;
    applyPropsPanel();
  } else if (openKey === "propsOpen" && uiState.treeOpen === true) {
    uiState.treeOpen = false;
    applyTreePanel();
  }
}

function initSidePanel(panelId, splitId, btnId, widthKey, openKey, side, openByDefault) {
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
    panel.classList.toggle("collapsed", !open);
    splitter.classList.toggle("collapsed", !open);
    btn.setAttribute("aria-pressed", String(open));
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
  btn.addEventListener("click", () => {
    uiState[openKey] = !isOpen();
    closeOtherCompactPanel(openKey);
    saveUi();
    apply();
  });
  window.addEventListener("resize", apply);
  apply();
  return apply;
}

const applyTreePanel =
  initSidePanel(
    "tree-panel",
    "split-tree",
    "btn-panel-tree",
    "treeWidth",
    "treeOpen",
    "left",
    window.innerWidth > 620,
  );
const applyPropsPanel =
  initSidePanel("props-panel", "split-props", "btn-panel-props", "propsWidth", "propsOpen", "right", false);

$("panel-scrim").addEventListener("click", () => {
  uiState.treeOpen = false;
  uiState.propsOpen = false;
  saveUi();
  applyTreePanel();
  applyPropsPanel();
  $("btn-panel-tree").focus();
});

const POPOVERS = [
  ["btn-settings", "settings-panel"],
  ["btn-help", "help-panel"],
  ["btn-tools", "tools-panel"],
];
function closePopovers(exceptId, restoreFocus = false) {
  for (const [btnId, panelId] of POPOVERS) {
    if (panelId === exceptId) continue;
    const wasOpen = !$(panelId).hidden;
    $(panelId).hidden = true;
    $(btnId).setAttribute("aria-expanded", "false");
    if (restoreFocus && wasOpen) $(btnId).focus();
  }
}

function togglePopover(btnId, panelId) {
  const button = $(btnId);
  const panel = $(panelId);
  const open = panel.hidden;
  if (open) closePopovers(panelId);
  panel.hidden = !open;
  button.setAttribute("aria-expanded", String(open));
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

// ---------------------------------------------------------------- chat dock
// The panel is a separate module and only loaded when asked for, so a viewer
// session that never opens the chat pays nothing for it.
const chatDock = $("chat-dock");
const chatResize = $("chat-dock-resize");
const chatBtn = $("btn-chat");
const CHAT_DOCK_MIN_WIDTH = 300;
const CHAT_DOCK_DEFAULT_WIDTH = 400;
let chatPanel = null;
let chatLoadPromise = null;
let chatDesiredOpen = false;
let chatRequestVersion = 0;

function chatDockMaxWidth() {
  return Math.max(CHAT_DOCK_MIN_WIDTH, Math.round(window.innerWidth * 0.6));
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
  if (window.innerWidth <= 900) {
    chatDock.style.width = "";
    syncChatResizeAria();
  } else if (uiState.chatWidth) {
    setChatWidth(uiState.chatWidth);
  } else {
    syncChatResizeAria();
  }
}

function applyChatChrome(open) {
  chatDock.hidden = !open;
  chatResize.hidden = !open;
  chatBtn.setAttribute("aria-pressed", String(open));
  applyChatWidthForViewport();
}

function closePanelsForChat() {
  let changed = false;
  if (window.innerWidth < 900 && $("btn-panel-tree").getAttribute("aria-pressed") === "true") {
    uiState.treeOpen = false;
    changed = true;
  }
  if (window.innerWidth < 1500 && $("btn-panel-props").getAttribute("aria-pressed") === "true") {
    uiState.propsOpen = false;
    changed = true;
  }
  if (changed) {
    applyTreePanel();
    applyPropsPanel();
  }
}

async function setChat(open) {
  const requestVersion = ++chatRequestVersion;
  chatDesiredOpen = Boolean(open);
  uiState.chatOpen = chatDesiredOpen;
  applyChatChrome(chatDesiredOpen);
  // three panels plus the 3D view do not fit a normal window; the properties
  // panel is the one the chat replaces, so fold it away rather than letterbox
  // the model.
  if (chatDesiredOpen) closePanelsForChat();
  saveUi();
  resize();
  if (!chatDesiredOpen) return;

  try {
    if (!chatPanel) {
      chatLoadPromise ||= import("/viewer/static/chat.js")
        .then(({ mountChat }) => {
          chatPanel ||= mountChat(chatDock, {
            onClose: () => {
              setChat(false);
              chatBtn.focus({ preventScroll: true });
            },
          });
          return chatPanel;
        })
        .finally(() => { chatLoadPromise = null; });
      await chatLoadPromise;
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
  applyChatWidthForViewport();
  if (window.innerWidth > 620) {
    syncPanelScrim();
    return;
  }
  const treeOpen = $("btn-panel-tree").getAttribute("aria-pressed") === "true";
  const propsOpen = $("btn-panel-props").getAttribute("aria-pressed") === "true";
  let changed = false;
  if (chatDesiredOpen) {
    if (treeOpen) {
      uiState.treeOpen = false;
      changed = true;
    }
    if (propsOpen) {
      uiState.propsOpen = false;
      changed = true;
    }
  } else if (treeOpen && propsOpen) {
    uiState.propsOpen = false;
    changed = true;
  }
  if (changed) {
    saveUi();
    applyTreePanel();
    applyPropsPanel();
  }
  syncPanelScrim();
}

// A viewer session with no chat should not offer a button that opens a dead
// panel, so the console's status decides whether it is there at all.
function setChatAvailable(on) {
  chatBtn.hidden = !on;
  if (!on && chatDesiredOpen) setChat(false);
}

chatBtn.addEventListener("click", () => setChat(chatDock.hidden));

chatResize.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  chatResize.setPointerCapture(e.pointerId);
  const startX = e.clientX;
  const startWidth = chatDock.getBoundingClientRect().width;
  const move = (ev) => {
    setChatWidth(startWidth - (ev.clientX - startX));
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
  const movement = event.key === "ArrowLeft" ? 16 : -16;
  setChatWidth(currentChatWidth() + movement);
  saveUi();
  resize();
});

if (uiState.chatOpen || queryParams.get("chat") === "1") setChat(true);
window.addEventListener("resize", reconcileCompactLayout);
reconcileCompactLayout();

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    // an active tool owns Escape first, then the popovers
    if (measureMode) setMeasureMode(false);
    else {
      const trigger = POPOVERS.find(([, panelId]) => !$(panelId).hidden)?.[0];
      closePopovers();
      if (trigger) $(trigger).focus();
    }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const shortcutSurface = e.target === document.body
    || e.target === canvas
    || e.target.closest?.(".tree-label, .search-hit");
  if (!shortcutSurface) return;
  const key = e.key.toLowerCase();
  if (key === "m") {
    e.preventDefault();
    setMeasureMode(!measureMode);
  } else if (key === "f") {
    e.preventDefault();
    fitTo(null);
  } else if (key === "c") {
    e.preventDefault();
    if (!chatBtn.hidden) setChat(chatDock.hidden);
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

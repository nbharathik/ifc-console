/* Scene objects and shaders built once: ground grid, element-state and probe materials, section helpers. */

import * as THREE from "./vendor/three.module.min.js";

// The ground grid is one large plane whose shader draws 1 m / 10 m lines in
// world space with a camera-distance fade, so it reads as infinite for any
// model size. The lines live in world coordinates: repositioning the plane
// (to follow the camera target) never makes them swim.
export function makeInfiniteGrid() {
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

/**
 * Rewrite a Lambert shader so it reads the per-element state textures.
 *
 * The caller has already added the uniforms; this forwards the element index,
 * discards hidden elements, dithers ghosted ones, substitutes the override
 * color and adds the glow term.
 */
export function injectElementState(shader, depthBias) {
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
}

// GPU id picking: a 1x1 render with this override encodes elementIndex + 1
// into 24 bits of color; 0 is background. Hidden elements discard, so a pick
// can never land on something the user cannot see.
export function createPickMaterial(stateW, stateH) {
  return new THREE.ShaderMaterial({
    side: THREE.DoubleSide,
    clipping: true,
    uniforms: {
      uStateTex: { value: null },
      uStateSize: { value: new THREE.Vector2(stateW, stateH) },
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
}

// Same 1x1 trick for measuring: encode view-axis depth into 24 bits. The range
// is tightened to the model bounds for every probe instead of spanning the
// camera's deliberately huge far plane. That keeps millimetre-scale picks
// stable even in a kilometre-scale site. Merged chunks free their CPU arrays
// after upload, so the GPU remains the source of the exact surface point.
export function createDepthMaterial(stateW, stateH) {
  return new THREE.ShaderMaterial({
    side: THREE.DoubleSide,
    clipping: true,
    uniforms: {
      uStateTex: { value: null },
      uStateSize: { value: new THREE.Vector2(stateW, stateH) },
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
}

/** One translucent plane and outline per axis, for the cut slider to place. */
export function createSectionHelpers(axes, colors) {
  const root = new THREE.Group();
  root.name = "section-plane-helper";
  root.visible = false;
  const helpers = {};
  for (const axis of axes) {
    const geometry = new THREE.PlaneGeometry(1, 1);
    const fill = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      color: colors[axis],
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.16,
      depthTest: false,
      depthWrite: false,
    }));
    const border = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry),
      new THREE.LineBasicMaterial({
        color: colors[axis], transparent: true, opacity: 0.9,
        depthTest: false, depthWrite: false,
      }),
    );
    fill.renderOrder = 990;
    border.renderOrder = 991;
    const group = new THREE.Group();
    group.add(fill, border);
    group.visible = false;
    root.add(group);
    helpers[axis] = group;
  }
  return { root, helpers };
}

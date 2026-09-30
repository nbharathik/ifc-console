/* What a measurement looks like: scene markers and tags, ledger row text, and the next-step hint. */

import * as THREE from "./vendor/three.module.min.js";
import { axisFrame } from "./frames.js";

export function screenScaledDot(px, color) {
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(1, 12, 8),
    new THREE.MeshBasicMaterial({ color, depthTest: true, depthWrite: false }));
  dot.userData.px = px;
  dot.renderOrder = 999;
  return dot;
}

// Snap glyphs follow the CAD convention people already read: square for a
// corner, triangle for a midpoint, circle for a face centre, diamond for a
// point along an edge, and a plain dot for a bare surface hit.
export const GLYPH_PX = { corner: 13, midpoint: 13, centre: 12, edge: 12, axis: 13, surface: 7 };
const _glyphTextures = new Map();

export function snapGlyphTexture(kind) {
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
export function labelSprite(text) {
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

export function disposeVisual(object) {
  for (const child of [...object.children]) disposeVisual(child);
  if (object.geometry) object.geometry.dispose();
  if (object.material) {
    if (object.material.map) object.material.map.dispose();
    object.material.dispose();
  }
}

/** The two texts of a ledger row: the headline value, and what it was made of. */
export function describeMeasurement(m, { formatLength, formatArea, formatVolume, sceneDelta }) {
  if (m.kind === "dimensions") {
    const d = m.data;
    return {
      value: formatLength(d.thickness),
      detail: `${m.label || "element"} · ${formatLength(d.length)} × ${formatLength(d.width)}`
        + ` × ${formatLength(d.thickness)}`
        + (d.volume > 0 ? ` · ${formatVolume(d.volume)}` : ""),
    };
  }
  if (m.kind === "path") {
    return {
      value: formatLength(m.data.distance),
      detail: `${m.data.points.length} points · ${m.data.segments.length} segments`,
    };
  }
  if (m.kind === "angle") {
    return {
      value: `${m.data.degrees.toFixed(1)}°`,
      detail: `legs ${formatLength(m.data.legs[0])} · ${formatLength(m.data.legs[1])}`,
    };
  }
  if (m.kind === "area") {
    return {
      value: formatArea(m.data.area),
      detail: `${m.data.points.length} points · perimeter ${formatLength(m.data.perimeter)}`
        + (m.data.flatness > m.data.perimeter * 0.002
          ? ` · off-plane ${formatLength(m.data.flatness)}` : ""),
    };
  }
  if (m.kind === "laser") {
    const parts = ["x", "y", "z"].map((axis) => {
      const value = m.data.axes[axis];
      return `${axis.toUpperCase()} ${value.span == null ? "-" : formatLength(value.span)}`;
    });
    return { value: "clearance", detail: parts.join(" · ") };
  }
  // three.js is Y-up while IFC is Z-up, so report the model's own axes
  const snapped = (m.ends || []).filter((end) => end && end !== "surface");
  const slope = m.vertical > 1e-9 && m.horizontal > 1e-9
    ? ` · slope ${m.slopePercent.toFixed(1)}%` : "";
  return {
    value: formatLength(m.distance),
    detail: (m.axis ? `${m.axis.toUpperCase()} locked · ` : "")
      + (snapped.length ? `${snapped.join("/")} · ` : "")
      + `X ${formatLength(m.delta[sceneDelta[axisFrame.x.axis]])}`
      + ` · Y ${formatLength(m.delta[sceneDelta[axisFrame.y.axis]])}`
      + ` · Z ${formatLength(m.delta[sceneDelta[axisFrame.z.axis]])}`
      + slope,
  };
}

/** What to do next, in the words of whichever tool is running. */
export function measureHint(snap, { measureProblem, measureKind, pending, axisLock }) {
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

/* Helpers of the geometry batcher: growable buffers, edge extraction, snap features, per-element mass. */

import * as THREE from "./vendor/three.module.min.js";
import { geometryMass, norm3 } from "./measure_math.js";

// Measurement keeps only a compact feature index. A tessellation too large
// to inspect uses exact surface picking without feature snapping: a box edge
// is not necessarily an edge of the product and must never be presented as
// one. A product can retain at most 800 real segments. Placements reference the
// shared local array, keeping the wider snap coverage inexpensive.
const SNAP_TRIANGLE_LIMIT = 50_000;
const SNAP_SEGMENT_LIMIT = 800;
const EMPTY_SNAP_EDGES = new Float32Array(0);
const EDGE_ANGLE = 30;

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

export class Accumulator {
  constructor(transparent) {
    this.transparent = transparent;
    this.positions = new GrowArray(Float32Array);
    this.normals = new GrowArray(Int16Array);
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

/**
 * The crease and boundary edges of one unique shape, in its own coordinates.
 *
 * Deduplicated geometry is the point: a door type placed four hundred times
 * pays for this once. Cached on the registry entry and built lazily, so a
 * shape that ends up instanced never pays at all.
 */
export function edgeListFor(geom) {
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
export function recordSnapParts(rec, geom, matrix, origin) {
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
export function finalizeSnapParts(elements) {
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

export function registerChunkGeometry(chunk, registry) {
  const g = chunk.geometry;
  let po = 0;
  let io = 0;
  let bo = 0;
  for (let i = 0; i < g.ids.length; i++) {
    const vc = g.vertexCounts[i];
    const ic = g.indexCounts[i];
    const positions = g.positions.subarray(po, po + vc * 3);
    const indices = g.indices.subarray(io, io + ic);
    const area = g.areas?.[i];
    const volume = g.volumes?.[i];
    registry.set(g.ids[i], {
      positions,
      normals: g.normals.subarray(po, po + vc * 3),
      indices,
      box: g.bounds.subarray(bo, bo + 6),
      // Deduplicated, so this runs once per shape however many times it is
      // placed. Doing it later is not an option: the arrays are freed.
      // Normal worker parses arrive with this already calculated. The fallback
      // keeps inline/legacy parsed chunks valid without charging the UI thread
      // in the normal path.
      mass: Number.isFinite(area) && Number.isFinite(volume)
        ? { area, volume } : geometryMass(positions, indices),
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
export function accrueMass(rec, geom, m, origin) {
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
export function useKeyFor(geometryID, alpha) {
  return alpha < 0.999 ? `${geometryID}:${alpha.toFixed(3)}` : `${geometryID}:o`;
}

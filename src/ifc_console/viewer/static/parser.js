/* IFC parse pipeline, shared by worker.js (normal path) and app.js (inline
 * fallback when workers are unavailable).
 *
 * Streams web-ifc output as table-of-arrays chunks built for zero-copy
 * postMessage transfer. One `coordination` message carries the matrix web-ifc
 * applied, which is the only way back from the viewport's coordinates to the
 * file's own. Geometry is deduplicated: each unique geometry
 * (keyed by its expressID) crosses the wire exactly once as de-interleaved
 * positions/normals/indices plus a local AABB; placements reference it with
 * a float64 matrix and a color. The viewer bakes or instances on its side.
 */

import {
  geometryMass,
  packNormalComponent,
} from "./measure_math.js";

// Flush a chunk at this many placements or roughly this payload size.
const BATCH_PLACEMENTS = 2048;
const BATCH_BYTES = 8 * 1024 * 1024;
const PROGRESS_EVERY = 500;

class ChunkBuilder {
  constructor() {
    this.reset();
  }

  reset() {
    this.geomIds = [];
    this.vertexCounts = [];
    this.indexCounts = [];
    this.bounds = [];
    this.areas = [];
    this.volumes = [];
    this.posParts = [];
    this.normParts = [];
    this.idxParts = [];
    this.expressIDs = [];
    this.geometryIDs = [];
    this.matrices = [];
    this.colors = [];
    this.bytes = 0;
  }

  addGeometry(id, positions, normals, indices, box, mass) {
    this.geomIds.push(id);
    this.vertexCounts.push(positions.length / 3);
    this.indexCounts.push(indices.length);
    for (let i = 0; i < 6; i++) this.bounds.push(box[i]);
    this.areas.push(mass.area);
    this.volumes.push(mass.volume);
    this.posParts.push(positions);
    this.normParts.push(normals);
    this.idxParts.push(indices);
    this.bytes += positions.byteLength + normals.byteLength + indices.byteLength + 16;
  }

  addPlacement(expressID, geometryID, matrix, color) {
    this.expressIDs.push(expressID);
    this.geometryIDs.push(geometryID);
    for (let i = 0; i < 16; i++) this.matrices.push(matrix[i]);
    this.colors.push(color.x, color.y, color.z, color.w ?? 1);
    this.bytes += 160;
  }

  get full() {
    return this.expressIDs.length >= BATCH_PLACEMENTS || this.bytes >= BATCH_BYTES;
  }

  flush(emit) {
    if (!this.expressIDs.length) {
      this.reset();
      return;
    }
    let vTotal = 0;
    let iTotal = 0;
    for (const c of this.vertexCounts) vTotal += c;
    for (const c of this.indexCounts) iTotal += c;
    const positions = new Float32Array(vTotal * 3);
    const normals = new Int16Array(vTotal * 3);
    const indices = new Uint32Array(iTotal);
    let po = 0;
    let io = 0;
    for (let g = 0; g < this.posParts.length; g++) {
      positions.set(this.posParts[g], po);
      normals.set(this.normParts[g], po);
      po += this.posParts[g].length;
      indices.set(this.idxParts[g], io);
      io += this.idxParts[g].length;
    }
    const chunk = {
      type: "chunk",
      geometry: {
        ids: Uint32Array.from(this.geomIds),
        vertexCounts: Uint32Array.from(this.vertexCounts),
        indexCounts: Uint32Array.from(this.indexCounts),
        bounds: Float64Array.from(this.bounds),
        areas: Float64Array.from(this.areas),
        volumes: Float64Array.from(this.volumes),
        positions,
        normals,
        indices,
      },
      placements: {
        expressIDs: Float64Array.from(this.expressIDs),
        geometryIDs: Uint32Array.from(this.geometryIDs),
        matrices: Float64Array.from(this.matrices),
        colors: Float32Array.from(this.colors),
      },
    };
    const g = chunk.geometry;
    const p = chunk.placements;
    emit(chunk, [
      g.ids.buffer, g.vertexCounts.buffer, g.indexCounts.buffer, g.bounds.buffer,
      g.areas.buffer, g.volumes.buffer,
      g.positions.buffer, g.normals.buffer, g.indices.buffer,
      p.expressIDs.buffer, p.geometryIDs.buffer, p.matrices.buffer, p.colors.buffer,
    ]);
    this.reset();
  }
}

function collectTreeIds(node, acc) {
  if (node && typeof node.expressID === "number") acc.add(node.expressID);
  for (const child of (node && node.children) || []) collectTreeIds(child, acc);
  return acc;
}

function annotateTree(node, names) {
  node._name = names.get(node.expressID) ?? null;
  for (const child of node.children || []) annotateTree(child, names);
}

// Curve tessellation by file size: big files trade smoothness for triangles.
function circleSegments(byteLength) {
  const mb = byteLength / 1_048_576;
  if (mb > 80) return 6;
  if (mb > 30) return 8;
  return 12;
}

/* Parse `buffer` with the given IfcAPI instance and emit(message, transfers?):
 * progress* / chunk* -> coordination -> maps -> tree -> done. Throws on open
 * failure. */
export async function parseModel(api, buffer, emit) {
  const modelID = api.OpenModel(buffer, {
    COORDINATE_TO_ORIGIN: true,
    CIRCLE_SEGMENTS: circleSegments(buffer.byteLength),
    MEMORY_LIMIT: 3 * 1024 * 1024 * 1024,
  });
  try {
    const builder = new ChunkBuilder();
    const seen = new Set();       // geometry ids already shipped
    const withGeometry = new Set();
    let products = 0;
    let triangles = 0;

    api.StreamAllMeshes(modelID, (flatMesh) => {
      const id = flatMesh.expressID;
      const parts = flatMesh.geometries;
      let any = false;
      for (let p = 0; p < parts.size(); p++) {
        const placed = parts.get(p);
        const gid = placed.geometryExpressID;
        if (!seen.has(gid)) {
          const geometry = api.GetGeometry(modelID, gid);
          const raw = api.GetVertexArray(
            geometry.GetVertexData(), geometry.GetVertexDataSize());
          const rawIndex = api.GetIndexArray(
            geometry.GetIndexData(), geometry.GetIndexDataSize());
          geometry.delete();
          if (!rawIndex.length) continue;
          // De-interleave once per unique geometry; local AABB in the same pass.
          const count = raw.length / 6;
          const positions = new Float32Array(count * 3);
          const normals = new Int16Array(count * 3);
          const box = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
          for (let v = 0; v < count; v++) {
            const s = v * 6;
            const o = v * 3;
            const x = raw[s], y = raw[s + 1], z = raw[s + 2];
            positions[o] = x;
            positions[o + 1] = y;
            positions[o + 2] = z;
            normals[o] = packNormalComponent(raw[s + 3]);
            normals[o + 1] = packNormalComponent(raw[s + 4]);
            normals[o + 2] = packNormalComponent(raw[s + 5]);
            if (x < box[0]) box[0] = x;
            if (y < box[1]) box[1] = y;
            if (z < box[2]) box[2] = z;
            if (x > box[3]) box[3] = x;
            if (y > box[4]) box[4] = y;
            if (z > box[5]) box[5] = z;
          }
          seen.add(gid);
          const indices = new Uint32Array(rawIndex);
          builder.addGeometry(
            gid, positions, normals, indices, box, geometryMass(positions, indices));
          triangles += rawIndex.length / 3;
        }
        builder.addPlacement(id, gid, placed.flatTransformation, placed.color);
        any = true;
      }
      if (any) withGeometry.add(id);
      products++;
      if (products % PROGRESS_EVERY === 0) {
        emit({ type: "progress", stage: "geometry", products, triangles });
      }
      if (builder.full) builder.flush(emit);
    });
    builder.flush(emit);

    // The frame web-ifc put the model in. Without it every coordinate the
    // viewer reports is in the viewport's terms rather than the file's.
    let coordination = null;
    try {
      coordination = Array.from(api.GetCoordinationMatrix(modelID));
    } catch {
      coordination = null;
    }
    emit({ type: "coordination", matrix: coordination });

    // Spatial tree first: its node ids join the product ids for one combined
    // GlobalId + Name pass, a single GetLine per entity.
    let tree = null;
    try {
      tree = await api.properties.getSpatialStructure(modelID);
    } catch {
      tree = null;
    }
    const wanted = new Set(withGeometry);
    if (tree) collectTreeIds(tree, wanted);

    const names = new Map();
    const guidIds = [];
    const guids = [];
    let resolved = 0;
    for (const id of wanted) {
      try {
        const line = api.GetLine(modelID, id);
        const guid = line && line.GlobalId && line.GlobalId.value;
        if (guid) {
          guidIds.push(id);
          guids.push(guid);
        }
        const name = line && line.Name && line.Name.value;
        if (name) names.set(id, name);
      } catch { /* entities without attributes stay anonymous */ }
      resolved++;
      if (resolved % (PROGRESS_EVERY * 4) === 0) {
        emit({ type: "progress", stage: "attributes", resolved, total: wanted.size });
      }
    }
    if (tree) annotateTree(tree, names);

    const idArr = Float64Array.from(guidIds);
    emit({ type: "maps", guidIds: idArr, guids }, [idArr.buffer]);
    emit({ type: "tree", tree });
    emit({ type: "done", products, triangles });
  } finally {
    api.CloseModel(modelID);
  }
}

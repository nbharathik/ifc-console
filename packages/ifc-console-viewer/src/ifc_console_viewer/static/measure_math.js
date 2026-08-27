/* The arithmetic behind the viewer's measurements and its batcher.
 *
 * Split out of app.js because these are the functions whose answers leave the
 * viewer: they end up in reports, in measure_elements results and in
 * AI-authored IFC properties. Nothing here touches THREE, the DOM or module
 * state, so the numbers can be checked in plain Node.
 *
 * Points are read as {x, y, z} and written through .set(x, y, z), which a
 * THREE.Vector3 and a plain object both satisfy. Boxes are six numbers
 * [minx, miny, minz, maxx, maxy, maxz]; matrices are sixteen, column-major.
 * Everything is in the scene's own frame; converting to the model's axes is
 * the caller's job, because only the caller knows the frame.
 */

const ZERO3 = [0, 0, 0];
const AXES3 = ["x", "y", "z"];
const SCENE_INDEX = { x: 0, y: 1, z: 2 };

// V8 does not lower Math.hypot to a square root and it benchmarks an order of
// magnitude slower, which the per-vertex loops cannot afford.
export function norm3(x, y, z) {
  return Math.sqrt(x * x + y * y + z * z);
}

export function emptyBox(target, at) {
  target[at] = target[at + 1] = target[at + 2] = Infinity;
  target[at + 3] = target[at + 4] = target[at + 5] = -Infinity;
}

/** Grow `target` at `at` to hold every corner of `box` under `m`. */
export function unionBoxCorners(target, box, m, at = 0, origin = ZERO3) {
  for (let c = 0; c < 8; c++) {
    const x = box[c & 1 ? 3 : 0];
    const y = box[c & 2 ? 4 : 1];
    const z = box[c & 4 ? 5 : 2];
    const wx = m[0] * x + m[4] * y + m[8] * z + m[12] - origin[0];
    const wy = m[1] * x + m[5] * y + m[9] * z + m[13] - origin[1];
    const wz = m[2] * x + m[6] * y + m[10] * z + m[14] - origin[2];
    if (wx < target[at]) target[at] = wx;
    if (wy < target[at + 1]) target[at + 1] = wy;
    if (wz < target[at + 2]) target[at + 2] = wz;
    if (wx > target[at + 3]) target[at + 3] = wx;
    if (wy > target[at + 4]) target[at + 4] = wy;
    if (wz > target[at + 5]) target[at + 5] = wz;
  }
}

/**
 * Surface area and enclosed volume of one tessellation, in its own units.
 *
 * Area is the sum of triangle areas. Volume is the divergence theorem over
 * the same triangles, which is exact for a closed mesh and meaningless for an
 * open one, so callers get it alongside the geometry they took it from.
 */
export function geometryMass(positions, indices) {
  let area2 = 0;
  let volume6 = 0;
  for (let i = 0; i + 2 < indices.length; i += 3) {
    const a = indices[i] * 3;
    const b = indices[i + 1] * 3;
    const c = indices[i + 2] * 3;
    const ax = positions[a], ay = positions[a + 1], az = positions[a + 2];
    const ux = positions[b] - ax;
    const uy = positions[b + 1] - ay;
    const uz = positions[b + 2] - az;
    const vx = positions[c] - ax;
    const vy = positions[c + 1] - ay;
    const vz = positions[c + 2] - az;
    const nx = uy * vz - uz * vy;
    const ny = uz * vx - ux * vz;
    const nz = ux * vy - uy * vx;
    area2 += norm3(nx, ny, nz);
    volume6 += ax * nx + ay * ny + az * nz;
  }
  return { area: area2 / 2, volume: Math.abs(volume6) / 6 };
}

/**
 * The eight corners of an element's own box, in scene coordinates.
 *
 * The oriented box is the local box the parser shipped, still attached to the
 * placement that put it in the model. A wall at forty degrees has its corners
 * where its corners are; the world-axis box only ever agrees with it by luck.
 */
export function obbCorners(rec, out) {
  const obb = rec && rec.obb;
  if (obb) {
    const b = obb.box;
    const m = obb.m;
    for (let c = 0; c < 8; c++) {
      const x = b[c & 1 ? 3 : 0];
      const y = b[c & 2 ? 4 : 1];
      const z = b[c & 4 ? 5 : 2];
      out[c].set(
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14]);
    }
    return true;
  }
  const b = rec && rec.box;
  if (!b || !Number.isFinite(b[0])) return false;
  for (let c = 0; c < 8; c++) {
    out[c].set(b[c & 1 ? 3 : 0], b[c & 2 ? 4 : 1], b[c & 4 ? 5 : 2]);
  }
  return true;
}

/** Closest point on segment a-b to `ray`, clamped to the segment. */
export function closestOnSegmentToRay(a, b, ray, target) {
  const ux = b.x - a.x, uy = b.y - a.y, uz = b.z - a.z;
  const wx = a.x - ray.origin.x, wy = a.y - ray.origin.y, wz = a.z - ray.origin.z;
  const d = ray.direction;
  const uu = ux * ux + uy * uy + uz * uz;
  const ud = ux * d.x + uy * d.y + uz * d.z;
  const denom = uu - ud * ud;
  let t = denom > 1e-12
    ? (ud * (wx * d.x + wy * d.y + wz * d.z) - (ux * wx + uy * wy + uz * wz)) / denom
    : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  target.set(a.x + ux * t, a.y + uy * t, a.z + uz * t);
  return target;
}

/** Signed area vector of a closed polygon (Newell), which is 2A along n. */
export function polygonNormal(points) {
  let nx = 0, ny = 0, nz = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    nx += a.y * b.z - a.z * b.y;
    ny += a.z * b.x - a.x * b.z;
    nz += a.x * b.y - a.y * b.x;
  }
  return [nx * 0.5, ny * 0.5, nz * 0.5];
}

/**
 * Area, perimeter and flatness of a clicked outline, in the scene's frame.
 *
 * The area is only meaningful if the points lie in a plane, so how far they
 * miss one by travels with the answer instead of being assumed away.
 */
export function polygonMeasure(points) {
  const areaVec = polygonNormal(points);
  const area = norm3(areaVec[0], areaVec[1], areaVec[2]);
  const unit = area > 1e-12 ? 1 / area : 0;
  const normal = area > 1e-12
    ? [areaVec[0] * unit, areaVec[1] * unit, areaVec[2] * unit]
    : [0, 1, 0];
  const centre = [0, 0, 0];
  for (const point of points) {
    centre[0] += point.x;
    centre[1] += point.y;
    centre[2] += point.z;
  }
  const share = 1 / points.length;
  for (let k = 0; k < 3; k++) centre[k] *= share;
  let perimeter = 0;
  let flatness = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    perimeter += norm3(b.x - a.x, b.y - a.y, b.z - a.z);
    const off = Math.abs((a.x - centre[0]) * normal[0]
      + (a.y - centre[1]) * normal[1]
      + (a.z - centre[2]) * normal[2]);
    if (off > flatness) flatness = off;
  }
  return { area, perimeter, flatness, normal, centre };
}

/** Length of an open chain, including each segment for a readable breakdown. */
export function polylineMeasure(points) {
  const segments = [];
  let distance = 0;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const length = norm3(b.x - a.x, b.y - a.y, b.z - a.z);
    segments.push(length);
    distance += length;
  }
  return { distance, segments };
}

/** Distance, run, rise and slope for one span. Geometry stays in metres. */
export function spanMeasure(from, to, verticalAxis = "y") {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const dz = to.z - from.z;
  const verticalDelta = { x: dx, y: dy, z: dz }[verticalAxis] ?? dy;
  const distance = norm3(dx, dy, dz);
  const horizontal = Math.sqrt(Math.max(0, distance * distance - verticalDelta * verticalDelta));
  const vertical = Math.abs(verticalDelta);
  return {
    distance,
    horizontal,
    vertical,
    slopePercent: horizontal > 1e-12 ? (vertical / horizontal) * 100 : null,
    slopeAngle: (Math.atan2(vertical, horizontal) * 180) / Math.PI,
  };
}

/** The angle at `at`, between the directions to `from` and `to`. */
export function angleMeasure(from, at, to) {
  const ux = from.x - at.x, uy = from.y - at.y, uz = from.z - at.z;
  const vx = to.x - at.x, vy = to.y - at.y, vz = to.z - at.z;
  const lu = norm3(ux, uy, uz);
  const lv = norm3(vx, vy, vz);
  const cos = lu > 0 && lv > 0 ? (ux * vx + uy * vy + uz * vz) / (lu * lv) : 0;
  // acos of anything a hair outside [-1, 1] is NaN, and a right angle taken
  // off two normalised legs lands there often enough to matter.
  return {
    degrees: (Math.acos(Math.min(1, Math.max(-1, cos))) * 180) / Math.PI,
    legs: [lu, lv],
  };
}

/** An outline with repeated points collapsed, including the closing one. */
export function outlinePoints(points, minEdgeSq) {
  const out = [];
  for (const point of points) {
    const last = out[out.length - 1];
    if (last && distanceSq(last, point) < minEdgeSq) continue;
    out.push(point);
  }
  if (out.length > 1
    && distanceSq(out[0], out[out.length - 1]) < minEdgeSq) out.pop();
  return out;
}

function distanceSq(a, b) {
  const dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
  return dx * dx + dy * dy + dz * dz;
}

/**
 * The model axis a rubber band is close enough to be running along, or "".
 *
 * SketchUp's inference: a line within a few degrees of an axis meant that
 * axis, and saying so is better than letting the user aim by hand.
 */
export function inferAxis(anchor, raw, axisFrame, cosLimit) {
  const dx = raw.x - anchor.x;
  const dy = raw.y - anchor.y;
  const dz = raw.z - anchor.z;
  const length = norm3(dx, dy, dz);
  if (length < 1e-6) return "";
  for (const name of AXES3) {
    const along = { x: dx, y: dy, z: dz }[axisFrame[name].axis];
    if (Math.abs(along) / length >= cosLimit) return name;
  }
  return "";
}

/**
 * The size of one element, measured on the element's own axes.
 *
 * A wall at forty degrees has a thickness; the world-axis box around it does
 * not know that and reports the diagonal instead. Length, width and thickness
 * therefore come from the oriented box when there is one, and `size` keeps the
 * world-axis extents for anyone who wants the footprint on the grid.
 */
export function boxExtents(box, obb, axisFrame) {
  const [minX, minY, minZ, maxX, maxY, maxZ] = box;
  const span = { x: maxX - minX, y: maxY - minY, z: maxZ - minZ };
  const sizes = {
    x: span[axisFrame.x.axis], y: span[axisFrame.y.axis], z: span[axisFrame.z.axis],
  };
  let extents = [sizes.x, sizes.y, sizes.z];
  let centre = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2];
  let method = "world bounding box";
  if (obb) {
    const b = obb.box;
    const m = obb.m;
    extents = [
      (b[3] - b[0]) * Math.hypot(m[0], m[1], m[2]),
      (b[4] - b[1]) * Math.hypot(m[4], m[5], m[6]),
      (b[5] - b[2]) * Math.hypot(m[8], m[9], m[10]),
    ];
    const cx = (b[0] + b[3]) / 2;
    const cy = (b[1] + b[4]) / 2;
    const cz = (b[2] + b[5]) / 2;
    centre = [
      m[0] * cx + m[4] * cy + m[8] * cz + m[12],
      m[1] * cx + m[5] * cy + m[9] * cz + m[13],
      m[2] * cx + m[6] * cy + m[10] * cz + m[14],
    ];
    method = "oriented bounding box";
  }
  const ordered = [...extents].sort((a, b) => b - a);
  return {
    size: sizes,
    length: ordered[0],
    width: ordered[1],
    thickness: ordered[2],
    diagonal: Math.hypot(extents[0], extents[1], extents[2]),
    box_volume: extents[0] * extents[1] * extents[2],
    centre,
    method,
  };
}

/**
 * Cast both ways along each model axis from a point, against element boxes.
 *
 * This is the clearance question: how far to the next thing above me, beside
 * me, in front of me. `boxes` is an array of [id, box] the caller has already
 * filtered, because which elements count is a question about visibility
 * rather than about geometry; it is walked once per axis.
 */
export function clearanceAxes(boxes, from, axisFrame, reach, skin) {
  const out = {};
  for (const name of AXES3) {
    const index = SCENE_INDEX[axisFrame[name].axis];
    const a = (index + 1) % 3;
    const b = (index + 2) % 3;
    let negative = null;
    let positive = null;
    for (const [id, box] of boxes) {
      if (!Number.isFinite(box[0])) continue;
      // The ray only travels along one axis, so a box is in its path when the
      // origin sits inside the box on the other two.
      if (from[a] < box[a] - skin || from[a] > box[a + 3] + skin) continue;
      if (from[b] < box[b] - skin || from[b] > box[b + 3] + skin) continue;
      const near = box[index];
      const far = box[index + 3];
      if (far < from[index] - skin) {
        const distance = from[index] - far;
        if (distance <= reach && (!negative || distance < negative.distance)) {
          negative = { distance, express_id: id };
        }
      } else if (near > from[index] + skin) {
        const distance = near - from[index];
        if (distance <= reach && (!positive || distance < positive.distance)) {
          positive = { distance, express_id: id };
        }
      }
    }
    // "negative" means down the model's axis, which is not always down the
    // scene axis it happens to run along.
    const flipped = axisFrame[name].sign < 0;
    out[name] = {
      negative: flipped ? positive : negative,
      positive: flipped ? negative : positive,
      span: negative && positive ? negative.distance + positive.distance : null,
    };
  }
  return out;
}

/**
 * The units a reader may ask a length in, and how many decimals each carries.
 *
 * A fixed decimal count per unit, not a switch at one metre: a column where
 * "999 mm" sits above "1.001 m" cannot be scanned, and a 2.4 mm joint printed
 * as "2 mm" hides the tolerance it was drawn to. Feet come with the files that
 * are drawn in them.
 */
export const LENGTH_UNITS = {
  mm: { label: "mm", perMetre: 1000, decimals: 1 },
  cm: { label: "cm", perMetre: 100, decimals: 2 },
  m: { label: "m", perMetre: 1, decimals: 3 },
  ft: { label: "ft-in", perMetre: 1 / 0.3048, decimals: 3, imperial: true },
};

const METRES_PER_INCH = 0.0254;
const SQUARE_FEET_PER_SQUARE_METRE = 1 / (0.3048 * 0.3048);
// One inch fraction per decimal step, so the precision control means the same
// thing either side of the Atlantic: whole inches through sixteenths.
const INCH_FRACTIONS = [1, 2, 4, 8, 16];

export function unitOf(key) {
  return LENGTH_UNITS[key] || LENGTH_UNITS.m;
}

/**
 * The unit a file is authored in, from the unit assignment the server read.
 *
 * The name is the trustworthy half when it is there; the factor answers for
 * the conversion-based units whose names are whatever the exporter wrote.
 */
export function unitForFile(units) {
  const name = String((units && units.length_unit) || "").toUpperCase();
  if (name.includes("MILLI")) return "mm";
  if (name.includes("CENTI")) return "cm";
  if (name.includes("FOOT") || name.includes("FEET") || name.includes("INCH")) return "ft";
  if (name.includes("METRE") || name.includes("METER")) return "m";
  const factor = Number(units && units.to_si_factor);
  if (!(factor > 0)) return "m";
  if (Math.abs(factor - 0.001) < 1e-9) return "mm";
  if (Math.abs(factor - 0.01) < 1e-9) return "cm";
  if (Math.abs(factor - 0.3048) < 1e-9 || Math.abs(factor - METRES_PER_INCH) < 1e-9) return "ft";
  return "m";
}

/** Architectural feet and inches, rounded to `denominator`ths of an inch. */
export function formatFeetInches(metres, denominator) {
  const den = denominator > 0 ? denominator : 1;
  const sign = metres < 0 ? "-" : "";
  let ticks = Math.round((Math.abs(metres) / METRES_PER_INCH) * den);
  const feet = Math.floor(ticks / (12 * den));
  ticks -= feet * 12 * den;
  const inches = Math.floor(ticks / den);
  let numerator = ticks - inches * den;
  let shown = den;
  while (numerator && numerator % 2 === 0 && shown % 2 === 0) {
    numerator /= 2;
    shown /= 2;
  }
  return `${sign}${feet}'-${inches}${numerator ? ` ${numerator}/${shown}` : ""}"`;
}

export function formatLength(metres, unitKey = "m", decimals) {
  const unit = unitOf(unitKey);
  const places = Number.isInteger(decimals) ? decimals : unit.decimals;
  if (unit.imperial) {
    return formatFeetInches(
      metres, INCH_FRACTIONS[Math.min(Math.max(places, 0), INCH_FRACTIONS.length - 1)]);
  }
  return `${(metres * unit.perMetre).toFixed(Math.min(Math.max(places, 0), 6))} ${unit.label}`;
}

/**
 * An area in the square unit its family reports areas in.
 *
 * Nobody quotes a room in square millimetres, so every metric unit answers in
 * square metres and the imperial one in square feet, both at a fixed width.
 */
export function formatArea(squareMetres, unitKey = "m") {
  if (unitOf(unitKey).imperial) {
    return `${(squareMetres * SQUARE_FEET_PER_SQUARE_METRE).toFixed(2)} ft²`;
  }
  return `${squareMetres.toFixed(3)} m²`;
}

export function formatVolume(cubicMetres, unitKey = "m") {
  if (unitOf(unitKey).imperial) {
    const perCubicMetre = SQUARE_FEET_PER_SQUARE_METRE / 0.3048;
    return `${(cubicMetres * perCubicMetre).toFixed(2)} ft³`;
  }
  return `${cubicMetres.toFixed(3)} m³`;
}

/**
 * How big a batching cell is, and how full one may get before it is baked.
 *
 * Merged geometry is bucketed by where it is so frustum culling can drop what
 * is behind the camera, which only works if a cell is much smaller than the
 * model. Cells therefore target a chunk's worth of vertices. What stops that
 * from eating memory is the second number: chunks arrive in no spatial order,
 * so every cell stays open until the model is loaded, and the staging budget
 * divided by the cell count is how many vertices each may hold meanwhile.
 */
export function planSpatialGrid(box, verts, options) {
  const {
    cellVertexTarget, stagingBudget, minChunkVerts, chunkVertexLimit, splitVerts,
    minCells,
  } = options;
  if (!box || !(verts >= splitVerts)) return { size: 0, cells: 1, flushAt: chunkVertexLimit };
  const spans = [box[3] - box[0], box[4] - box[1], box[5] - box[2]];
  const widest = Math.max(spans[0], spans[1], spans[2], 1e-6);
  // A single-storey model has almost no height, and a cell of no height would
  // put every element in its own column. A flat span counts as a slice of the
  // widest one instead.
  let volume = 1;
  for (const span of spans) volume *= Math.max(span, widest * 0.02);
  const cells = Math.max(minCells, Math.min(
    Math.ceil(verts / cellVertexTarget), Math.floor(stagingBudget / minChunkVerts)));
  return {
    size: Math.max(Math.cbrt(volume / cells), 1e-6),
    cells,
    flushAt: Math.min(
      chunkVertexLimit, Math.max(minChunkVerts, Math.floor(stagingBudget / cells))),
  };
}

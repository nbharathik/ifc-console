/* The numbers the viewer's measurements are made of.
 *
 * Everything here was covered only by grepping app.js for string literals: a
 * flipped cross-product sign or an off-by-one in the OBB scale extraction kept
 * every literal intact and passed. These answers go into reports and into
 * AI-authored IFC properties, so they are checked as numbers.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  angleMeasure,
  boxExtents,
  clearanceAxes,
  emptyBox,
  formatArea,
  formatFeetInches,
  formatLength,
  formatVolume,
  geometryMass,
  LENGTH_UNITS,
  norm3,
  outlinePoints,
  planSpatialGrid,
  polylineMeasure,
  polygonMeasure,
  polygonNormal,
  spanMeasure,
  unionBoxCorners,
  unitForFile,
  unitOf,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/measure_math.js";

const near = (actual, expected, tolerance = 1e-9) => {
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `expected ${actual} to be within ${tolerance} of ${expected}`);
};

/** The viewer hands THREE.Vector3 in; the maths only needs x, y, z and set. */
const v3 = (x = 0, y = 0, z = 0) => ({
  x, y, z,
  set(nx, ny, nz) {
    this.x = nx;
    this.y = ny;
    this.z = nz;
    return this;
  },
});

// web-ifc draws Y-up, so the model's Z runs along the scene's Y.
const FRAME = {
  x: { axis: "x", sign: 1 },
  y: { axis: "z", sign: 1 },
  z: { axis: "y", sign: 1 },
};

const identity = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

/** Column-major rotation about the scene's Y axis, columns scaled by `scale`. */
function rotationY(degrees, scale = 1, translation = [0, 0, 0]) {
  const a = (degrees * Math.PI) / 180;
  const c = Math.cos(a) * scale;
  const s = Math.sin(a) * scale;
  return [
    c, 0, -s, 0,
    0, scale, 0, 0,
    s, 0, c, 0,
    translation[0], translation[1], translation[2], 1,
  ];
}

/** A closed unit cube at [0,1]^3, wound outwards. */
function unitCube(scale = 1) {
  const corner = [
    [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
    [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
  ];
  const positions = new Float32Array(24);
  for (let i = 0; i < 8; i++) {
    positions[i * 3] = corner[i][0] * scale;
    positions[i * 3 + 1] = corner[i][1] * scale;
    positions[i * 3 + 2] = corner[i][2] * scale;
  }
  const indices = new Uint32Array([
    0, 3, 2, 0, 2, 1,
    4, 5, 6, 4, 6, 7,
    0, 1, 5, 0, 5, 4,
    3, 7, 6, 3, 6, 2,
    0, 4, 7, 0, 7, 3,
    1, 2, 6, 1, 6, 5,
  ]);
  return { positions, indices };
}

test("a unit cube has area 6 and volume 1, and both follow its scale", () => {
  const cube = unitCube(1);
  const one = geometryMass(cube.positions, cube.indices);
  near(one.area, 6);
  near(one.volume, 1);
  // Millimetres instead of metres: area goes as the square, volume as the cube.
  const thousand = unitCube(1000);
  const big = geometryMass(thousand.positions, thousand.indices);
  near(big.area, 6e6, 1);
  near(big.volume, 1e9, 1e3);
});

test("an open surface still reports its area", () => {
  // Two triangles making a 3 by 4 rectangle, no enclosed volume.
  const positions = new Float32Array([0, 0, 0, 3, 0, 0, 3, 0, 4, 0, 0, 4]);
  const indices = new Uint32Array([0, 1, 2, 0, 2, 3]);
  const mass = geometryMass(positions, indices);
  near(mass.area, 12);
  near(mass.volume, 0);
});

test("a rectangle's area, perimeter and normal", () => {
  // 3 by 4 in the scene's XZ plane, which is the model's ground plane.
  const points = [v3(0, 0, 0), v3(3, 0, 0), v3(3, 0, 4), v3(0, 0, 4)];
  const measured = polygonMeasure(points);
  near(measured.area, 12);
  near(measured.perimeter, 14);
  near(measured.flatness, 0);
  near(measured.centre[0], 1.5);
  near(measured.centre[1], 0);
  near(measured.centre[2], 2);
  near(Math.abs(measured.normal[1]), 1);
  near(norm3(...measured.normal), 1);
});

test("reversing the winding flips the normal and keeps the area positive", () => {
  const points = [v3(0, 0, 0), v3(3, 0, 0), v3(3, 0, 4), v3(0, 0, 4)];
  const forward = polygonMeasure(points);
  const back = polygonMeasure([...points].reverse());
  near(back.area, forward.area);
  near(back.normal[1], -forward.normal[1]);
});

test("a polygon that is not quite flat says how far from flat it is", () => {
  // The last corner is lifted 0.5 out of the plane the other three sit in.
  const points = [v3(0, 0, 0), v3(2, 0, 0), v3(2, 0, 2), v3(0, 0.5, 2)];
  const measured = polygonMeasure(points);
  assert.ok(measured.flatness > 0.1, "a lifted corner is not flat");
  assert.ok(Number.isFinite(measured.area));
  // Every corner is still counted in the perimeter.
  near(measured.perimeter, 2 + 2 + Math.sqrt(4 + 0.25) + Math.sqrt(4 + 0.25), 1e-12);
});

test("collinear and degenerate outlines have no area and no NaN", () => {
  const collinear = polygonMeasure([v3(0, 0, 0), v3(1, 0, 0), v3(2, 0, 0)]);
  near(collinear.area, 0);
  // Newell gives no direction at all, so the fallback is up rather than NaN.
  assert.deepEqual(collinear.normal, [0, 1, 0]);
  near(collinear.perimeter, 4);
  const same = polygonMeasure([v3(1, 1, 1), v3(1, 1, 1), v3(1, 1, 1)]);
  near(same.area, 0);
  near(same.perimeter, 0);
  assert.ok(Number.isFinite(same.flatness));
});

test("Newell's area vector is twice the area along the normal", () => {
  const areaVec = polygonNormal([v3(0, 0, 0), v3(2, 0, 0), v3(2, 0, 2), v3(0, 0, 2)]);
  near(norm3(...areaVec), 4);
});

test("an open path totals its segments without closing the route", () => {
  const measured = polylineMeasure([
    v3(0, 0, 0), v3(3, 0, 0), v3(3, 4, 0), v3(3, 4, 12),
  ]);
  near(measured.distance, 19);
  assert.deepEqual(measured.segments, [3, 4, 12]);
});

test("a span reports horizontal run, rise and slope", () => {
  const measured = spanMeasure(v3(0, 0, 0), v3(3, 4, 0), "y");
  near(measured.distance, 5);
  near(measured.horizontal, 3);
  near(measured.vertical, 4);
  near(measured.slopePercent, 400 / 3);
  near(measured.slopeAngle, (Math.atan2(4, 3) * 180) / Math.PI);

  const vertical = spanMeasure(v3(0, 0, 0), v3(0, 8, 0), "y");
  assert.equal(vertical.slopePercent, null);
  near(vertical.slopeAngle, 90);
});

test("a 3-4-5 triangle's angles", () => {
  const right = v3(0, 0, 0);
  const along3 = v3(3, 0, 0);
  const along4 = v3(0, 0, 4);
  near(angleMeasure(along3, right, along4).degrees, 90, 1e-12);
  const atThree = angleMeasure(right, along3, along4);
  near(atThree.degrees, (Math.atan2(4, 3) * 180) / Math.PI, 1e-12);
  near(atThree.legs[0], 3);
  near(atThree.legs[1], 5);
  const atFour = angleMeasure(right, along4, along3);
  near(atFour.degrees + atThree.degrees + 90, 180, 1e-12);
});

test("straight and folded-back angles do not overflow acos", () => {
  const back = angleMeasure(v3(-1, 0, 0), v3(0, 0, 0), v3(1, 0, 0));
  near(back.degrees, 180, 1e-12);
  const together = angleMeasure(v3(1, 0, 0), v3(0, 0, 0), v3(2, 0, 0));
  near(together.degrees, 0, 1e-12);
  // A leg of no length has no direction; the answer is still a number.
  const collapsed = angleMeasure(v3(0, 0, 0), v3(0, 0, 0), v3(1, 0, 0));
  near(collapsed.degrees, 90);
  near(collapsed.legs[0], 0);
});

test("a world box is measured on the model's axes, longest first", () => {
  // Scene spans x 3, y 5, z 4; the model's Y is the scene's Z.
  const sized = boxExtents([0, 0, 0, 3, 5, 4], null, FRAME);
  assert.deepEqual(sized.size, { x: 3, y: 4, z: 5 });
  near(sized.length, 5);
  near(sized.width, 4);
  near(sized.thickness, 3);
  near(sized.diagonal, Math.sqrt(9 + 16 + 25));
  near(sized.box_volume, 60);
  assert.deepEqual(sized.centre, [1.5, 2.5, 2]);
  assert.equal(sized.method, "world bounding box");
});

test("a wall at forty degrees has a thickness, not a diagonal", () => {
  // 4 long, 3 tall, 0.2 thick, turned on the spot.
  const obb = { box: [0, 0, 0, 4, 3, 0.2], m: rotationY(40, 1, [10, 0, -5]) };
  // The world box around it is much wider than the wall is thick.
  const world = [10 - 3, 0, -5 - 3, 10 + 3, 3, -5 + 3];
  const sized = boxExtents(world, obb, FRAME);
  near(sized.length, 4, 1e-6);
  near(sized.width, 3, 1e-6);
  near(sized.thickness, 0.2, 1e-6);
  assert.equal(sized.method, "oriented bounding box");
  // The centre travels with the placement rather than with the world box.
  const a = (40 * Math.PI) / 180;
  near(sized.centre[0], 10 + Math.cos(a) * 2 + Math.sin(a) * 0.1, 1e-6);
  near(sized.centre[1], 1.5, 1e-6);
  near(sized.centre[2], -5 - Math.sin(a) * 2 + Math.cos(a) * 0.1, 1e-6);
  // The world box is still there for anyone who wants the footprint.
  near(sized.size.x, 6);
});

test("a scaled placement scales the extents it reports", () => {
  const obb = { box: [0, 0, 0, 4, 3, 0.2], m: rotationY(0, 2) };
  const sized = boxExtents([0, 0, 0, 8, 6, 0.4], obb, FRAME);
  near(sized.length, 8, 1e-6);
  near(sized.width, 6, 1e-6);
  near(sized.thickness, 0.4, 1e-6);
  near(sized.box_volume, 8 * 6 * 0.4, 1e-6);
});

test("clearance reads both ways along a model axis", () => {
  const from = [0, 0, 0];
  const boxes = [
    [11, [-1, -3, -1, 1, -1, 1]],   // below, 1 away
    [22, [-1, 2, -1, 1, 4, 1]],     // above, 2 away
    [33, [8, -1, 8, 9, 1, 9]],      // out of the ray's cross-section
  ];
  const axes = clearanceAxes(boxes, from, FRAME, 100, 1e-5);
  near(axes.z.negative.distance, 1);
  assert.equal(axes.z.negative.express_id, 11);
  near(axes.z.positive.distance, 2);
  assert.equal(axes.z.positive.express_id, 22);
  near(axes.z.span, 3);
  // Nothing sits either way along the model's X, so there is no span.
  assert.equal(axes.x.negative, null);
  assert.equal(axes.x.positive, null);
  assert.equal(axes.x.span, null);
});

test("a model axis that runs backwards reports its two ways round", () => {
  const flipped = { ...FRAME, z: { axis: "y", sign: -1 } };
  const boxes = [
    [11, [-1, -3, -1, 1, -1, 1]],
    [22, [-1, 2, -1, 1, 4, 1]],
  ];
  const axes = clearanceAxes(boxes, [0, 0, 0], flipped, 100, 1e-5);
  // Down the model's Z is up the scene's Y when the frame is flipped.
  assert.equal(axes.z.negative.express_id, 22);
  assert.equal(axes.z.positive.express_id, 11);
  near(axes.z.span, 3);
});

test("clearance stops at its reach and ignores an unmeasured box", () => {
  const boxes = [
    [11, [-1, -3, -1, 1, -1, 1]],
    [22, [-1, 2, -1, 1, 4, 1]],
    [33, [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity]],
  ];
  const axes = clearanceAxes(boxes, [0, 0, 0], FRAME, 1.5, 1e-5);
  near(axes.z.negative.distance, 1);
  assert.equal(axes.z.positive, null);
  assert.equal(axes.z.span, null);
});

test("an outline drops the points a double-click added twice", () => {
  const min = 1e-12;
  const corners = [v3(0, 0, 0), v3(4, 0, 0), v3(4, 0, 3), v3(0, 0, 3)];
  // pointerup commits a point for each half of the double-click that closes it.
  const clicked = [...corners, v3(0, 0, 3)];
  const outline = outlinePoints(clicked, min);
  assert.equal(outline.length, 4);
  const measured = polygonMeasure(outline);
  near(measured.area, 12);
  near(measured.perimeter, 14);
  // A closing click back on the first corner is the same corner, not a fifth.
  assert.equal(outlinePoints([...corners, v3(0, 0, 0)], min).length, 4);
  // And the surviving objects are the ones handed in, so their owners are
  // still reachable by identity.
  assert.equal(outline[0], corners[0]);
});

test("a column of lengths keeps one unit and one width", () => {
  // The old magnitude switch put "999 mm" directly above "1.001 m", which is
  // two units, two widths and no way to scan the column.
  assert.equal(formatLength(1), "1.000 m");
  assert.equal(formatLength(2.4567), "2.457 m");
  assert.equal(formatLength(-2.5), "-2.500 m");
  assert.equal(formatLength(0.9994), "0.999 m");
  assert.equal(formatLength(0.2), "0.200 m");
  assert.equal(formatLength(0), "0.000 m");
  // A millimetre-authored model reads in millimetres, tenths included: a
  // 2.4 mm joint printed as "2 mm" hid the tolerance it was drawn to.
  assert.equal(formatLength(0.0024, "mm"), "2.4 mm");
  assert.equal(formatLength(1, "mm"), "1000.0 mm");
  assert.equal(formatLength(1, "cm"), "100.00 cm");
  // and the precision control overrides the unit's own default
  assert.equal(formatLength(1.23456, "m", 1), "1.2 m");
  assert.equal(formatLength(1.23456, "m", 0), "1 m");
  assert.equal(formatLength(0.0024, "mm", 0), "2 mm");
  assert.equal(formatArea(0.1), "0.100 m²");
  assert.equal(formatArea(12), "12.000 m²");
  assert.equal(formatArea(0.05), "0.050 m²");
  // every metric unit still answers areas in square metres
  assert.equal(formatArea(12, "mm"), "12.000 m²");
  assert.equal(formatVolume(2.5), "2.500 m³");
});

test("feet and inches come out as a drawing would write them", () => {
  // 1 m is 39.37 inches: three feet and three and three eighths.
  assert.equal(formatLength(1, "ft"), `3'-3 3/8"`);
  assert.equal(formatLength(0.3048, "ft"), `1'-0"`);
  assert.equal(formatLength(0.0254, "ft"), `0'-1"`);
  assert.equal(formatLength(-0.3048, "ft"), `-1'-0"`);
  // the precision control picks the inch fraction, whole inches at zero
  assert.equal(formatLength(1, "ft", 0), `3'-3"`);
  assert.equal(formatLength(1, "ft", 1), `3'-3 1/2"`);
  assert.equal(formatLength(1, "ft", 4), `3'-3 3/8"`);
  // a fraction is reduced, never left as 8/16
  assert.equal(formatFeetInches(0.3048 + 0.0254 * 0.5, 16), `1'-0 1/2"`);
  assert.equal(formatFeetInches(0.3048 + 0.0254 * 0.25, 16), `1'-0 1/4"`);
  // imperial areas and volumes follow the length, not the metric default
  near(Number.parseFloat(formatArea(1, "ft")), 10.76, 0.01);
  near(Number.parseFloat(formatVolume(1, "ft")), 35.31, 0.01);
});

test("a formatted length parses back to what was measured", () => {
  for (const [unit, tolerance] of [["m", 5e-4], ["mm", 5e-5], ["cm", 5e-5]]) {
    for (const metres of [0.001, 0.25, 1, 3.1415, 120.5]) {
      const text = formatLength(metres, unit);
      const back = Number.parseFloat(text) / LENGTH_UNITS[unit].perMetre;
      near(back, metres, tolerance);
    }
  }
});

test("the file's own unit is the default, however the exporter spelt it", () => {
  assert.equal(unitForFile({ length_unit: "MILLIMETRE", to_si_factor: 0.001 }), "mm");
  assert.equal(unitForFile({ length_unit: "CENTIMETRE", to_si_factor: 0.01 }), "cm");
  assert.equal(unitForFile({ length_unit: "METRE", to_si_factor: 1 }), "m");
  assert.equal(unitForFile({ length_unit: "FOOT", to_si_factor: 0.3048 }), "ft");
  assert.equal(unitForFile({ length_unit: "inch", to_si_factor: 0.0254 }), "ft");
  // MILLIMETRE contains METRE, so the order of those two tests is the whole
  // difference between 200 mm and 0.2 m
  assert.notEqual(unitForFile({ length_unit: "MILLIMETRE" }), "m");
  // a conversion-based unit whose name is whatever the exporter wrote falls
  // back to the factor
  assert.equal(unitForFile({ length_unit: "ft", to_si_factor: 0.3048 }), "ft");
  assert.equal(unitForFile({ length_unit: null, to_si_factor: 0.001 }), "mm");
  // and nothing at all is metres, never a crash
  assert.equal(unitForFile(null), "m");
  assert.equal(unitForFile({}), "m");
  assert.equal(unitForFile({ to_si_factor: 0 }), "m");
  assert.equal(unitOf("nonsense"), LENGTH_UNITS.m);
});

test("a placed box unions into world bounds, shift included", () => {
  const target = new Float64Array(6);
  emptyBox(target, 0);
  assert.equal(target[0], Infinity);
  assert.equal(target[3], -Infinity);
  // A unit box turned 45 degrees about Y reaches sqrt(2) across.
  unionBoxCorners(target, [-0.5, 0, -0.5, 0.5, 1, 0.5], rotationY(45), 0);
  near(target[0], -Math.SQRT1_2, 1e-9);
  near(target[3], Math.SQRT1_2, 1e-9);
  near(target[1], 0);
  near(target[4], 1);
  // The georeferencing shift comes off before anything is cast to f32.
  const shifted = new Float64Array(6);
  emptyBox(shifted, 0);
  unionBoxCorners(shifted, [0, 0, 0, 1, 1, 1], identity(), 0, [100, 200, 300]);
  assert.deepEqual([...shifted], [-100, -200, -300, -99, -199, -299]);
});

test("a small model is batched whole and a large one is split", () => {
  const options = {
    cellVertexTarget: 120_000,
    stagingBudget: 6_000_000,
    minChunkVerts: 150_000,
    chunkVertexLimit: 500_000,
    splitVerts: 50_000,
    minCells: 8,
  };
  const box = [0, 0, 0, 40, 12, 30];
  const small = planSpatialGrid(box, 20_000, options);
  assert.equal(small.size, 0);
  assert.equal(small.cells, 1);
  assert.equal(small.flushAt, 500_000);
  assert.equal(planSpatialGrid(null, 5_000_000, options).size, 0);

  // A model worth splitting is never split more coarsely than the eight
  // octants the batcher used before there was a grid.
  const medium = planSpatialGrid(box, 500_000, options);
  assert.equal(medium.cells, 8);
  assert.ok(medium.size > 0 && medium.size < 40);
  const large = planSpatialGrid(box, 30_000_000, options);
  assert.ok(large.cells > medium.cells, "more geometry means more cells");
  assert.ok(large.size < medium.size, "more cells means smaller cells");
  // Whatever the model, the open cells cannot hold more than the budget.
  for (const verts of [60_000, 500_000, 5_000_000, 30_000_000, 300_000_000]) {
    const plan = planSpatialGrid(box, verts, options);
    assert.ok(plan.cells * plan.flushAt <= options.stagingBudget,
      `staging blew the budget at ${verts} vertices`);
    assert.ok(plan.flushAt >= options.minChunkVerts, "chunks stay worth drawing");
  }
});

test("a single-storey model is still divided across its floor", () => {
  const options = {
    cellVertexTarget: 120_000,
    stagingBudget: 6_000_000,
    minChunkVerts: 150_000,
    chunkVertexLimit: 500_000,
    splitVerts: 50_000,
    minCells: 8,
  };
  // 80 by 60 metres and 3 tall: a cell taken from the raw volume would be
  // taller than the building and would put every element in one column.
  const flat = planSpatialGrid([0, 0, 0, 80, 3, 60], 3_000_000, options);
  assert.ok(flat.size < 80 / 2, "the floor plate is divided, not kept whole");
  assert.ok(Number.isFinite(flat.size) && flat.size > 0);
  // A model with no height at all is still a model.
  const paper = planSpatialGrid([0, 0, 0, 80, 0, 60], 3_000_000, options);
  assert.ok(Number.isFinite(paper.size) && paper.size > 0);
});

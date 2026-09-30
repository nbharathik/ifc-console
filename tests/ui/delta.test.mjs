/* Whether a tab can follow an edit in place or has to rebuild. */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  indexTree,
  labelParts,
  planUpdate,
  touchesElement,
} from "../../src/ifc_console/viewer/static/delta.js";

const frame = (extra = {}) => ({
  type: "model_updated",
  etag: "m-fp-n-4",
  base_etag: "m-fp-n-3",
  reason: "edited",
  geometry: false,
  ...extra,
});

test("a property edit on the revision the tab shows is adopted in place", () => {
  const plan = planUpdate(frame({ elements: ["g1", "g2"] }), { etag: "m-fp-n-3" });

  assert.equal(plan.kind, "adopt");
  assert.deepEqual(plan.elements, ["g1", "g2"]);
  assert.deepEqual(plan.names, {});
});

test("a save leaves the etag alone and asks for nothing", () => {
  const plan = planUpdate(
    frame({ etag: "m-fp-n-3", base_etag: "m-fp-n-2", reason: "saved" }),
    { etag: "m-fp-n-3" },
  );

  assert.equal(plan.kind, "none");
});

test("a change that touched geometry rebuilds", () => {
  assert.equal(planUpdate(frame({ geometry: true }), { etag: "m-fp-n-3" }).kind, "reload");
  assert.equal(planUpdate(frame({ geometry: undefined }), { etag: "m-fp-n-3" }).kind, "reload");
});

test("a change to the tree structure rebuilds", () => {
  assert.equal(planUpdate(frame({ tree: true }), { etag: "m-fp-n-3" }).kind, "reload");
});

test("a tab that missed a step rebuilds instead of guessing", () => {
  assert.equal(planUpdate(frame(), { etag: "m-fp-n-2" }).kind, "reload");
  assert.equal(planUpdate(frame(), { etag: null }).kind, "reload");
  assert.equal(planUpdate(frame({ base_etag: null }), { etag: "m-fp-n-3" }).kind, "reload");
});

test("a load is never applied in place", () => {
  assert.equal(planUpdate(frame({ reason: "loaded" }), { etag: "m-fp-n-3" }).kind, "reload");
});

test("a frame without an etag rebuilds", () => {
  assert.equal(planUpdate(frame({ etag: null }), { etag: "m-fp-n-3" }).kind, "reload");
});

test("renamed elements carry their new labels", () => {
  const plan = planUpdate(
    frame({ labels: true, names: { g1: "Wall A" }, elements: ["g1"] }),
    { etag: "m-fp-n-3" },
  );

  assert.equal(plan.kind, "adopt");
  assert.deepEqual(plan.names, { g1: "Wall A" });
});

test("labels that arrive without their names rebuild", () => {
  assert.equal(
    planUpdate(frame({ labels: true }), { etag: "m-fp-n-3" }).kind,
    "reload",
  );
});

test("an update without an element list refreshes whatever is open", () => {
  const plan = planUpdate(frame(), { etag: "m-fp-n-3" });

  assert.equal(plan.elements, null);
  assert.equal(touchesElement(plan, "g9"), true);
  assert.equal(touchesElement(plan, null), false);
});

test("an update with an element list refreshes only those elements", () => {
  const plan = planUpdate(frame({ elements: ["g1"] }), { etag: "m-fp-n-3" });

  assert.equal(touchesElement(plan, "g1"), true);
  assert.equal(touchesElement(plan, "g2"), false);
});

test("the tree is indexed by express id, children included", () => {
  const tree = {
    expressID: 1,
    children: [{ expressID: 2, children: [{ expressID: 3 }] }, { expressID: 4 }],
  };

  const byId = indexTree(tree);

  assert.deepEqual([...byId.keys()].sort(), [1, 2, 3, 4]);
  assert.equal(byId.get(3).expressID, 3);
  assert.equal(indexTree(null).size, 0);
});

test("a label shows the name first and the class in brackets", () => {
  assert.deepEqual(labelParts("Wall A", "IfcWall"), {
    name: "Wall A ",
    cls: "(IfcWall)",
    title: "Wall A (IfcWall)",
  });
  assert.deepEqual(labelParts(null, "IfcWall"), { name: "", cls: "IfcWall", title: "IfcWall" });
});

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseModel } from "../../src/ifc_console/viewer/static/parser.js";

const identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

function cubeGeometry() {
  const corners = [
    [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
    [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
  ];
  const vertices = new Float32Array(corners.length * 6);
  for (let i = 0; i < corners.length; i++) {
    vertices.set([...corners[i], 0, 1, 0], i * 6);
  }
  const indices = new Uint32Array([
    0, 3, 2, 0, 2, 1,
    4, 5, 6, 4, 6, 7,
    0, 1, 5, 0, 5, 4,
    3, 7, 6, 3, 6, 2,
    0, 4, 7, 0, 7, 3,
    1, 2, 6, 1, 6, 5,
  ]);
  return { vertices, indices };
}

test("the parser worker payload packs normals and carries geometry mass", async () => {
  const cube = cubeGeometry();
  let settings = null;
  let closed = false;
  const api = {
    properties: {
      async getSpatialStructure() {
        return { expressID: 1, children: [{ expressID: 42, children: [] }] };
      },
    },
    OpenModel(_buffer, options) {
      settings = options;
      return 3;
    },
    StreamAllMeshes(modelID, callback) {
      assert.equal(modelID, 3);
      callback({
        expressID: 42,
        geometries: {
          size: () => 1,
          get: () => ({
            geometryExpressID: 7,
            flatTransformation: identity,
            color: { x: 0.2, y: 0.4, z: 0.6, w: 1 },
          }),
        },
      });
    },
    GetGeometry() {
      return {
        GetVertexData: () => cube.vertices,
        GetVertexDataSize: () => cube.vertices.length,
        GetIndexData: () => cube.indices,
        GetIndexDataSize: () => cube.indices.length,
        delete() {},
      };
    },
    GetVertexArray: (value) => value,
    GetIndexArray: (value) => value,
    GetCoordinationMatrix: () => identity,
    GetLine(_modelID, expressID) {
      return expressID === 42
        ? { GlobalId: { value: "cube-guid" }, Name: { value: "Cube" } }
        : {};
    },
    CloseModel(modelID) {
      assert.equal(modelID, 3);
      closed = true;
    },
  };

  const emitted = [];
  await parseModel(api, new Uint8Array(1024), (message, transfers = []) => {
    emitted.push({ message, transfers });
  });

  const packet = emitted.find(({ message }) => message.type === "chunk");
  assert.ok(packet);
  const geometry = packet.message.geometry;
  assert.ok(geometry.normals instanceof Int16Array);
  assert.equal(geometry.normals.byteLength, geometry.positions.byteLength / 2);
  assert.deepEqual([...geometry.normals.slice(0, 3)], [0, 32767, 0]);
  assert.equal(geometry.areas[0], 6);
  assert.equal(geometry.volumes[0], 1);
  assert.ok(packet.transfers.includes(geometry.normals.buffer));
  assert.ok(packet.transfers.includes(geometry.areas.buffer));
  assert.ok(packet.transfers.includes(geometry.volumes.buffer));
  assert.equal(settings.COORDINATE_TO_ORIGIN, true);
  assert.equal(emitted.at(-1).message.type, "done");
  assert.equal(closed, true);
});

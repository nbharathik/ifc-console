/* The two coordinate frames: the scene web-ifc draws in, and the model's own axes. */

import * as THREE from "./vendor/three.module.min.js";

// Two frames. The scene's, which is what three.js draws and what every pick
// and bounding box is in; and the model's, which is what the IFC file says
// and what every answer has to be in. web-ifc's coordination matrix is the
// step between them, and `origin` is the extra shift this viewer applies to
// keep a georeferenced file inside f32.
// web-ifc hands geometry back Y-up: the file's Z becomes the scene's Y and
// the file's Y becomes the scene's -Z. Its coordination matrix carries the
// origin shift on top of that and nothing else, so both are needed to get
// back to the file's own coordinates.
const IFC_TO_GL = new THREE.Matrix4().set(
  1, 0, 0, 0,
  0, 0, 1, 0,
  0, -1, 0, 0,
  0, 0, 0, 1,
);
export const modelToScene = new THREE.Matrix4();
export const sceneToModel = new THREE.Matrix4();
// Which scene axis each model axis runs along, and which way round.
export const axisFrame = {
  x: { axis: "x", sign: 1 }, y: { axis: "z", sign: 1 }, z: { axis: "y", sign: 1 },
};
export const MODEL_OF_SCENE = { x: "x", y: "z", z: "y" };

export function refreshFrames(coordinationMatrix, origin) {
  modelToScene.copy(coordinationMatrix).multiply(IFC_TO_GL);
  modelToScene.premultiply(
    new THREE.Matrix4().makeTranslation(-origin[0], -origin[1], -origin[2]));
  sceneToModel.copy(modelToScene).invert();
  const probe = new THREE.Vector3();
  for (const name of ["x", "y", "z"]) {
    probe.set(name === "x" ? 1 : 0, name === "y" ? 1 : 0, name === "z" ? 1 : 0);
    probe.transformDirection(modelToScene);
    const axis = Math.abs(probe.x) >= Math.abs(probe.y) && Math.abs(probe.x) >= Math.abs(probe.z)
      ? "x" : Math.abs(probe.y) >= Math.abs(probe.z) ? "y" : "z";
    axisFrame[name] = { axis, sign: probe[axis] < 0 ? -1 : 1 };
    MODEL_OF_SCENE[axis] = name;
  }
}

/** A scene point as the model's own [x, y, z]. */
export function toModelPoint(point) {
  const out = point.clone().applyMatrix4(sceneToModel);
  return [out.x, out.y, out.z];
}

/** A model [x, y, z] as a scene point. */
export function toScenePoint(triple) {
  return new THREE.Vector3(triple[0], triple[1], triple[2]).applyMatrix4(modelToScene);
}

/** Where `value` on one scene axis falls on the model axis that runs along it. */
export function toModelAxis(sceneAxis, value) {
  const probe = new THREE.Vector3();
  probe[sceneAxis] = value;
  const out = probe.applyMatrix4(sceneToModel);
  return out[MODEL_OF_SCENE[sceneAxis]];
}

/** And back: a model-axis position as a position on the scene axis. */
export function toSceneAxis(sceneAxis, modelValue) {
  const at0 = toModelAxis(sceneAxis, 0);
  const at1 = toModelAxis(sceneAxis, 1);
  return at1 === at0 ? 0 : (modelValue - at0) / (at1 - at0);
}

/** A scene direction as the model's own unit [x, y, z]; no origin shift. */
export function toModelDirection(vector) {
  const out = vector.clone().transformDirection(sceneToModel);
  return [out.x, out.y, out.z];
}

/** And back: a model direction as a scene direction. */
export function toSceneDirection(triple) {
  return new THREE.Vector3(triple[0], triple[1], triple[2])
    .transformDirection(modelToScene);
}

// Binary/ASCII STL → BufferGeometry (unshared vertices: computeVertexNormals gives the faceted
// per-face look that reads as CAD — same choice as the baked viewer page).
import * as THREE from "three";

export function parseSTL(buf: ArrayBuffer): THREE.BufferGeometry {
  const bytes = new Uint8Array(buf);
  let positions: Float32Array;
  if (bytes.byteLength >= 84) {
    const dv = new DataView(buf);
    const n = dv.getUint32(80, true);
    if (84 + n * 50 === bytes.byteLength) {
      positions = new Float32Array(n * 9);
      let o = 84;
      let k = 0;
      for (let i = 0; i < n; i++) {
        o += 12; // stored facet normal ignored; recomputed
        for (let v = 0; v < 9; v++) {
          positions[k++] = dv.getFloat32(o, true);
          o += 4;
        }
        o += 2;
      }
      return build(positions);
    }
  }
  const text = new TextDecoder().decode(bytes);
  const re = /vertex\s+([-+eE\d.]+)\s+([-+eE\d.]+)\s+([-+eE\d.]+)/g;
  const arr: number[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) arr.push(+m[1], +m[2], +m[3]);
  arr.length -= arr.length % 9;
  positions = new Float32Array(arr);
  return build(positions);
}

function build(positions: Float32Array): THREE.BufferGeometry {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geo.computeVertexNormals();
  geo.computeBoundingBox();
  return geo;
}

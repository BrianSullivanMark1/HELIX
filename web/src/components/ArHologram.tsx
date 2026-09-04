// The hologram layer of the camera panel: one of HELIX's 3D designs rendered over the live video
// (transparent WebGL, orthographic so screen pixels are the unit), placed by the user's gestures
// and carried by the tracker. Ghost-cyan, half-transparent, with crease edges — a hologram, not a
// render — so the real board stays readable underneath.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import * as THREE from "three";
import { getToken } from "../lib/api";
import { parseSTL } from "../lib/stl";
import { angleOf, apply, relative, scaleOf, type Similarity } from "../lib/track";

/** What the tracker knows this frame, in its own (small) pixel space. */
export interface ArFrame {
  T: Similarity; // BASE → now
  locked: boolean;
  tw: number;
  th: number;
}

/** Where the hologram sits: anchored in the frame whose BASE→frame transform is `T`. */
export interface Placement {
  x: number; // normalized, in the anchor frame
  y: number;
  size: number; // normalized frame-widths per millimetre
  roll: number; // radians, screen plane
  tiltX: number; // radians
  yaw: number; // radians
  T: Similarity;
}

export interface Box {
  w: number;
  h: number;
}

interface Loaded {
  geo: THREE.BufferGeometry;
  dims: THREE.Vector3;
}

function useMesh(url: string, onLoaded: (dims: THREE.Vector3) => void): Loaded | null {
  const [model, setModel] = useState<Loaded | null>(null);
  useEffect(() => {
    if (!url) return;
    let dead = false;
    void fetch(url, { headers: { "X-Helix-Token": getToken() } })
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        if (dead) return;
        const geo = parseSTL(buf);
        geo.center();
        geo.computeBoundingBox();
        const dims = new THREE.Vector3();
        geo.boundingBox!.getSize(dims);
        setModel((old) => {
          old?.geo.dispose();
          return { geo, dims };
        });
        onLoaded(dims);
      })
      .catch(() => undefined);
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);
  return model;
}

function Carried({
  model, arRef, placeRef, boxRef, mirror,
}: {
  model: Loaded;
  arRef: RefObject<ArFrame>;
  placeRef: RefObject<Placement>;
  boxRef: RefObject<Box>;
  mirror: boolean;
}) {
  const group = useRef<THREE.Group>(null);
  const edges = useMemo(() => new THREE.EdgesGeometry(model.geo, 28), [model.geo]);
  const invalidate = useThree((s) => s.invalidate);
  useFrame(() => {
    const g = group.current;
    const ar = arRef.current;
    const p = placeRef.current;
    const box = boxRef.current;
    if (!g || !ar || !p || !box) return;
    const M = relative(ar.T, p.T);
    const [px, py] = apply(M, p.x * ar.tw, p.y * ar.th);
    let sx = (px / ar.tw) * box.w;
    const sy = (py / ar.th) * box.h;
    if (mirror) sx = box.w - sx;
    const k = scaleOf(M);
    const pxPerMm = Math.max(0.05, p.size * box.w * k);
    const ang = angleOf(M);
    g.position.set(sx - box.w / 2, box.h / 2 - sy, 0);
    g.scale.set(mirror ? -pxPerMm : pxPerMm, pxPerMm, pxPerMm);
    // screen-plane roll last, then tilt, then yaw (ZXY); screen y points down, three's up.
    g.rotation.set(p.tiltX, p.yaw, mirror ? p.roll + ang : -(p.roll + ang), "ZXY");
    invalidate();
  });
  return (
    <group ref={group}>
      <mesh geometry={model.geo}>
        <meshPhysicalMaterial
          color="#3fe0e0"
          emissive="#0d3b3b"
          transparent
          opacity={0.42}
          roughness={0.35}
          metalness={0.1}
          depthWrite={false}
          side={THREE.DoubleSide}
        />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color="#9ff5f5" transparent opacity={0.9} />
      </lineSegments>
    </group>
  );
}

function KeepRendering() {
  const invalidate = useThree((s) => s.invalidate);
  useFrame(() => invalidate());
  return null;
}

export default function ArHologram({
  stl, arRef, placeRef, boxRef, box, mirror, onLoaded,
}: {
  stl: string;
  arRef: RefObject<ArFrame>;
  placeRef: RefObject<Placement>;
  boxRef: RefObject<Box>;
  box: Box;
  mirror: boolean;
  onLoaded: (dims: THREE.Vector3) => void;
}) {
  const model = useMesh(stl, onLoaded);
  if (box.w < 4 || box.h < 4) return null;
  return (
    <Canvas
      orthographic
      gl={{ alpha: true, antialias: true, premultipliedAlpha: false }}
      camera={{ position: [0, 0, 2000], near: -10000, far: 10000, zoom: 1 }}
      style={{ position: "absolute", inset: 0, pointerEvents: "none", background: "transparent" }}
      dpr={[1, 2]}
    >
      <KeepRendering />
      <hemisphereLight args={["#dff7f7", "#0a1a20", 1.2]} />
      <directionalLight position={[300, 400, 900]} intensity={0.8} />
      {model && (
        <Carried model={model} arRef={arRef} placeRef={placeRef} boxRef={boxRef} mirror={mirror} />
      )}
    </Canvas>
  );
}

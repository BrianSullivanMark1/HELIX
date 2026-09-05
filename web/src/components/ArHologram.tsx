// The hologram layer of the camera panel: one of HELIX's 3D designs rendered over the live video
// (transparent WebGL, orthographic so screen pixels are the unit), placed by the user's gestures
// and carried by the tracker. Ghost-cyan, half-transparent, with crease edges — a hologram, not a
// render — so the real board stays readable underneath.
//
// When the design carries a component layout (design_enclosure's assets/layout.json), each part is
// drawn as a labelled translucent rectangle on the cavity floor inside the shell — a ghost pocket —
// with its apertures marked, so the real parts can be laid in their pockets on the desk and compared.
// At true scale (the panel calibrated on a card) 1 mm on the plate is 1 mm on the desk.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import * as THREE from "three";
import { getToken } from "../lib/api";
import { parseSTL } from "../lib/stl";
import type { HologramLayout, LayoutAperture } from "../lib/store";
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

// ----- component ghosts -----

const GHOST = "#ffcf45"; // a part's footprint: amber, so it reads apart from the cyan shell
const MARK = "#ff5df2"; // an aperture (lens, mic, port…): magenta

/** A text label as a sprite (a canvas texture, no font files): always faces the camera, never
 * mirrored — three.js sizes sprites by the LENGTH of the model matrix's columns, so the panel's
 * mirrored preview (a negative x scale on the group) leaves the words readable. */
function Label({ text, position, height }: { text: string; position: [number, number, number]; height: number }) {
  const texture = useMemo(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 512;
    canvas.height = 96;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.font = "600 44px Inter, 'Segoe UI', sans-serif";
      const w = Math.min(500, ctx.measureText(text).width + 36);
      const x0 = (canvas.width - w) / 2;
      ctx.fillStyle = "rgba(5,8,11,0.82)";
      ctx.beginPath();
      ctx.roundRect(x0, 8, w, 80, 18);
      ctx.fill();
      ctx.strokeStyle = GHOST;
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.fillStyle = GHOST;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(text, canvas.width / 2, 50, 470);
    }
    const t = new THREE.CanvasTexture(canvas);
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }, [text]);
  useEffect(() => () => texture.dispose(), [texture]);
  // the canvas is 512×96: keep its aspect so the words are not squashed
  return (
    <sprite position={position} scale={[height * (512 / 96), height, 1]}>
      <spriteMaterial map={texture} transparent depthTest={false} depthWrite={false} />
    </sprite>
  );
}

function outline(w: number, h: number): THREE.BufferGeometry {
  const g = new THREE.BufferGeometry();
  g.setFromPoints([
    new THREE.Vector3(-w / 2, -h / 2, 0), new THREE.Vector3(w / 2, -h / 2, 0),
    new THREE.Vector3(w / 2, h / 2, 0), new THREE.Vector3(-w / 2, h / 2, 0),
  ]);
  return g;
}

/** A translucent rectangle with an outline — a pocket's footprint, an aperture's opening. */
function Pane({
  position, w, h, color, opacity, rotation,
}: {
  position: [number, number, number]; w: number; h: number; color: string; opacity: number;
  rotation?: [number, number, number];
}) {
  const edges = useMemo(() => outline(w, h), [w, h]);
  useEffect(() => () => edges.dispose(), [edges]);
  return (
    <group position={position} rotation={rotation}>
      <mesh>
        <planeGeometry args={[w, h]} />
        <meshBasicMaterial color={color} transparent opacity={opacity} depthWrite={false} side={THREE.DoubleSide} />
      </mesh>
      <lineLoop geometry={edges}>
        <lineBasicMaterial color={color} transparent opacity={0.9} />
      </lineLoop>
    </group>
  );
}

function Mark({ position, a, rotation }: { position: [number, number, number]; a: LayoutAperture; rotation?: [number, number, number] }) {
  if (a.d > 0) {
    return (
      <group position={position} rotation={rotation}>
        <mesh>
          <ringGeometry args={[Math.max(0.2, a.d / 2 - 0.5), a.d / 2, 40]} />
          <meshBasicMaterial color={MARK} transparent opacity={0.85} depthWrite={false} side={THREE.DoubleSide} />
        </mesh>
        <mesh>
          <circleGeometry args={[a.d / 2, 40]} />
          <meshBasicMaterial color={MARK} transparent opacity={0.16} depthWrite={false} side={THREE.DoubleSide} />
        </mesh>
      </group>
    );
  }
  if (a.w > 0 && a.h > 0) return <Pane position={position} w={a.w} h={a.h} color={MARK} opacity={0.22} rotation={rotation} />;
  return null;
}

/**
 * The layout's ghosts, in the hologram's own millimetre frame. The layout's origin is the shell's
 * OUTER bottom-left corner in plan view; the mesh is centred, so a layout point (x, y) lands at
 * (x − L/2, y − W/2) — with L taken from the mesh's leftmost edge, which is the same −L/2 for a
 * single body and the body's own left edge when a lid is laid out beside it (the runner places
 * parts along X in order, the body first). z: the cavity floor sits `floor` above the shell's base.
 */
function Ghosts({ layout, dims }: { layout: HologramLayout; dims: THREE.Vector3 }) {
  const [L, W, H] = layout.outer;
  const ox = -dims.x / 2;
  const oy = -W / 2;
  const base = -dims.z / 2;
  const floorZ = base + layout.floor + 0.15; // a hair above the floor so it never z-fights
  const lidZ = base + Math.min(H, dims.z) - 0.15;
  const wall = (a: LayoutAperture): { position: [number, number, number]; rotation: [number, number, number] } | null => {
    // an enclosure aperture lies IN a wall: x runs along that wall (from its left end, seen from
    // outside), z up from the shell's base
    // The generator's frame (helix/domain/enclosure.py): left/right are the x=0 / x=L walls and
    // a.x runs along them in plan y; bottom/top are the y=0 / y=W walls and a.x runs along plan x;
    // front/back are the base and lid PLATE faces, where (a.x, a.y) is a plan position.
    const z = base + a.z;
    switch (a.face) {
      case "left": return { position: [ox, oy + a.x, z], rotation: [Math.PI / 2, Math.PI / 2, 0] };
      case "right": return { position: [ox + L, oy + a.x, z], rotation: [Math.PI / 2, Math.PI / 2, 0] };
      case "bottom": return { position: [ox + a.x, oy, z], rotation: [Math.PI / 2, 0, 0] };
      case "top": return { position: [ox + a.x, oy + W, z], rotation: [Math.PI / 2, 0, 0] };
      case "front": return { position: [ox + a.x, oy + a.y, base + 0.15], rotation: [0, 0, 0] };
      case "back": return { position: [ox + a.x, oy + a.y, lidZ], rotation: [0, 0, 0] };
      default: return null;
    }
  };
  return (
    <group>
      {layout.components.map((c, i) => {
        const z = c.on_lid ? lidZ : floorZ;
        const cx = ox + c.x + c.w / 2;
        const cy = oy + c.y + c.h / 2;
        const labelH = Math.max(2.5, Math.min(7, Math.min(c.w, c.h) * 0.38));
        return (
          <group key={`${c.key}-${i}`}>
            <Pane position={[cx, cy, z]} w={c.w} h={c.h} color={GHOST} opacity={0.2} />
            {c.z_top > 0 && !c.on_lid && (
              // the part's standing height, as a faint lid over the pocket: does the tallest point clear?
              <Pane position={[cx, cy, floorZ + c.z_top]} w={c.w} h={c.h} color={GHOST} opacity={0.06} />
            )}
            <Label text={`${c.label} ${Math.round(c.w)}×${Math.round(c.h)}`} position={[cx, cy, z + 0.5]} height={labelH} />
            {c.apertures.map((a, j) => (
              <Mark key={j} position={[ox + a.x, oy + a.y, c.on_lid ? lidZ : floorZ + Math.max(c.z_top, 0.3)]} a={a} />
            ))}
          </group>
        );
      })}
      {layout.apertures.map((a, i) => {
        const at = wall(a);
        return at ? <Mark key={`ap-${i}`} position={at.position} rotation={at.rotation} a={a} /> : null;
      })}
      {layout.screws.map((s, i) => (
        <mesh key={`sc-${i}`} position={[ox + s.x, oy + s.y, floorZ]}>
          <ringGeometry args={[0.9, 1.6, 24]} />
          <meshBasicMaterial color={GHOST} transparent opacity={0.6} depthWrite={false} side={THREE.DoubleSide} />
        </mesh>
      ))}
    </group>
  );
}

function Carried({
  model, layout, arRef, placeRef, boxRef, mirror,
}: {
  model: Loaded;
  layout: HologramLayout | null;
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
      {layout && layout.components.length > 0 && <Ghosts layout={layout} dims={model.dims} />}
    </group>
  );
}

function KeepRendering() {
  const invalidate = useThree((s) => s.invalidate);
  useFrame(() => invalidate());
  return null;
}

export default function ArHologram({
  stl, layout = null, arRef, placeRef, boxRef, box, mirror, onLoaded,
}: {
  stl: string;
  layout?: HologramLayout | null;
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
        <Carried model={model} layout={layout} arRef={arRef} placeRef={placeRef} boxRef={boxRef} mirror={mirror} />
      )}
    </Canvas>
  );
}

// The Hologram Studio — real CAD feel: the technical-illustration viewer (Z-up, mm grid, flat
// matcap-ish shading with crease edges, orbit) beside LIVE parameter sliders that recompile the
// design through the warm kernel, a print-readiness panel tuned to the Bambu P1S bed, and
// STL / STEP / 3MF exports. "Save to design" writes the values into model.py and re-bakes.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { api, getToken } from "../lib/api";
import { parseSTL } from "../lib/stl";
import { useHelix } from "../lib/store";

interface Param {
  name: string;
  value: string;
  kind: "number" | "bool" | "string";
  description: string;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  choices: string[];
}
interface Hologram {
  slug: string;
  legacy: boolean;
  page?: string;
  name: string;
  brief: { title: string; summary: string; parts: string[] };
  params: Param[];
  files: { stl: string; step: string; mf: string; preview: string };
  meta: { bbox_mm?: number[]; volume_cm3?: number; solid_grams_pla?: number; parts?: string[] };
  engine: string;
}

const P1S_BED = [256, 256, 256];

interface LoadedModel {
  geo: THREE.BufferGeometry;
  size: THREE.Vector3;
  floorZ: number;
}

function useModel(url: string): LoadedModel | null {
  const [model, setModel] = useState<LoadedModel | null>(null);
  useEffect(() => {
    if (!url) return;
    let dead = false;
    void fetch(url, { headers: { "X-Helix-Token": getToken() } })
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        if (dead) return;
        const geo = parseSTL(buf);
        geo.center(); // origin = the model's center; the camera math becomes trivial
        geo.computeBoundingBox();
        const size = new THREE.Vector3();
        geo.boundingBox!.getSize(size);
        setModel((old) => {
          old?.geo.dispose();
          return { geo, size, floorZ: -size.z / 2 };
        });
      })
      .catch(() => undefined);
    return () => {
      dead = true;
    };
  }, [url]);
  return model;
}

function ModelView({ model }: { model: LoadedModel }) {
  const edges = useMemo(() => new THREE.EdgesGeometry(model.geo, 30), [model.geo]);
  return (
    <group>
      <mesh geometry={model.geo}>
        <meshStandardMaterial color="#aebac7" flatShading metalness={0.05} roughness={0.85} />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial color="#2b3742" />
      </lineSegments>
    </group>
  );
}

function KeepRendering() {
  // The orb's canvas proves this build renders when invalidated per frame; without it this canvas
  // stayed permanently black (the demand loop never woke). Cheap: damping wants frames anyway.
  const invalidate = useThree((s) => s.invalidate);
  useFrame(() => invalidate());
  return null;
}

function FrameCamera({ model }: { model: LoadedModel | null }) {
  const camera = useThree((s) => s.camera) as THREE.PerspectiveCamera;
  useEffect(() => {
    if (!model) return;
    const dim = Math.max(model.size.x, model.size.y, model.size.z, 1);
    camera.up.set(0, 0, 1); // the CAD world is Z-up
    camera.position.set(dim * 0.95, -dim * 1.25, dim * 0.8);
    camera.near = dim / 100;
    camera.far = dim * 50;
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
  }, [model, camera]);
  return null;
}

function StudioCanvas({ stlUrl }: { stlUrl: string }) {
  const model = useModel(stlUrl);
  const dim = model ? Math.max(model.size.x, model.size.y, model.size.z) : 120;
  const gridSize = Math.max(200, Math.ceil((dim * 2.2) / 100) * 100);
  return (
    <Canvas gl={{ antialias: true }} camera={{ fov: 40, up: [0, 0, 1], position: [110, -150, 95] }}>
      <color attach="background" args={["#10161c"]} />
      <KeepRendering />
      <FrameCamera model={model} />
      <hemisphereLight args={["#cfe4ee", "#1a2730", 1.1]} />
      <directionalLight position={[dim, -dim * 0.7, dim * 1.4]} intensity={0.7} />
      {model && <ModelView model={model} />}
      {model && (
        <group position={[0, 0, model.floorZ]}>
          <gridHelper args={[gridSize, gridSize / 10, "#1f2c36", "#15202a"]}
            rotation={[Math.PI / 2, 0, 0]} />
          <axesHelper args={[Math.max(20, dim * 0.25)]} />
        </group>
      )}
      <OrbitControls makeDefault enableDamping dampingFactor={0.12} target={[0, 0, 0]} />
    </Canvas>
  );
}

export default function Studio({ slug, title }: { slug: string; title: string }) {
  const navigate = useHelix((s) => s.navigate);
  const buildsVersion = useHelix((s) => s.buildsVersion);
  const [holo, setHolo] = useState<Hologram | null>(null);
  const [values, setValues] = useState<Record<string, number | boolean | string>>({});
  const [dirty, setDirty] = useState(false);
  const [stlUrl, setStlUrl] = useState("");
  const [meta, setMeta] = useState<Hologram["meta"]>({});
  const [busyLine, setBusyLine] = useState("");
  const debounce = useRef<number>(0);
  const generation = useRef(0);

  const load = useCallback(() => {
    void api.get<Hologram>(`/api/holograms/${slug}`).then((h) => {
      setHolo(h);
      if (!h.legacy) {
        const vals: Record<string, number | boolean | string> = {};
        for (const p of h.params) {
          vals[p.name] = p.kind === "number" ? Number(p.value)
            : p.kind === "bool" ? p.value === "True" : p.value;
        }
        setValues(vals);
        setDirty(false);
        setMeta(h.meta || {});
        setStlUrl(h.files.stl ? `${h.files.stl}?v=${Date.now()}` : "");
      }
    }).catch(() => undefined);
  }, [slug]);
  useEffect(load, [load, buildsVersion]);

  const recompile = useCallback((vals: Record<string, number | boolean | string>) => {
    const gen = ++generation.current;
    setBusyLine("Recomputing…");
    void api.post<{ ok: boolean; stl?: string; meta?: Hologram["meta"]; problem?: string; seconds?: number }>(
      `/api/holograms/${slug}/preview`, { overrides: vals },
    ).then((res) => {
      if (gen !== generation.current) return; // a newer drag superseded this one
      if (res.ok && res.stl) {
        setStlUrl(res.stl);
        setMeta(res.meta || {});
        setBusyLine(`Recomputed in ${res.seconds?.toFixed(1) ?? "?"}s`);
      } else {
        setBusyLine(res.problem || "The recompile failed.");
      }
    }).catch(() => gen === generation.current && setBusyLine("The recompile failed."));
  }, [slug]);

  const change = (name: string, value: number | boolean | string) => {
    const next = { ...values, [name]: value };
    setValues(next);
    setDirty(true);
    window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => recompile(next), 420);
  };

  const commit = () => {
    setBusyLine("Saving to the design…");
    void api.post<{ ok: boolean; problem?: string }>(`/api/holograms/${slug}/commit`, { values })
      .then((res) => {
        setBusyLine(res.ok ? "Saved — the design now carries these values." : (res.problem || "Save failed."));
        if (res.ok) load();
      });
  };

  const dl = (url: string) => `${url}${url.includes("?") ? "&" : "?"}t=${encodeURIComponent(getToken())}`;

  if (holo?.legacy) {
    return (
      <div className="h-full pt-14 px-8 pb-6 flex flex-col" style={{ pointerEvents: "auto" }}>
        <div className="flex items-center gap-3 mb-3">
          <button className="btn-nav" onClick={() => navigate({ name: "menu" })}>← Back</button>
          <span className="font-semibold" style={{ color: "var(--cyan)" }}>{title}</span>
          <span className="text-xs" style={{ color: "var(--muted)" }}>
            legacy hologram — ask HELIX for any change and it redraws it on the new engine
          </span>
        </div>
        <iframe src={holo.page} className="flex-1 rounded-xl" style={{ border: "1px solid var(--line)", background: "#0a0e14" }}
          title={title} />
      </div>
    );
  }

  const bbox = meta.bbox_mm || [];
  const fits = bbox.length === 3 && bbox.every((v, i) => v <= P1S_BED[i]);

  return (
    <div className="h-full pt-14 px-6 pb-6 flex gap-4" style={{ pointerEvents: "auto" }}>
      {/* viewer */}
      <div className="flex-1 rounded-2xl overflow-hidden relative"
        style={{ background: "#10161c", border: "1px solid rgba(63,224,224,0.25)" }}>
        <div className="absolute top-3 left-4 z-10 flex items-center gap-3">
          <button className="btn-nav" onClick={() => navigate({ name: "menu" })}>← Back</button>
          <div>
            <div className="font-semibold" style={{ color: "var(--cyan)" }}>
              {holo?.brief.title || title}
            </div>
            {holo?.brief.summary && (
              <div className="text-xs max-w-[420px] elide" style={{ color: "var(--muted)" }}>
                {holo.brief.summary}
              </div>
            )}
          </div>
        </div>
        <div className="absolute bottom-3 left-4 z-10 text-xs" style={{ color: "var(--muted)" }}>
          {busyLine || (holo?.engine ? `build123d ${holo.engine}` : "")}
        </div>
        <StudioCanvas stlUrl={stlUrl} />
      </div>

      {/* panel */}
      <div className="w-[330px] flex flex-col gap-4 overflow-y-auto">
        <div className="glass rounded-2xl p-4">
          <div className="section-title mb-3">Parameters</div>
          {(holo?.params ?? []).map((p) => (
            <div key={p.name} className="mb-3">
              <div className="flex justify-between text-xs mb-1">
                <span style={{ color: "var(--text)" }}>{p.description || p.name}</span>
                {p.kind === "number" && (
                  <span style={{ color: "var(--cyan)" }}>{String(values[p.name])}</span>
                )}
              </div>
              {p.kind === "number" && p.minimum !== null && p.maximum !== null ? (
                <input
                  type="range"
                  min={p.minimum}
                  max={p.maximum}
                  step={p.step ?? (p.maximum - p.minimum) / 100}
                  value={Number(values[p.name] ?? p.value)}
                  className="w-full accent-[#3fe0e0]"
                  onChange={(e) => change(p.name, Number(e.target.value))}
                />
              ) : p.kind === "number" ? (
                <input
                  type="number"
                  value={Number(values[p.name] ?? p.value)}
                  onChange={(e) => change(p.name, Number(e.target.value))}
                  className="w-full"
                />
              ) : p.kind === "bool" ? (
                <label className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
                  <input
                    type="checkbox"
                    checked={Boolean(values[p.name])}
                    onChange={(e) => change(p.name, e.target.checked)}
                  />
                  {p.name}
                </label>
              ) : (
                <select
                  value={String(values[p.name] ?? p.value)}
                  onChange={(e) => change(p.name, e.target.value)}
                  className="w-full"
                >
                  {(p.choices.length ? p.choices : [p.value]).map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              )}
            </div>
          ))}
          {(holo?.params.length ?? 0) === 0 && (
            <div className="text-xs" style={{ color: "var(--muted)" }}>
              This design has no adjustable parameters yet — ask HELIX to add some.
            </div>
          )}
          {dirty && (
            <div className="flex gap-2 mt-2">
              <button className="btn btn-primary text-xs flex-1" onClick={commit}>
                Save to design
              </button>
              <button className="btn text-xs" onClick={load}>Reset</button>
            </div>
          )}
        </div>

        <div className="glass rounded-2xl p-4">
          <div className="section-title mb-3">Print — Bambu P1S</div>
          <div className="text-xs space-y-1.5" style={{ color: "var(--muted)" }}>
            {bbox.length === 3 && (
              <div>
                Size: <span style={{ color: "var(--text)" }}>
                  {bbox[0]} × {bbox[1]} × {bbox[2]} mm</span>
              </div>
            )}
            {bbox.length === 3 && (
              <div style={{ color: fits ? "var(--done)" : "var(--error)" }}>
                {fits ? "✓ Fits the P1S bed (256³)" : "✗ Exceeds the P1S bed (256³) — split or shrink it"}
              </div>
            )}
            {meta.volume_cm3 !== undefined && (
              <div>Volume: <span style={{ color: "var(--text)" }}>{meta.volume_cm3} cm³</span></div>
            )}
            {meta.solid_grams_pla !== undefined && (
              <div>≈ <span style={{ color: "var(--text)" }}>{meta.solid_grams_pla} g</span> PLA solid</div>
            )}
            {(meta.parts?.length ?? 0) > 1 && <div>Parts: {meta.parts!.join(", ")}</div>}
          </div>
          <div className="flex gap-2 mt-3 flex-wrap">
            {holo?.files.step && (
              <a className="btn btn-primary text-xs" href={dl(holo.files.step)} download>STEP</a>
            )}
            {holo?.files.stl && (
              <a className="btn text-xs" href={dl(holo.files.stl)} download>STL</a>
            )}
            {holo?.files.mf && (
              <a className="btn text-xs" href={dl(holo.files.mf)} download>3MF</a>
            )}
          </div>
          <div className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
            STEP opens natively in Bambu Studio — cleanest geometry for slicing.
          </div>
        </div>

        <div className="glass rounded-2xl p-4">
          <div className="section-title mb-2">Change it by talking</div>
          <div className="text-xs" style={{ color: "var(--muted)" }}>
            “Make the walls 3 millimetres”, “add vent slots”, “switch to a screw-down lid” — say it
            on the Console and HELIX edits the design itself.
          </div>
        </div>
      </div>
    </div>
  );
}

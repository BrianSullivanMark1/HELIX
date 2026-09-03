// The Presence Orb — "The Contained Star". HELIX as a captive star: turbulent plasma filaments
// swimming visibly BEHIND a glass shell (a 4-tap parallax march through domain-warped fbm — real
// perceived depth, no volumetrics), stacked-fresnel glass with limb darkening, a corona of wisps
// licking outward that LEAN TOWARD YOU while HELIX listens, spectral flares wrapping the equator
// as it speaks (the 16 TTS/mic bands mapped to longitude via a DataTexture), and one clean
// shockwave from core to limb when a build lands or fails. Temperature carries state — blue-cyan
// attention, amber computation, green resolution, red fault (frequency up, never brightness) —
// and a Reinhard fold keeps the additive stack from ever blowing out to white.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

export const STATE_LOOK: Record<string, { color: [number, number, number]; glow: number; speed: number }> = {
  idle: { color: [0.16, 0.55, 1.0], glow: 0.42, speed: 0.55 },
  listening: { color: [0.2, 0.66, 1.0], glow: 0.8, speed: 1.0 },
  transcribing: { color: [0.3, 0.8, 1.0], glow: 0.95, speed: 2.2 },
  thinking: { color: [1.0, 0.62, 0.16], glow: 0.72, speed: 1.6 },
  speaking: { color: [1.0, 0.74, 0.24], glow: 1.0, speed: 1.3 },
};
export const HUE_LOOK: Record<string, [number, number, number]> = {
  working: [1.0, 0.78, 0.2],
  done: [0.22, 0.92, 0.42],
  error: [1.0, 0.3, 0.32],
};

const NOISE_GLSL = /* glsl */ `
  float hash13(vec3 p){ p = fract(p * 0.1031); p += dot(p, p.zyx + 31.32); return fract((p.x + p.y) * p.z); }
  float vnoise(vec3 p){
    vec3 i = floor(p), f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash13(i),               hash13(i+vec3(1,0,0)), u.x),
          mix(hash13(i+vec3(0,1,0)),   hash13(i+vec3(1,1,0)), u.x), u.y),
      mix(mix(hash13(i+vec3(0,0,1)),   hash13(i+vec3(1,0,1)), u.x),
          mix(hash13(i+vec3(0,1,1)),   hash13(i+vec3(1,1,1)), u.x), u.y),
      u.z);
  }
  float fbm(vec3 p){
    float s = 0.0, a = 0.5;
    for (int i = 0; i < 3; i++){ s += a * vnoise(p); p = p * 2.17 + 11.5; a *= 0.5; }
    return s;
  }
`;

const CORE_VERT = /* glsl */ `
  uniform vec3 uCamLocal;
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vLocal;
  varying vec3 vRay;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vLocal = position;
    vRay = position - uCamLocal;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const CORE_FRAG = /* glsl */ `
  #define MARCH 4
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vLocal;
  varying vec3 vRay;
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uGlow;
  uniform float uSpeed;
  uniform float uEnergy;
  uniform float uWarp;
  uniform float uJitter;
  uniform sampler2D uBandTex;
  ${NOISE_GLSL}

  void main() {
    // ---- parallax plasma interior: march the view ray INSIDE the unit sphere ----
    vec3 ro = normalize(vLocal);
    vec3 rd = normalize(vRay);
    float chord = -2.0 * dot(ro, rd);
    vec3 drift = vec3(0.0, uTime * 0.22 * (0.5 + uSpeed), 0.0)
               + vec3(0.0, sin(uTime * 23.0) * 0.15 * uJitter, 0.0);  // error: frequency, not brightness
    float glowAcc = 0.0, haze = 0.0;
    for (int i = 0; i < MARCH; i++) {
      float t = chord * (float(i) + 0.5) / float(MARCH);
      vec3 p = ro + rd * t;
      vec3 q = p * 2.4 + drift;
      float w = fbm(q + uWarp * fbm(q * 1.3 - drift.yxz));
      float fil = smoothstep(0.49, 0.75, w);
      float depthFade = exp(-t * 1.3);
      haze    += w * depthFade * 0.21;
      glowAcc += fil * fil * depthFade * 0.78;
    }
    vec3 interior = uColor * haze * 1.15 + mix(uColor, vec3(1.0), 0.55) * glowAcc * (0.7 + uGlow);

    // ---- stacked-fresnel glass + limb darkening (the volume read) ----
    float ndv = clamp(dot(normalize(vNormal), normalize(vView)), 0.0, 1.0);
    float shellA = pow(1.0 - ndv, 1.5);
    float shellB = pow(1.0 - ndv, 4.0);
    float shellC = pow(1.0 - ndv, 9.0);
    float limb = mix(1.0, 0.30, pow(1.0 - ndv, 2.0));
    vec3 col = interior * limb
      + uColor * 0.10 * shellA
      + mix(uColor, vec3(1.0), 0.35) * 0.50 * shellB * uGlow
      + vec3(1.0) * 0.90 * shellC * uGlow;

    // ---- spectral flares wrap the equator: speech is weather ----
    vec3 n = normalize(vLocal);
    float lon = atan(n.z, n.x) * 0.15915494 + 0.5;
    float band = texture2D(uBandTex, vec2(lon, 0.5)).r;  // Linear+Repeat = free interp, no seam
    float lick = 0.6 + 0.4 * sin(uTime * 6.0 + lon * 44.0);
    float flare = band * band * pow(1.0 - ndv, 3.0) * lick;
    col += mix(uColor, vec3(1.0), 0.45) * flare * 2.2;

    // ---- the fold: the additive stack can never blow out ----
    col *= 1.55;
    col = col / (1.0 + col);
    gl_FragColor = vec4(col, 1.0);
  }
`;

const CORONA_FRAG = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vLocal;
  varying vec3 vRay;
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uGlow;
  uniform float uEnergy;
  uniform float uPulse;
  uniform float uAttend;
  uniform vec3 uCamDir;
  ${NOISE_GLSL}

  void main() {
    vec3 n = normalize(vLocal);
    float cndv = abs(dot(normalize(vNormal), normalize(vView)));
    float rim = pow(1.0 - cndv, 2.2);
    float reach = fbm(n * 3.0 + vec3(0.0, uTime * 0.18, 0.0));
    reach *= 1.0 + 0.7 * uAttend * max(0.0, dot(n, uCamDir));  // eye contact while listening
    float crawl = fbm(n * 5.0 - vec3(0.0, 0.0, uTime * 0.5));
    float wisp = smoothstep(0.35, 0.75, reach * crawl + rim * 0.35) * rim;
    float alpha = min(wisp * (0.35 + uGlow * 0.5 + uEnergy * 0.6), 0.8);
    vec3 cor = mix(uColor, vec3(1.0), 0.25) * alpha;
    float ring = smoothstep(0.06, 0.0, abs((1.0 - cndv) - (1.0 - uPulse) * 0.9));
    cor += uColor * ring * uPulse * 2.0;
    gl_FragColor = vec4(cor, alpha);
  }
`;

function makeHaloTexture(): THREE.Texture {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(128, 128, 40, 128, 128, 128);
  grad.addColorStop(0, "rgba(255,255,255,0.55)");
  grad.addColorStop(0.35, "rgba(255,255,255,0.16)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(c);
}

const CAM_SCRATCH = new THREE.Vector3();

function OrbScene() {
  const look = useRef({ color: new THREE.Color(0.16, 0.55, 1.0), glow: 0.42, speed: 0.55, energy: 0 });
  const core = useRef<THREE.Mesh>(null!);
  const corona = useRef<THREE.Mesh>(null!);
  const gyro = useRef<THREE.Group>(null!);
  const sparks = useRef<THREE.Points>(null!);
  const halo = useRef<THREE.Sprite>(null!);
  const phase = useRef(0);
  const prevHue = useRef("none");
  const easedBands = useRef(new Float32Array(16));
  const frameProbe = useRef({ n: 0, total: 0, degraded: false });
  const { invalidate, gl } = useThree();

  const bandTex = useMemo(() => {
    const tex = new THREE.DataTexture(new Float32Array(16), 16, 1, THREE.RedFormat, THREE.FloatType);
    tex.magFilter = THREE.LinearFilter;
    tex.minFilter = THREE.LinearFilter;
    tex.wrapS = THREE.RepeatWrapping;
    tex.needsUpdate = true;
    return tex;
  }, []);

  // Shared uniform SLOTS (spread copies the references, so one write drives both materials).
  const shared = useMemo(
    () => ({
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(0.16, 0.55, 1.0) },
      uGlow: { value: 0.42 },
      uEnergy: { value: 0 },
      uPulse: { value: 0 },
      uAttend: { value: 0 },
    }),
    [],
  );
  const coreUniforms = useMemo(
    () => ({
      ...shared,
      uSpeed: { value: 0.55 },
      uWarp: { value: 1.6 },
      uJitter: { value: 0 },
      uCamLocal: { value: new THREE.Vector3(0, 0, 5) },
      uBandTex: { value: bandTex },
    }),
    [shared, bandTex],
  );
  const coronaUniforms = useMemo(
    () => ({ ...shared, uCamLocal: { value: new THREE.Vector3(0, 0, 5) }, uCamDir: { value: new THREE.Vector3(0, 0, 1) } }),
    [shared],
  );

  const haloTex = useMemo(makeHaloTexture, []);
  const sparkGeo = useMemo(() => {
    const n = 420;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = 1.15 + Math.random() * 0.75;
      const theta = Math.random() * Math.PI * 2;
      pos[i * 3] = Math.cos(theta) * r;
      pos[i * 3 + 1] = (Math.random() - 0.5) * 1.4;
      pos[i * 3 + 2] = Math.sin(theta) * r;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return geo;
  }, []);

  const rings = useMemo(
    () => [
      { radius: 1.3, tube: 0.0084, tilt: [1.15, 0, 0.2] as const },
      { radius: 1.46, tube: 0.0063, tilt: [2.05, 0, 0.85] as const },
      { radius: 1.62, tube: 0.005, tilt: [0.45, 0, 2.3] as const },
    ],
    [],
  );

  useFrame(({ camera }, dtRaw) => {
    const dt = Math.min(dtRaw, 0.05);
    const s = useHelix.getState();
    const base = STATE_LOOK[s.orb] ?? STATE_LOOK.idle;
    const hue = s.hue !== "none" ? HUE_LOOK[s.hue] : null;
    const target = hue ?? base.color;
    const err = s.hue === "error";
    const churn = s.orb === "thinking" || s.hue === "working";

    const L = look.current;
    const ease = 0.06;
    L.color.r += (target[0] - L.color.r) * ease;
    L.color.g += (target[1] - L.color.g) * ease;
    L.color.b += (target[2] - L.color.b) * ease;
    L.glow += (base.glow - L.glow) * ease;
    L.speed += (base.speed - L.speed) * ease;

    // Bands: fast attack, smooth release; band² in the shader keeps silence silent.
    const e = easedBands.current;
    let bandsAvg = 0;
    for (let i = 0; i < 16; i++) {
      const t = s.bands[i] ?? 0;
      e[i] += (t - e[i]) * (t > e[i] ? 0.5 : 0.12);
      bandsAvg += e[i];
    }
    bandsAvg /= 16;
    (bandTex.image.data as Float32Array).set(e);
    bandTex.needsUpdate = true;
    const energyTarget = Math.min(1, s.level + bandsAvg);
    L.energy += (energyTarget - L.energy) * 0.35;
    L.energy *= 0.985;

    // Shared + core uniforms.
    shared.uTime.value += dt * (0.6 + L.speed * 0.6);
    shared.uColor.value.copy(L.color);
    shared.uGlow.value = L.glow;
    shared.uEnergy.value = L.energy;
    if (s.hue !== prevHue.current) {
      if (s.hue === "done" || s.hue === "error") shared.uPulse.value = 1;
      prevHue.current = s.hue;
    }
    shared.uPulse.value *= 0.94;
    shared.uAttend.value += ((s.orb === "listening" ? 1 : 0) - shared.uAttend.value) * 0.08;
    coreUniforms.uSpeed.value = L.speed;
    coreUniforms.uWarp.value += ((churn ? 2.1 : 1.6) - coreUniforms.uWarp.value) * 0.05;
    coreUniforms.uJitter.value += ((err ? 1 : 0) - coreUniforms.uJitter.value) * 0.1;

    // Motion.
    phase.current += dt * 1.6;
    const scale = 1 + 0.035 * Math.sin(phase.current) + L.energy * 0.1;
    core.current.scale.setScalar(scale);
    core.current.rotation.y += dt * 0.1 * (1 + L.speed * 0.6);
    gyro.current.rotation.y += dt * 0.132 * (1 + L.speed * 0.5);
    const ring1 = gyro.current.children[1];
    if (ring1) ring1.rotation.z += dt * (err ? -0.5 : 0.15); // desync reads as fault
    sparks.current.rotation.y -= dt * 0.05 * (1 + L.speed * 0.4);

    // The parallax key: the camera in the CORE's local frame (zero-allocation).
    CAM_SCRATCH.copy(camera.position);
    core.current.worldToLocal(CAM_SCRATCH);
    coreUniforms.uCamLocal.value.copy(CAM_SCRATCH);
    coronaUniforms.uCamDir.value.copy(camera.position).normalize();

    // Dressing.
    const low = (e[0] + e[1] + e[2] + e[3]) / 4;
    halo.current.material.color.copy(L.color);
    halo.current.material.opacity = 0.14 + L.glow * 0.26 + low * 0.25;
    halo.current.scale.setScalar(3.1 + low * 0.9);
    const sparkMat = sparks.current.material as THREE.PointsMaterial;
    sparkMat.color.copy(L.color);
    sparkMat.size = 0.01 + L.energy * 0.012;
    sparkMat.opacity = 0.22 + L.glow * 0.3 + L.energy * 0.25;
    gyro.current.children.forEach((child, i) => {
      const m = (child as THREE.Mesh).material as THREE.MeshBasicMaterial;
      m.color.copy(L.color);
      m.opacity = 0.24 + L.glow * 0.3 + 0.1 * Math.sin(phase.current * 1.3 + i * 2.1);
    });

    // Perf ladder: if the first ~120 frames average slow, drop dpr once. No per-frame branching after.
    const probe = frameProbe.current;
    if (!probe.degraded && probe.n < 120) {
      probe.n += 1;
      probe.total += dtRaw;
      if (probe.n === 120 && probe.total / 120 > 0.019) {
        probe.degraded = true;
        gl.setPixelRatio(1.25);
      }
    }
    invalidate();
  });

  const tap = () => void api.post("/api/shell/tap").catch(() => undefined);

  return (
    <group>
      <sprite ref={halo} renderOrder={2}>
        <spriteMaterial map={haloTex} transparent depthWrite={false} blending={THREE.AdditiveBlending} />
      </sprite>
      <mesh ref={core} onClick={tap}>
        <sphereGeometry args={[1, 72, 72]} />
        <shaderMaterial vertexShader={CORE_VERT} fragmentShader={CORE_FRAG} uniforms={coreUniforms} />
      </mesh>
      <mesh ref={corona} renderOrder={1} raycast={() => undefined}>
        <sphereGeometry args={[1.3, 48, 48]} />
        <shaderMaterial
          vertexShader={CORE_VERT}
          fragmentShader={CORONA_FRAG}
          uniforms={coronaUniforms}
          side={THREE.BackSide}
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </mesh>
      <group ref={gyro}>
        {rings.map((r, i) => (
          <mesh key={i} rotation={[r.tilt[0], r.tilt[1], r.tilt[2]]}>
            <torusGeometry args={[r.radius, r.tube, 8, 128]} />
            <meshBasicMaterial transparent opacity={0.35} depthWrite={false} blending={THREE.AdditiveBlending} />
          </mesh>
        ))}
      </group>
      <points ref={sparks} geometry={sparkGeo}>
        <pointsMaterial size={0.012} transparent opacity={0.4} depthWrite={false}
          blending={THREE.AdditiveBlending} sizeAttenuation />
      </points>
    </group>
  );
}

export default function Orb() {
  return (
    <div className="fixed inset-0" style={{ zIndex: 0 }}>
      <Canvas
        camera={{ position: [0, 0.35, 5.1], fov: 42 }}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
        dpr={[1, 2]}
        style={{ pointerEvents: "auto" }}
        frameloop="always"
      >
        <OrbScene />
      </Canvas>
    </div>
  );
}

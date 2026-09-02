// The Presence Orb — HELIX itself. A WebGL circuit-sphere: sparse traces racing charge pulses,
// twinkling junction pads, a breathing reactor heart, a fresnel rim; three tilted gyroscope rings
// and a field of orbiting sparks around it; everything driven by the conversation state machine
// (idle / listening / transcribing / thinking / speaking), overridden whole by the build hue
// (working / done / error), and made audio-reactive by the mic level + spectral bands.
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

const STATE_LOOK: Record<string, { color: [number, number, number]; glow: number; speed: number }> = {
  idle: { color: [0.16, 0.55, 1.0], glow: 0.42, speed: 0.55 },
  listening: { color: [0.2, 0.66, 1.0], glow: 0.8, speed: 1.0 },
  transcribing: { color: [0.3, 0.8, 1.0], glow: 0.95, speed: 2.2 },
  thinking: { color: [1.0, 0.62, 0.16], glow: 0.72, speed: 1.6 },
  speaking: { color: [1.0, 0.74, 0.24], glow: 1.0, speed: 1.3 },
};
const HUE_LOOK: Record<string, [number, number, number]> = {
  working: [1.0, 0.78, 0.2],
  done: [0.22, 0.92, 0.42],
  error: [1.0, 0.3, 0.32],
};

const CORE_VERT = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vLocal;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vLocal = position;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const CORE_FRAG = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec3 vLocal;
  uniform float uTime;
  uniform vec3 uColor;
  uniform float uGlow;
  uniform float uSpeed;
  uniform float uEnergy;

  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }

  void main() {
    vec3 n = normalize(vLocal);
    // Spherical circuit grid: 36 longitudes x 22 latitudes.
    float lon = atan(n.z, n.x) / 6.28318 + 0.5;
    float lat = acos(clamp(n.y, -1.0, 1.0)) / 3.14159;
    vec2 cell = vec2(lon * 36.0, lat * 22.0);
    vec2 id = floor(cell);
    vec2 f = fract(cell);

    // Thin, SPARSE traces along cell edges — a routed board, not a checkerboard.
    float dY = min(f.y, 1.0 - f.y);
    float dX = min(f.x, 1.0 - f.x);
    float hWire = step(0.62, hash(vec2(id.x, floor(cell.y + 0.5)) + 7.3));
    float vWire = step(0.55, hash(vec2(floor(cell.x + 0.5), id.y) + 3.1));
    float edgeH = (1.0 - smoothstep(0.020, 0.055, dY)) * hWire;
    float edgeV = (1.0 - smoothstep(0.020, 0.055, dX)) * vWire;
    float wires = clamp(edgeH + edgeV, 0.0, 1.0);

    // Charge pulses racing ALONG the traces (dashes confined to the wires).
    float laneH = hash(vec2(0.0, floor(cell.y + 0.5))) * 6.28318;
    float laneV = hash(vec2(floor(cell.x + 0.5), 0.0)) * 6.28318;
    float t = uTime * (1.8 + uSpeed * 2.2);
    float pulseH = pow(max(0.0, sin(lon * 44.0 * 6.28318 * 0.15 - t + laneH)), 18.0) * edgeH;
    float pulseV = pow(max(0.0, sin(lat * 44.0 * 6.28318 * 0.15 + t * 0.8 + laneV)), 18.0) * edgeV;
    float charge = (pulseH + pulseV) * 2.6;

    // Junction pads twinkling on their own clocks; the brightest flare white-hot.
    vec2 padF = f - 0.5;
    float pad = 1.0 - smoothstep(0.035, 0.075, length(padF));
    float clock = hash(id + 11.7);
    float tw = 0.5 + 0.5 * sin(uTime * (0.6 + clock * 2.4) + clock * 40.0);
    float padLit = pad * step(0.86, clock) * tw;
    float hot = padLit * step(0.86, tw);

    // The reactor heart: brightens toward the camera-facing center, breathing.
    float face = max(0.0, dot(normalize(vNormal), normalize(vView)));
    float breath = 0.72 + 0.28 * sin(uTime * 0.9);
    float heart = pow(face, 4.0) * breath * (0.55 + uEnergy * 0.9);

    // Fresnel rim.
    float rim = pow(1.0 - face, 3.0);

    vec3 body = vec3(0.008, 0.013, 0.022);
    vec3 col = body
      + uColor * wires * 0.16 * (0.6 + uGlow)
      + uColor * charge * uGlow
      + mix(uColor, vec3(1.0), 0.75) * padLit * 0.9
      + vec3(1.0) * hot * 0.7
      + uColor * heart * 0.55 * uGlow
      + uColor * rim * (0.65 + uEnergy * 0.5) * uGlow;

    gl_FragColor = vec4(col, 1.0);
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

function OrbScene() {
  const look = useRef({ color: new THREE.Color(0.16, 0.55, 1.0), glow: 0.42, speed: 0.55, energy: 0 });
  const core = useRef<THREE.Mesh>(null!);
  const gyro = useRef<THREE.Group>(null!);
  const sparks = useRef<THREE.Points>(null!);
  const halo = useRef<THREE.Sprite>(null!);
  const phase = useRef(0);
  const { invalidate } = useThree();

  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uColor: { value: new THREE.Color(0.16, 0.55, 1.0) },
      uGlow: { value: 0.42 },
      uSpeed: { value: 0.55 },
      uEnergy: { value: 0 },
    }),
    [],
  );

  const haloTex = useMemo(makeHaloTexture, []);
  const sparkGeo = useMemo(() => {
    const n = 420;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = 1.15 + Math.random() * 0.75;
      const theta = Math.random() * Math.PI * 2;
      const y = (Math.random() - 0.5) * 1.4;
      pos[i * 3] = Math.cos(theta) * r;
      pos[i * 3 + 1] = y;
      pos[i * 3 + 2] = Math.sin(theta) * r;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    return geo;
  }, []);

  const rings = useMemo(
    () => [
      { radius: 1.3, tube: 0.012, tilt: [1.15, 0, 0.2] as const, speed: 1.0 },
      { radius: 1.46, tube: 0.009, tilt: [2.05, 0, 0.85] as const, speed: -0.7 },
      { radius: 1.62, tube: 0.007, tilt: [0.45, 0, 2.3] as const, speed: 0.5 },
    ],
    [],
  );

  useFrame((_, dtRaw) => {
    const dt = Math.min(dtRaw, 0.05);
    const s = useHelix.getState();
    const base = STATE_LOOK[s.orb] ?? STATE_LOOK.idle;
    const hue = s.hue !== "none" ? HUE_LOOK[s.hue] : null;
    const target = hue ?? base.color;
    const bandsAvg = s.bands.length ? s.bands.reduce((a, b) => a + b, 0) / s.bands.length : 0;
    const energyTarget = Math.min(1, s.level + bandsAvg);

    const L = look.current;
    const ease = 0.06;
    L.color.r += (target[0] - L.color.r) * ease;
    L.color.g += (target[1] - L.color.g) * ease;
    L.color.b += (target[2] - L.color.b) * ease;
    L.glow += (base.glow - L.glow) * ease;
    L.speed += (base.speed - L.speed) * ease;
    L.energy += (energyTarget - L.energy) * 0.35;
    L.energy *= 0.985;

    uniforms.uTime.value += dt * (0.6 + L.speed * 0.6);
    uniforms.uColor.value.copy(L.color);
    uniforms.uGlow.value = L.glow;
    uniforms.uSpeed.value = L.speed;
    uniforms.uEnergy.value = L.energy;

    phase.current += dt * 1.6;
    const scale = 1 + 0.035 * Math.sin(phase.current) + L.energy * 0.1;
    core.current.scale.setScalar(scale);
    core.current.rotation.y += dt * 0.1 * (1 + L.speed * 0.6);
    gyro.current.rotation.y += dt * 0.22 * (1 + L.speed * 0.5);
    sparks.current.rotation.y -= dt * 0.05 * (1 + L.speed * 0.4);

    (sparks.current.material as THREE.PointsMaterial).color.copy(L.color);
    (sparks.current.material as THREE.PointsMaterial).opacity = 0.35 + L.glow * 0.4;
    halo.current.material.color.copy(L.color);
    halo.current.material.opacity = 0.16 + L.glow * 0.3 + L.energy * 0.2;
    halo.current.scale.setScalar(3.1 + L.energy * 0.5);
    gyro.current.children.forEach((child, i) => {
      const m = (child as THREE.Mesh).material as THREE.MeshBasicMaterial;
      m.color.copy(L.color);
      m.opacity = 0.28 + L.glow * 0.35 + 0.12 * Math.sin(phase.current * 1.3 + i * 2.1);
    });
    invalidate();
  });

  const tap = () => void api.post("/api/shell/tap").catch(() => undefined);

  return (
    <group>
      <sprite ref={halo}>
        <spriteMaterial
          map={haloTex}
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </sprite>
      <mesh ref={core} onClick={tap}>
        <sphereGeometry args={[1, 72, 72]} />
        <shaderMaterial vertexShader={CORE_VERT} fragmentShader={CORE_FRAG} uniforms={uniforms} />
      </mesh>
      <group ref={gyro}>
        {rings.map((r, i) => (
          <mesh key={i} rotation={[r.tilt[0], r.tilt[1], r.tilt[2]]}>
            <torusGeometry args={[r.radius, r.tube, 8, 128]} />
            <meshBasicMaterial
              transparent
              opacity={0.4}
              depthWrite={false}
              blending={THREE.AdditiveBlending}
            />
          </mesh>
        ))}
      </group>
      <points ref={sparks} geometry={sparkGeo}>
        <pointsMaterial
          size={0.014}
          transparent
          opacity={0.5}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          sizeAttenuation
        />
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

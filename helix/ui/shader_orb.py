"""ShaderOrb — an optional GPU-shader Presence layered OVER the proven QPainter orb.

A transparent Three.js WebGL view renders a glowing, audio-reactive energy core. It is revealed ONLY once
the page confirms a successful first render (it sets the document title to a ready sentinel). On any
failure — no PyQt6-WebEngine, a lost GL context, a shader that won't compile — the title is never set, so
the QPainter PresenceOrb underneath stays visible. Both layers receive the same state/level/bands, so the
experience degrades gracefully and nothing is ever lost. The view is transparent for mouse events, so the
Console keeps handling tap-to-talk exactly as before.
"""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QSizePolicy, QStackedLayout, QWidget

from helix.ui.orb import OrbState, PresenceOrb

_READY = "helix-orb-ready"

try:  # WebEngine is optional; without it ShaderOrb is just the QPainter orb
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    _HAVE_WEBENGINE = True
except Exception:  # pragma: no cover - depends on the optional WebEngine dependency
    _HAVE_WEBENGINE = False


class ShaderOrb(QWidget):
    """Drop-in replacement for PresenceOrb: same set_state/set_level/set_bands + clicked signal."""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 240)
        self._ready = False
        self._view = None
        self._fallback = PresenceOrb(self)
        self._fallback.clicked.connect(self.clicked)
        lay = QStackedLayout(self)
        lay.setStackingMode(QStackedLayout.StackingMode.StackAll)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._fallback)
        if _HAVE_WEBENGINE:
            try:
                self._init_webgl(lay)
            except Exception:
                self._view = None  # any setup hiccup → keep the painter orb only

    def _init_webgl(self, lay: QStackedLayout) -> None:
        view = QWebEngineView(self)
        view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        view.page().setBackgroundColor(Qt.GlobalColor.transparent)
        view.setStyleSheet("background: transparent;")
        view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        view.titleChanged.connect(self._on_title)
        view.setHtml(_ORB_HTML)
        view.hide()
        lay.addWidget(view)
        self._view = view

    def _on_title(self, title: str) -> None:
        # The page sets this only after a successful first WebGL frame.
        if title == _READY and self._view is not None and not self._ready:
            self._ready = True
            self._view.show()
            self._view.raise_()

    # ----- same interface the Console drives -----
    def set_state(self, state: OrbState) -> None:
        self._fallback.set_state(state)
        name = state.value if isinstance(state, OrbState) else "idle"
        self._js(f"window.orbState && window.orbState({json.dumps(name)})")

    def set_level(self, level: float) -> None:
        self._fallback.set_level(level)
        try:
            self._js(f"window.orbLevel && window.orbLevel({float(level)})")
        except (TypeError, ValueError):
            pass

    def set_bands(self, bands) -> None:
        self._fallback.set_bands(bands)
        try:
            arr = json.dumps([float(x) for x in list(bands)])
        except (TypeError, ValueError):
            return
        self._js(f"window.orbBands && window.orbBands({arr})")

    def _js(self, code: str) -> None:
        if self._view is not None and self._ready:
            try:
                self._view.page().runJavaScript(f"try{{{code}}}catch(e){{}}")
            except Exception:
                pass


# A transparent Three.js energy core: a Fresnel-lit, audio-reactive sphere with an additive outer glow.
# Fresnel is simple, robust GLSL (no fragile noise), so it compiles reliably; if anything fails the page
# never sets the ready title and the QPainter orb shows instead.
_ORB_HTML = r"""<!doctype html><html><head><meta charset="utf-8" />
<style>html,body{margin:0;height:100%;background:transparent;overflow:hidden}#c{width:100%;height:100%}</style>
<script type="importmap">{ "imports": { "three": "https://unpkg.com/three@0.160.0/build/three.module.js" } }</script>
</head><body><div id="c"></div>
<script type="module">
import * as THREE from "three";
const READY = "helix-orb-ready";
const STATES = {
  idle:        { col: [0.25, 0.88, 0.88], glow: 0.45, speed: 0.25 },
  listening:   { col: [0.25, 0.88, 0.88], glow: 0.85, speed: 0.6 },
  transcribing:{ col: [0.30, 0.95, 0.95], glow: 0.95, speed: 1.3 },
  thinking:    { col: [0.96, 0.70, 0.22], glow: 0.7,  speed: 0.9 },
  speaking:    { col: [1.00, 0.78, 0.34], glow: 1.0,  speed: 0.8 },
};
let cur = STATES.idle, tgt = STATES.idle, level = 0.0, bands = new Array(16).fill(0);
window.orbState = (s) => { tgt = STATES[s] || STATES.idle; };
window.orbLevel = (v) => { level = Math.max(0, Math.min(1, +v || 0)); };
window.orbBands = (a) => { if (Array.isArray(a)) bands = a; };

try {
  const el = document.getElementById("c");
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  const resize = () => renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(renderer.domElement); resize();
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100); camera.position.z = 3.2;
  const fixCam = () => { camera.aspect = el.clientWidth / el.clientHeight; camera.updateProjectionMatrix(); };
  fixCam();
  addEventListener("resize", () => { resize(); fixCam(); });

  const uni = { uTime: { value: 0 }, uColor: { value: new THREE.Color(0.25,0.88,0.88) },
                uGlow: { value: 0.45 }, uLevel: { value: 0 } };
  const vert = `varying vec3 vN; varying vec3 vV; uniform float uTime; uniform float uLevel;
    void main(){ vec3 p = position;
      float w = sin(p.y*6.0 + uTime*2.0)*0.02 + sin(p.x*5.0 - uTime*1.5)*0.02;
      p += normal * (w * (0.6 + uLevel*2.5));
      vec4 mv = modelViewMatrix * vec4(p,1.0);
      vN = normalize(normalMatrix * normal); vV = normalize(-mv.xyz);
      gl_Position = projectionMatrix * mv; }`;
  const frag = `varying vec3 vN; varying vec3 vV; uniform vec3 uColor; uniform float uGlow; uniform float uLevel;
    void main(){ float fres = pow(1.0 - max(dot(vN, vV), 0.0), 2.2);
      float core = 0.18 + 0.5*uGlow;
      vec3 c = uColor * (core + fres*1.6) + uColor*uLevel*0.8;
      float a = clamp(fres*1.3 + core*0.5 + uLevel*0.3, 0.0, 1.0);
      gl_FragColor = vec4(c, a); }`;
  const mat = new THREE.ShaderMaterial({ uniforms: uni, vertexShader: vert, fragmentShader: frag,
    transparent: true, blending: THREE.AdditiveBlending, depthWrite: false });
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(1.0, 24), mat); scene.add(core);

  const glowMat = new THREE.ShaderMaterial({ uniforms: uni,
    vertexShader: `varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0);
      vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }`,
    fragmentShader: `varying vec3 vN; varying vec3 vV; uniform vec3 uColor; uniform float uGlow;
      void main(){ float f = pow(1.0 - max(dot(vN,vV),0.0), 3.0);
        gl_FragColor = vec4(uColor, f * (0.35 + uGlow*0.5)); }`,
    transparent: true, blending: THREE.AdditiveBlending, side: THREE.BackSide, depthWrite: false });
  scene.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1.5, 8), glowMat));

  const clock = new THREE.Clock(); let started = false; let phase = 0;
  (function loop(){
    requestAnimationFrame(loop);
    const dt = clock.getDelta();
    // ease state
    for (const k of ["glow","speed"]) cur[k] += (tgt[k]-cur[k])*0.06;
    for (let i=0;i<3;i++) cur.col[i] += (tgt.col[i]-cur.col[i])*0.06;
    phase += dt * cur.speed;
    const bandAvg = bands.length ? bands.reduce((a,b)=>a+ (+b||0),0)/bands.length : 0;
    const energy = Math.min(1, level + bandAvg);
    uni.uTime.value = phase;
    uni.uColor.value.setRGB(cur.col[0], cur.col[1], cur.col[2]);
    uni.uGlow.value = cur.glow;
    uni.uLevel.value = energy;
    core.rotation.y += dt*0.3*(1+cur.speed); core.rotation.x += dt*0.12;
    const s = 1.0 + 0.05*Math.sin(phase*2.0) + energy*0.12; core.scale.setScalar(s);
    renderer.render(scene, camera);
    if (!started){ started = true; document.title = READY; }  // first frame OK -> reveal the WebGL orb
  })();
} catch (e) { /* leave the title unset so the QPainter orb stays */ }
</script></body></html>"""

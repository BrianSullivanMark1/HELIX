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
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QSizePolicy, QStackedLayout, QWidget

from helix.logging_setup import get_logger
from helix.ui.orb import OrbState, OrbStatus, PresenceOrb

_LOG = get_logger("shader_orb")

_READY = "helix-orb-ready"
_LOST = "helix-orb-lost"  # the page's WebGL context died — hide the view, the painter orb returns

try:  # WebEngine is optional; without it ShaderOrb is just the QPainter orb
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    _HAVE_WEBENGINE = True

    class _LoggingPage(QWebEnginePage):
        """The orb page's JS console lands in helix.log — the ONLY window into why the GPU layer
        did or didn't come up inside the frozen app (shader errors, GL failures, context loss)."""

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 - Qt override
            _LOG.info("orb page js[%s:%s]: %s", getattr(level, "value", level), line, str(message)[:400])

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
        html = _orb_html()
        if html is None:  # no bundled three.js → the painter orb simply stays
            return
        view = QWebEngineView(self)
        view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        view.setPage(_LoggingPage(view))  # page JS console → helix.log (frozen-app observability)
        # The page OWNS its background (the dark circuit city) — no window-transparency dependence.
        # Transparent WebEngine backgrounds fail on some GPUs by painting an opaque WHITE rectangle
        # behind the overlays; an opaque near-black page can't. Matches the page's own clear colour.
        view.page().setBackgroundColor(QColor(5, 7, 11))
        # The page is fully self-contained (three.js is spliced inline) — it needs NO remote access,
        # so none is granted.
        view.titleChanged.connect(self._on_title)
        view.setHtml(html)
        view.hide()
        lay.addWidget(view)
        self._view = view
        # If the page never reaches its first rendered frame, say so in the log — otherwise a
        # frozen-app GPU failure is indistinguishable from the opt-in flag being off.
        QTimer.singleShot(10_000, self._report_if_never_ready)

    def _report_if_never_ready(self) -> None:
        if self._view is not None and not self._ready:
            _LOG.warning("shader orb never reached first render — the painter Presence remains")

    def _on_title(self, title: str) -> None:
        # READY fires only after a successful first WebGL frame; LOST fires if the GL context dies
        # later (a dead opaque view would otherwise COVER the painter orb — hide it instead, and
        # come back only if the context is restored and renders again).
        if self._view is None:
            return
        if title == _READY:
            if not self._ready:
                _LOG.info("shader orb live (first frame rendered)")
            self._ready = True
            self._view.show()
            self._view.raise_()
            # No need to stop the painter orb underneath: this view is opaque
            # (page().setBackgroundColor above sets alpha 255, which makes WebEngine's render delegate
            # set WA_OpaquePaintEvent), so Qt's repaint manager subtracts the covered region and the
            # fallback's update() calls resolve to zero paintEvents on their own. Measured: 0 paints
            # while occluded, resuming the instant this view hides.
        elif title == _LOST and self._ready:
            _LOG.warning("shader orb GL context lost — painter Presence takes over")
            self._ready = False
            self._view.hide()

    # ----- same interface the Console drives -----
    def set_state(self, state: OrbState) -> None:
        self._fallback.set_state(state)
        name = state.value if isinstance(state, OrbState) else "idle"
        self._js(f"window.orbState && window.orbState({json.dumps(name)})")

    def set_status(self, status: OrbStatus) -> None:
        self._fallback.set_status(status)
        name = status.value if isinstance(status, OrbStatus) else "none"
        self._js(f"window.orbStatus && window.orbStatus({json.dumps(name)})")

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


# The V3 Presence: a dark glass sphere etched with a living circuit (charge pulses racing the traces,
# twinkling junction pads, a breathing reactor heart, a hard fresnel rim), a tight neon halo, three
# gyroscopic energy rings carrying in-hue charge packets, and an orbiting spark field. Dark always —
# the body stays near-black; only the energy glows. All GLSL is simple sin/fract/smoothstep so it
# compiles reliably; if anything fails the page never sets the ready title and the QPainter orb shows.
#
# SELF-CONTAINED on purpose: three.js is BUNDLED (helix/ui/assets/three.min.js, spliced into the page
# at load) and the orb code is a classic script — no CDN fetch, no import maps, no ES modules. The
# original CDN import worked in dev but silently failed in the frozen app, leaving the painter orb
# where the V3 Presence should be; a fully inline page removes every environmental dependency.


def _orb_html() -> str | None:
    """The orb page with the bundled three.js spliced in — or None when the asset is missing
    (a broken install just keeps the painter orb; never a blank layer)."""
    try:
        three = (Path(__file__).parent / "assets" / "three.min.js").read_text(encoding="utf-8")
    except OSError:
        return None
    return _ORB_HTML_TEMPLATE.replace("/*__THREE_JS__*/", three)


_ORB_HTML_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8" />
<style>html,body{margin:0;height:100%;background:#05070b;overflow:hidden}#c{width:100%;height:100%}</style>
<script>/*__THREE_JS__*/</script>
</head><body><div id="c"></div>
<script>
const READY = "helix-orb-ready";
// Per-state params. Colour is the STATE's voice; a build status (below) overrides the hue outright.
const STATES = {
  idle:        { col: [0.16, 0.55, 1.00], glow: 0.42, speed: 0.55 },
  listening:   { col: [0.20, 0.66, 1.00], glow: 0.80, speed: 1.0 },
  transcribing:{ col: [0.30, 0.80, 1.00], glow: 0.95, speed: 2.2 },
  thinking:    { col: [1.00, 0.62, 0.16], glow: 0.72, speed: 1.6 },
  speaking:    { col: [1.00, 0.74, 0.24], glow: 1.00, speed: 1.3 },
};
const STATUS = { working: [1.0, 0.78, 0.20], done: [0.22, 0.92, 0.42], error: [1.0, 0.30, 0.32] };
let cur = { col:[0.16,0.55,1.0], glow:0.42, speed:0.55 }, tgt = STATES.idle;
let level = 0.0, bands = new Array(16).fill(0), statusCol = null;
window.orbState  = (s) => { tgt = STATES[s] || STATES.idle; };
window.orbStatus = (s) => { statusCol = STATUS[s] || null; };
window.orbLevel  = (v) => { level = Math.max(0, Math.min(1, +v || 0)); };
window.orbBands  = (a) => { if (Array.isArray(a)) bands = a; };

try {
  const el = document.getElementById("c");
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false,
                                             powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
  renderer.setClearColor(0x05070b, 1);
  const resize = () => renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(renderer.domElement); resize();
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100); camera.position.z = 3.4;
  const fixCam = () => { camera.aspect = el.clientWidth / el.clientHeight; camera.updateProjectionMatrix(); };
  fixCam(); addEventListener("resize", () => { resize(); fixCam(); });

  const uni = {
    uTime:  { value: 0 },
    uColor: { value: new THREE.Color(0.16, 0.55, 1.0) },
    uGlow:  { value: 0.42 },
    uLevel: { value: 0 },
  };

  // 1. The core: a dark glass sphere etched with a LIVING CIRCUIT.
  const coreVert = `
    varying vec3 vN; varying vec3 vV; varying vec3 vL;
    void main(){
      vL = position;
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      vN = normalize(normalMatrix * normal);
      vV = normalize(-mv.xyz);
      gl_Position = projectionMatrix * mv;
    }`;
  const coreFrag = `
    varying vec3 vN; varying vec3 vV; varying vec3 vL;
    uniform vec3 uColor; uniform float uTime; uniform float uGlow; uniform float uLevel;
    float h21(vec2 p){ p = fract(p*vec2(123.34, 345.45)); p += dot(p, p+34.345); return fract(p.x*p.y); }
    void main(){
      vec3 n = normalize(vL);
      float lat = acos(clamp(n.y, -1.0, 1.0));
      float lon = atan(n.z, n.x);
      // fine circuit grid in spherical coords - sparse wires over a near-black body
      float NLat = 20.0, NLon = 32.0;
      vec2 cell = vec2(floor(lon/6.2831853*NLon), floor(lat/3.1415927*NLat));
      vec2 f = vec2(fract(lon/6.2831853*NLon), fract(lat/3.1415927*NLat));
      float hx = h21(cell + 7.0), hy = h21(cell + 41.0);
      float lineU = (hx > 0.62) ? smoothstep(0.030, 0.0, min(f.x, 1.0-f.x)) : 0.0;
      float lineV = (hy > 0.58) ? smoothstep(0.030, 0.0, min(f.y, 1.0-f.y)) : 0.0;
      float wire = max(lineU, lineV);
      // charge pulses race along the wires
      float pulseU = pow(0.5 + 0.5*sin(lat*11.0 - uTime*2.8 + hx*6.28), 14.0);
      float pulseV = pow(0.5 + 0.5*sin(lon*9.0 + uTime*2.3 + hy*6.28), 14.0);
      float charge = lineU*pulseU*2.2 + lineV*pulseV*2.2;
      // a few lit pads at junctions, twinkling on their own clocks
      float pad = 0.0;
      float hp = h21(cell + 99.0);
      if (hp > 0.855) {
        float d = length(f - 0.5);
        float tw = 0.5 + 0.5*sin(uTime*(1.5 + hp*3.0) + hp*40.0);
        pad = smoothstep(0.11, 0.0, d) * (0.25 + 0.85*tw);
      }
      // reactor heart: front-facing inner glow that breathes
      float face = max(dot(vN, vV), 0.0);
      float breathe = 0.80 + 0.20*sin(uTime*1.1);
      float heart = pow(face, 4.0) * breathe * (0.30 + 0.75*uLevel);
      // fresnel rim - the hard neon edge
      float fres = pow(1.0 - face, 3.0);
      // dark body first, energy on top; never a white flood, never a bright disc
      vec3 body = vec3(0.006, 0.010, 0.018) + vec3(0.010, 0.016, 0.028)*face;
      vec3 c = body
             + uColor * wire   * (0.10 + 0.16*uGlow)
             + uColor * charge * (1.10 + 1.40*uGlow)
             + uColor * pad    * (0.55 + 0.65*uGlow)
             + uColor * heart  * (0.16 + 0.38*uGlow)
             + uColor * fres   * (0.80 + 0.95*uGlow);
      c += vec3(1.0) * charge * 0.10 * uGlow;  // a white-hot pin in the racing charges only
      gl_FragColor = vec4(c, 1.0);
    }`;
  const core = new THREE.Mesh(
    new THREE.SphereGeometry(1.0, 72, 72),
    new THREE.ShaderMaterial({ uniforms: uni, vertexShader: coreVert, fragmentShader: coreFrag })
  );
  scene.add(core);

  // 1b. The circuit city: a vast, faint circuit plane deep behind the presence — dark always, the
  //     traces barely-lit in the current hue, fading into black at the edges. The whole app's
  //     backdrop, so the page owns its background (no reliance on window transparency).
  const cityMat = new THREE.ShaderMaterial({ uniforms: uni,
    vertexShader: `varying vec2 vUv; void main(){ vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `varying vec2 vUv; uniform float uTime; uniform vec3 uColor; uniform float uGlow;
      float h21(vec2 p){ p = fract(p*vec2(123.34, 345.45)); p += dot(p, p+34.345); return fract(p.x*p.y); }
      void main(){
        vec2 g = vUv*vec2(52.0, 30.0) + vec2(uTime*0.04, uTime*0.008);
        vec2 cell = floor(g), f = fract(g);
        float hx = h21(cell + 7.0), hy = h21(cell + 41.0);
        float lu = (hx > 0.58) ? smoothstep(0.055, 0.0, min(f.x, 1.0-f.x)) : 0.0;
        float lv = (hy > 0.62) ? smoothstep(0.055, 0.0, min(f.y, 1.0-f.y)) : 0.0;
        float wire = max(lu, lv);
        float hp = h21(cell + 99.0);
        float pad = 0.0;
        if (hp > 0.90) {
          float tw = 0.5 + 0.5*sin(uTime*(0.8 + hp*2.2) + hp*40.0);
          pad = smoothstep(0.16, 0.0, length(f - 0.5)) * tw;
        }
        float charge = (hx > 0.58) ? lu * pow(0.5 + 0.5*sin(g.y*2.2 - uTime*1.4 + hx*6.28), 16.0) : 0.0;
        float vig = smoothstep(1.05, 0.18, length(vUv - 0.5)*1.6);
        vec3 base = vec3(0.007, 0.011, 0.020);
        vec3 c = base
               + uColor * wire   * (0.050 + 0.028*uGlow) * vig
               + uColor * pad    * (0.14 + 0.08*uGlow) * vig
               + uColor * charge * 0.26 * vig;
        gl_FragColor = vec4(c, 1.0);
      }`, depthWrite: false });
  const city = new THREE.Mesh(new THREE.PlaneGeometry(64, 36), cityMat);
  city.position.z = -8.0;
  scene.add(city);

  // 2. A tight neon halo hugging the rim - depth, never a bright disc.
  const halo = new THREE.Mesh(
    new THREE.SphereGeometry(1.07, 48, 48),
    new THREE.ShaderMaterial({ uniforms: uni,
      vertexShader: `varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0);
        vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }`,
      fragmentShader: `varying vec3 vN; varying vec3 vV; uniform vec3 uColor; uniform float uGlow;
        void main(){ float f = pow(1.0 - max(dot(vN,vV),0.0), 3.0);
          gl_FragColor = vec4(uColor, f * (0.05 + uGlow*0.11)); }`,
      transparent: true, blending: THREE.AdditiveBlending, side: THREE.BackSide, depthWrite: false })
  );
  scene.add(halo);

  // 3. Gyroscope: three thin energy rings on tilted axes carrying in-hue charge packets.
  const gyro = new THREE.Group(); scene.add(gyro);
  const RINGS = [
    { r: 1.30, tube: 0.016, tiltX: 1.15, tiltZ: 0.00, packets: 3.0, flow:  1.6 },
    { r: 1.46, tube: 0.012, tiltX: 2.05, tiltZ: 0.85, packets: 5.0, flow: -1.1 },
    { r: 1.62, tube: 0.009, tiltX: 0.45, tiltZ: 2.30, packets: 2.0, flow:  0.8 },
  ];
  for (const cfg of RINGS) {
    const u = { uTime: uni.uTime, uColor: uni.uColor, uGlow: uni.uGlow, uLevel: uni.uLevel,
                uPk: { value: cfg.packets }, uFlow: { value: cfg.flow } };
    const mat = new THREE.ShaderMaterial({ uniforms: u,
      vertexShader: `varying float vAng; void main(){ vAng = atan(position.y, position.x);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `varying float vAng; uniform vec3 uColor; uniform float uTime; uniform float uGlow;
        uniform float uLevel; uniform float uPk; uniform float uFlow;
        void main(){
          float packet = pow(0.5 + 0.5*sin(vAng*uPk - uTime*uFlow*2.2), 18.0);
          float base = 0.16 + 0.14*uGlow;
          float a = base + packet*(1.0 + uLevel*0.8);
          // keep the packet IN HUE: a clipped multiplier washes every colour to white
          vec3 c = uColor*(base*1.1) + uColor*packet*(1.25 + uLevel*0.6) + vec3(1.0)*packet*0.10;
          gl_FragColor = vec4(c, clamp(a, 0.0, 1.0));
        }`,
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(cfg.r, cfg.tube, 16, 220), mat);
    const holder = new THREE.Group();
    holder.rotation.x = cfg.tiltX; holder.rotation.z = cfg.tiltZ;
    holder.add(ring); gyro.add(holder);
  }

  // 4. Spark field: a few hundred points in tilted orbital shells, twinkling, swelling with voice.
  const N = 420, pos = new Float32Array(N*3), seed = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const a = Math.random()*Math.PI*2, b = Math.acos(2*Math.random()-1);
    const r = 1.25 + Math.random()*0.95;
    pos[i*3]   = r*Math.sin(b)*Math.cos(a);
    pos[i*3+1] = r*Math.cos(b)*0.75;
    pos[i*3+2] = r*Math.sin(b)*Math.sin(a);
    seed[i] = Math.random();
  }
  const pGeo = new THREE.BufferGeometry();
  pGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  pGeo.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
  const sparks = new THREE.Points(pGeo, new THREE.ShaderMaterial({ uniforms: uni,
    vertexShader: `attribute float aSeed; varying float vS; uniform float uTime; uniform float uLevel;
      void main(){ vS = aSeed;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        float tw = 0.5 + 0.5*sin(uTime*(0.8 + aSeed*2.6) + aSeed*40.0);
        gl_PointSize = (1.4 + 3.2*aSeed*tw + uLevel*3.0) * (300.0 / -mv.z) * 0.01;
        gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `varying float vS; uniform vec3 uColor; uniform float uTime; uniform float uGlow;
      void main(){ float d = length(gl_PointCoord - 0.5); if (d > 0.5) discard;
        float tw = 0.5 + 0.5*sin(uTime*(0.8 + vS*2.6) + vS*40.0);
        float a = smoothstep(0.5, 0.0, d) * tw * (0.25 + 0.55*uGlow);
        gl_FragColor = vec4(uColor*(0.8 + tw*0.6), a); }`,
    transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
  scene.add(sparks);

  // drive
  const clock = new THREE.Clock(); let started = false; let phase = 0; let acc = 0;
  // A dead GL context would leave an opaque black view COVERING the painter orb — tell the shell,
  // and re-announce readiness if the context comes back and renders again.
  renderer.domElement.addEventListener("webglcontextlost", (e) => {
    e.preventDefault(); started = false; document.title = "helix-orb-lost";
  });
  (function loop(){
    requestAnimationFrame(loop);
    const dt = Math.min(clock.getDelta(), 0.05);
    for (const k of ["glow","speed"]) cur[k] += (tgt[k]-cur[k])*0.06;
    const colTgt = statusCol || tgt.col;  // a build status overrides the conversational hue
    for (let i=0;i<3;i++) cur.col[i] += (colTgt[i]-cur.col[i])*0.06;
    phase += dt * (0.8 + cur.speed);
    const bandAvg = bands.length ? bands.reduce((a,b)=>a+(+b||0),0)/bands.length : 0;
    const energy = Math.min(1, level + bandAvg);
    uni.uTime.value = phase;
    uni.uColor.value.setRGB(cur.col[0], cur.col[1], cur.col[2]);
    uni.uGlow.value = cur.glow;
    uni.uLevel.value = energy;
    core.rotation.y += dt*0.10*(1 + cur.speed*0.6);
    core.rotation.x  = 0.35 + 0.05*Math.sin(phase*0.3);
    gyro.rotation.y += dt*0.22*(1 + cur.speed*0.5);
    gyro.rotation.x  = 0.12*Math.sin(phase*0.22);
    sparks.rotation.y -= dt*0.05*(1 + cur.speed*0.3);
    const s = 1.0 + 0.035*Math.sin(phase*1.6) + energy*0.10;
    core.scale.setScalar(s);
    // The FIRST frame is never throttled: the shell only reveals the WebGL orb once this title lands,
    // and a dropped first frame would leave it on the painter fallback forever. Same path re-announces
    // after a context loss (the handler above clears `started`).
    if (!started){
      renderer.render(scene, camera);
      started = true; document.title = READY;
      acc = 0;
      return;
    }
    // Frame-rate discipline, mirroring the QPainter orb's ~30fps busy / ~15fps idle. This runs for the
    // lifetime of a permanently-open app, in a separate Chromium process: an ungated
    // requestAnimationFrame draws at the display's full refresh (60-165Hz) forever, whether or not
    // anything is moving. The state easing above still runs every frame, so motion stays time-accurate
    // (each rotation is scaled by dt) — only the GL draw is skipped.
    if (document.hidden) return;                       // minimised / occluded: nothing to draw for
    const busy = (energy > 0.01) || !!statusCol || cur.speed > 0.05;
    acc += dt;
    if (acc < (busy ? 1/30 : 1/15)) return;
    acc = 0;
    renderer.render(scene, camera);
  })();
} catch (e) { /* leave the title unset so the QPainter orb stays */ }
</script></body></html>"""

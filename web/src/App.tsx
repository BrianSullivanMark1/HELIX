// The shell: the orb behind everything, a reveal nav, one routed page, and the global panels.
import { useEffect, useState } from "react";
import Orb from "./components/Orb";
import Organism, { type OrganismLook } from "./components/Organism";
import CameraDock from "./components/CameraDock";
import CartDock from "./components/CartDock";
import ConnectModal from "./components/ConnectModal";
import StateColor from "./components/StateColor";
import ConsolePage from "./pages/Console";
import Talk from "./pages/Talk";
import Dream from "./pages/Dream";
import Settings from "./pages/Settings";
import Sparks from "./components/Sparks";
import { RadioButton, RadioDeck } from "./components/Radio";
import Backdrop from "./components/Backdrop";
import Wordmark from "./components/Wordmark";
import Boot from "./components/Boot";
import { TaskDock, TaskLog, TaskToasts } from "./components/Tasks";
import { PowerAsk } from "./components/Shutdown";
import VaultWindow from "./components/Vault";
import TipLayer from "./components/Tip";
import { perf, startGovernor } from "./lib/perf";
import { useJobs } from "./lib/jobs";
import "./shine.css";
import Studio from "./pages/Studio";
import Vault from "./pages/Vault";
import Viewer from "./pages/Viewer";
import { api, connectEvents, tokenUrl } from "./lib/api";
import { applyEvent, cameraFromEvent, useHelix, type CartSnapshot, type Page } from "./lib/store";

interface Snapshot {
  authed: boolean;
  legend: never[];
  voice: never;
  busy: boolean;
  hue: never;
  status: string;
  greeting?: string;
  camera?: Record<string, unknown> | null;
  cart?: CartSnapshot | null;
  dream?: { running?: boolean; line?: string } | null;
  murmur?: { text?: string; kind?: string; at?: string } | null;
}

/** THE LEAP BETWEEN PAGES (Brian, 2026-09-22: no cut between tabs - the face travels). Leaving Talk,
 *  a snapshot of the big face flies down into the corner and the docked head takes over; entering
 *  Talk, the docked head flies up and grows into the big face. Snapshots need the canvases to keep
 *  their drawing buffer (Organism sets preserveDrawingBuffer). */
function leapFace(from: "full" | "dock") {
  const src = document.querySelector<HTMLCanvasElement>(from === "full" ? ".face-full canvas" : ".face-dock canvas");
  if (!src) return;
  let url = "";
  try { url = src.toDataURL("image/png"); } catch { return; }
  const r = src.getBoundingClientRect();
  const W = window.innerWidth, H = window.innerHeight;
  const dockW = 210, dockCx = W - 26 - dockW / 2, dockCy = H - 40 - dockW / 2;
  const img = document.createElement("img");
  img.src = url; img.className = "face-leap"; img.alt = "";
  img.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;z-index:26;pointer-events:none;transform-origin:50% 50%;will-change:transform,opacity;`;
  document.body.appendChild(img);
  const fullCx = r.left + r.width / 2, fullCy = r.top + r.height * 0.47;   // the face sits a touch above the middle of the big canvas
  const frames = from === "full"
    ? [{ transform: "translate(0,0) scale(1)", opacity: 1 }, { transform: `translate(${dockCx - fullCx}px, ${dockCy - fullCy}px) scale(0.26)`, opacity: 0.15 }]
    : [{ transform: "translate(0,0) scale(1)", opacity: 1 }, { transform: `translate(${W / 2 - (r.left + r.width / 2)}px, ${H * 0.47 - (r.top + r.height / 2)}px) scale(3.4)`, opacity: 0 }];
  const a = img.animate(frames, { duration: from === "full" ? 620 : 560, easing: "cubic-bezier(.2,.85,.25,1)", fill: "forwards" });
  a.onfinish = () => img.remove();
  window.setTimeout(() => img.remove(), 900);
}

export default function App() {
  const page = useHelix((s) => s.page);
  const navigate = useHelix((s) => s.navigate);
  const camera = useHelix((s) => s.camera);
  const cart = useHelix((s) => s.cart);
  const dream = useHelix((s) => s.dream);
  const connectModal = useHelix((s) => s.connectModal);
  const lightbox = useHelix((s) => s.lightbox);
  const [menuOpen, setMenuOpen] = useState(false);
  // THE STAGE: click the docked face and it leaps to the front, the page pushed back behind it
  const [stage, setStage] = useState(false);
  useEffect(() => {
    if (!stage) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setStage(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stage]);
  useEffect(() => { setStage(false); }, [page.name]);
  // the leap rides on navigate itself: the snapshot must be taken BEFORE the page changes
  const [dockDelayed, setDockDelayed] = useState(false);
  useEffect(() => {
    const original = useHelix.getState().navigate;
    useHelix.setState({
      navigate: (p) => {
        const now = useHelix.getState().page.name;
        if (now === "talk" && p.name !== "talk") { leapFace("full"); setDockDelayed(true); window.setTimeout(() => setDockDelayed(false), 480); }
        else if (now !== "talk" && p.name === "talk") leapFace("dock");
        original(p);
      },
    });
    return () => { useHelix.setState({ navigate: original }); };
  }, []);
  const [radioOpen, setRadioOpen] = useState(false);
  const [powerAsk, setPowerAsk] = useState(false);
  // THE VAULT opens from the Console's Vault tab, from Settings, or by the helix-vault event
  const [vaultOpen, setVaultOpen] = useState(false);
  useEffect(() => { const on = () => setVaultOpen(true); window.addEventListener("helix-vault", on); return () => window.removeEventListener("helix-vault", on); }, []);
  const logOpen = useJobs((s) => s.logOpen !== null);
  // the boot curtain lifts when the backend answers once
  const [booted, setBooted] = useState(false);
  useEffect(() => { void api.get("/api/snapshot").then(() => setBooted(true)).catch(() => setBooted(true)); }, []);
  // THE GOVERNOR: measures the frame time and steps the art down on a slow machine (lib/perf.ts)
  useEffect(() => { startGovernor(); }, []);
  // the frame-rate badge: Settings > Overview > Show the frame rate (kept on this PC)
  const [fpsOn, setFpsOn] = useState<boolean>(() => { try { return localStorage.getItem("helix_fps") === "1"; } catch { return false; } });
  const [fpsLine, setFpsLine] = useState({ text: "", slow: false });
  useEffect(() => {
    const on = () => { try { setFpsOn(localStorage.getItem("helix_fps") === "1"); } catch { /* fine */ } };
    window.addEventListener("helix-fps-toggle", on);
    return () => window.removeEventListener("helix-fps-toggle", on);
  }, []);
  useEffect(() => {
    if (!fpsOn) return;
    const id = window.setInterval(() => setFpsLine({ text: `${perf.fps} FPS · ${perf.frameMs} MS · ${perf.level.toUpperCase()}${perf.mode === "auto" ? " · AUTO" : ""}`, slow: perf.fps < 45 }), 1000);
    return () => window.clearInterval(id);
  }, [fpsOn]);
  // the deploy lane's lines ride the event stream; the Deploy window listens
  useEffect(() => {
    const relay = (e: Event) => window.dispatchEvent(new CustomEvent("helix-deploy", { detail: (e as CustomEvent).detail }));
    window.addEventListener("helix-event-deploy", relay); window.addEventListener("helix-event-deploy_done", relay);
    return () => { window.removeEventListener("helix-event-deploy", relay); window.removeEventListener("helix-event-deploy_done", relay); };
  }, []);
  // HELIX IS OFF: the backend quit (Settings -> Power, the last tab rule, or a crash). The stream
  // stops trying, the WebGL pages unmount, and one calm screen says how to start it again.
  const [off, setOff] = useState<string | null>(null);
  // THE ORB'S BODY (Settings -> Voice & look -> The orb): the avatar unless "The star" was chosen.
  const [body, setBody] = useState<{ style: "star" | "cell"; look: OrganismLook }>({ style: "cell", look: { phase: "face", hue: 185, energy: 1 } });
  useEffect(() => {
    const read = () => void api.get<{ values?: Record<string, unknown> }>("/api/settings").then((d) => {
      const v = d.values ?? {};
      const phase = String(v.orb_phase || "face");
      setBody({
        style: v.orb_style === "star" ? "star" : "cell",
        look: { phase: phase === "core" || phase === "cell" || phase === "storm" ? phase : "face", hue: Number(v.orb_hue ?? 185) || 185, energy: Math.max(0.3, Math.min(2.5, Number(v.orb_energy ?? 1) || 1)) },
      });
    }).catch(() => undefined);
    read();
    window.addEventListener("helix-settings-saved", read);
    return () => window.removeEventListener("helix-settings-saved", read);
  }, []);
  useEffect(() => {
    const onOff = (e: Event) => setOff((e as CustomEvent).detail?.reason || "HELIX has stopped.");
    window.addEventListener("helix-off", onOff);
    return () => window.removeEventListener("helix-off", onOff);
  }, []);
  // Updates waiting (Settings -> Updates): the built face is behind its source, or the Python
  // changed since launch. Polled gently; the Settings button pulses until it is dealt with.
  const [updates, setUpdates] = useState(false);
  // THE BRAIN: when no Claude is connected the header says so in glowing amber, left of the nav,
  // and the menu burns orange until it is fixed - out of the way, impossible to miss.
  const [brain, setBrain] = useState<{ ok: boolean; line: string }>({ ok: true, line: "" });
  // THE TOOLS: what the deploy lane needs on this PC (gcloud signed in, the Firebase CLI, the
  // console checkout, a GitHub token). Missing ones light Settings the same way a missing brain does.
  const [needs, setNeeds] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    const check = () => {
      void api.get<{ face?: { stale?: boolean }; backend?: { stale?: boolean } }>("/api/face/status")
        .then((s) => { if (alive) setUpdates(Boolean(s.face?.stale || s.backend?.stale)); })
        .catch(() => undefined);
      void api.get<{ brain?: { tone?: string; line?: string }; secrets?: Record<string, boolean> }>("/api/settings")
        .then((d) => {
          if (!alive) return;
          const connected = Boolean(d.secrets?.claude_code_oauth_token || d.secrets?.claude_api_key);
          setBrain({ ok: connected, line: connected ? "" : "Claude is not connected" });
        })
        .catch(() => undefined);
    };
    // the tools check spawns gcloud and firebase: once now, then every five minutes, and after a save
    const tools = () => void api.get<{ needs?: string[] }>("/api/deploy/tools").then((d) => { if (alive) setNeeds(d.needs || []); }).catch(() => undefined);
    tools();
    const tid = window.setInterval(tools, 300000);
    window.addEventListener("helix-settings-saved", tools);
    check();
    const id = window.setInterval(check, 30000);
    window.addEventListener("helix-settings-saved", check);
    return () => { alive = false; window.clearInterval(id); window.clearInterval(tid); window.removeEventListener("helix-settings-saved", check); window.removeEventListener("helix-settings-saved", tools); };
  }, []);

  useEffect(() => {
    const stop = connectEvents(applyEvent, () => {
      void api.get<Snapshot>("/api/snapshot").then((snap) => {
        const s = useHelix.getState();
        s.set({
          authed: snap.authed,
          legend: snap.legend,
          voice: snap.voice,
          busy: snap.busy,
          hue: snap.hue,
          status: snap.status,
          idleLine: (snap.voice as { idle_line?: string })?.idle_line ?? "Ready when you are.",
        });
        // A camera panel the backend still holds (this page reloaded, or reconnected) comes back
        // up with the same id, so a look parked on it still finds its panel.
        if (snap.camera && snap.camera.id) {
          if (s.camera?.id !== snap.camera.id) s.set({ camera: cameraFromEvent(snap.camera), overlays: [], hologram: null });
        } else if (s.camera) {
          s.set({ camera: null, captureOrder: null, overlays: [], hologram: null, cameraCapture: null });
        }
        if (snap.cart !== undefined) s.set({ cart: snap.cart || null });
        // A dream session already running (a reload at 3 AM, a reconnect) shows its chip at once.
        if (snap.dream !== undefined) {
          s.set({
            dream: snap.dream
              ? { running: Boolean(snap.dream.running), line: snap.dream.line || "" }
              : null,
          });
        }
        // Mid-session, the star's last words come back with the page so it isn't mute on reload.
        if (snap.murmur && snap.murmur.text) {
          s.set({
            murmur: {
              text: snap.murmur.text, kind: snap.murmur.kind || "note", at: snap.murmur.at || "",
              seq: (s.murmur?.seq ?? 0) + 1,
            },
          });
        }
        if (snap.greeting) {
          s.addBubble({
            id: "greeting", role: "helix", text: snap.greeting,
            visuals: [], sources: [], actions: [], images: [],
          });
        }
      }).catch(() => undefined);
    });
    return stop;
  }, []);

  // The model asked to open a build — resolve it exactly like a menu click.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { slug: string; name: string };
      void openBuild(detail.slug, detail.name, navigate);
    };
    window.addEventListener("helix-open-build", handler);
    return () => window.removeEventListener("helix-open-build", handler);
  }, [navigate]);

  // The header stays put - it used to tuck itself away on Talk after 5 s and nobody could find it.

  const onConsole = page.name === "talk"; // the orb page: nav tucks away, the orb fills the window

  if (off) {
    return (
      <div className="h-full w-full relative overflow-hidden flex items-center justify-center">
        <div className="atmosphere" style={{ zIndex: 1 }} />
        <div className="glass rounded-2xl p-8 max-w-[460px] text-center relative" style={{ zIndex: 2 }}>
          <div className="font-display text-glow-cyan text-[22px] font-bold tracking-[4px]" style={{ color: "var(--cyan)" }}>◉ HELIX IS OFF</div>
          <div className="text-[13px] mt-3" style={{ color: "var(--muted)" }}>{off}</div>
          <div className="text-[13px] mt-4">Start it again from the desktop icon; this tab can be closed.</div>
          <button className="btn btn-primary mt-5" onClick={() => window.location.reload()}>Try to reconnect</button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full relative overflow-hidden">
      <div className="atmosphere" style={{ zIndex: 1 }} />
      {/* THE BACKDROP paints first: everything else (the face, the pages) sits on top of it */}
      <Backdrop />
      {onConsole && (body.style === "cell" ? <Organism look={body.look} /> : <Orb />)}
      <Sparks />
      <RadioDeck open={radioOpen} onClose={() => setRadioOpen(false)} look={body.look} />
      <StateColor />

      {/* reveal strip */}
      <nav
        className="absolute top-0 left-0 right-0 flex items-center px-5 py-3"
        style={{ zIndex: 29 }}
      >
        <Wordmark onClick={() => navigate({ name: "console" })} />
        <div className="flex-1" />
        {!brain.ok && page.name !== "settings" && (
          <button className="brain-warn" title="Open Settings -> The brain and connect Claude (a Claude Code sign-in token or an API key)"
            onClick={() => navigate({ name: "settings" })}>
            <i /> {brain.line} · connect it in Settings
          </button>
        )}
        <div className="topnav">
          <button className={`topnav-btn${page.name === "console" || page.name === "menu" || page.name === "board" ? " on" : ""}`}
            onClick={() => navigate({ name: "console" })}>
            <span className="topnav-ic grid" aria-hidden="true"><i /><i /><i /><i /></span><span>Console</span>
          </button>
          <button className={`topnav-btn${page.name === "talk" ? " on" : ""}`}
            onClick={() => navigate({ name: "talk" })}>
            <span className="topnav-ic orb" aria-hidden="true"><i className="r" /><i className="r r2" /><b /></span><span>Talk</span>
          </button>
          <span style={{ width: 1, height: 18, background: "var(--line)", margin: "0 4px" }} />
          <RadioButton open={radioOpen} onClick={() => setRadioOpen((o) => !o)} />
          <div className="relative">
            <button className={`btn-nav${(!brain.ok || needs.length > 0) && page.name !== "settings" ? " nav-burn" : updates && page.name !== "settings" ? " nav-alert" : ""}`} title={!brain.ok ? "Claude is not connected - open Settings" : needs.length ? `${needs.length} tool${needs.length === 1 ? "" : "s"} missing on this PC - open Settings > Tools` : updates ? "Updates waiting - open Settings" : "Menu"}
              onClick={() => setMenuOpen((m) => !m)}>☰</button>
            {menuOpen && (
              <div className="glass rounded-xl p-1 absolute right-0 mt-1 flex flex-col min-w-[190px]" style={{ zIndex: 40 }}
                onMouseLeave={() => setMenuOpen(false)}>
                {([
                  ["⚙ Settings" + (!brain.ok ? "  ▲ connect Claude" : needs.length ? `  ▲ ${needs.length} tool${needs.length === 1 ? "" : "s"} to fix` : updates ? "  ●" : ""), { name: "settings" }],
                  ["◐ Dream journal", { name: "dream" }],
                ] as [string, Page][]).map(([label, target]) => (
                  <button key={label} className="btn-nav text-left" style={target.name === "settings" && (!brain.ok || needs.length > 0) ? { color: "#ff8a3d" } : updates && target.name === "settings" ? { color: "var(--working)" } : undefined}
                    onClick={() => { setMenuOpen(false); navigate(target); }}>{label}</button>
                ))}
                <button className="btn-nav text-left" onClick={() => { setMenuOpen(false); setRadioOpen(true); }}>♫ HELIX radio</button>
                <button className="btn-nav text-left power" data-tip="Power HELIX off - it asks first, then the lights go out" onClick={() => { setMenuOpen(false); setPowerAsk(true); }}>⏻ Power off</button>
                <div className="px-3 pt-1 text-[10px] tracking-wider" style={{ color: "var(--muted)" }} data-tip="When the page you are looking at was built (UTC)">
                  build {__HELIX_BUILD__}
                </div>
              </div>
            )}
          </div>
        </div>
      </nav>
      <Boot ready={booted} minMs={1400} />

      {/* CURRENT TASKS on every page: the strip at the very bottom, the log window, the toasts */}
      <TaskDock />
      <TaskLog />
      <TaskToasts />
      <TipLayer />
      {powerAsk && <PowerAsk onClose={() => setPowerAsk(false)} />}
      {vaultOpen && <VaultWindow onClose={() => setVaultOpen(false)} />}
      {fpsOn && fpsLine.text && <div className={`fps-badge${fpsLine.slow ? " slow" : ""}`} aria-hidden="true">{fpsLine.text}</div>}

      <main className={`absolute inset-0${stage ? " stage-back" : ""}`} style={{ zIndex: 10, pointerEvents: "none" }}>
        {(page.name === "console" || page.name === "menu" || page.name === "board") && <ConsolePage />}
        {page.name === "talk" && <Talk project={page.project} />}
        {page.name === "settings" && <Settings />}
        {page.name === "dream" && <Dream />}
        {page.name === "vault" && <Vault slug={page.slug} title={page.title} />}
        {page.name === "studio" && <Studio slug={page.slug} title={page.title} />}
        {page.name === "viewer" && (
          <Viewer slug={page.slug} title={page.title} url={page.url} server={page.server} />
        )}
      </main>

      {/* the docked orb: HELIX is one click away on every page; Talk is the full orb */}
      {/* the docked head is unmounted (not just hidden) while the radio deck or a log is open: one WebGL canvas fewer */}
      {!onConsole && !off && !stage && !dockDelayed && !radioOpen && !logOpen && (body.style === "cell"
        ? <Organism look={body.look} mini onClick={() => setStage(true)} />
        : <button className="orb-dock" data-tip="Talk to HELIX" onClick={() => navigate({ name: "talk" })} style={{ zIndex: 25 }}><span className="orb-dock-core" /></button>)}
      {stage && !off && body.style === "cell" && (
        <div className="stage-leap" style={{ position: "fixed", inset: 0, zIndex: 30, pointerEvents: "none" }}>
          <Organism look={body.look} mode="stage" onClose={() => setStage(false)} onClick={() => { setStage(false); navigate({ name: "talk" }); }} />
        </div>
      )}

      {/* The dream chip: a session of self-improvement is drafting in the background right now.
          Small and out of the way (under the nav, clear of the legend strip and the input row);
          it opens Settings, where the Dreaming card holds the status line and the stop button. */}
      {dream?.running && (
        <button
          className="glass rounded-full px-3 py-1 text-xs absolute"
          style={{ top: 52, right: 18, zIndex: 25, color: "var(--working)", pointerEvents: "auto" }}
          title={dream.line || "HELIX is dreaming — improving its own code in the background."}
          onClick={() => navigate({ name: "settings" })}
        >
          ◐ dreaming
        </button>
      )}

      {/* The camera panel stays MOUNTED across pages (its stream and tracker keep running while
          you glance at the Menu or Settings) and simply hides off the Console, where the AR
          surface belongs. Keyed by session id so a re-open (new id) always remounts and can
          never 'stick' on a stale stream. */}
      {camera && <CameraDock key={camera.id} session={camera} hidden={!onConsole} />}
      {cart && <CartDock cart={cart} hidden={!onConsole} />}
      {connectModal && <ConnectModal modal={connectModal} />}

      {lightbox && (
        <div
          className="fixed inset-0 flex items-center justify-center"
          style={{ zIndex: 60, background: "rgba(3,6,9,0.85)", cursor: "zoom-out" }}
          onClick={() => useHelix.getState().set({ lightbox: "" })}
        >
          <img src={tokenUrl(lightbox)} alt="" style={{ maxWidth: "92vw", maxHeight: "92vh", borderRadius: 12, border: "1px solid var(--line)" }} />
        </div>
      )}
    </div>
  );
}

export async function openBuild(
  slug: string,
  name: string,
  navigate: (p: Page) => void,
): Promise<void> {
  try {
    const res = await api.post<{
      mode: string;
      url?: string;
      port?: number;
      name?: string;
    }>(`/api/builds/${slug}/open`);
    const title = res.name || name;
    if (res.mode === "vault") navigate({ name: "vault", slug, title });
    else if (res.mode === "hologram") navigate({ name: "studio", slug, title });
    else if (res.mode === "page") navigate({ name: "viewer", slug, title, url: res.url! });
    else if (res.mode === "server")
      navigate({ name: "viewer", slug, title, url: res.url!, server: true });
  } catch {
    /* stays where it is; the status line will have said why */
  }
}

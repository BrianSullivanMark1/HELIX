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

export default function App() {
  const page = useHelix((s) => s.page);
  const navigate = useHelix((s) => s.navigate);
  const camera = useHelix((s) => s.camera);
  const cart = useHelix((s) => s.cart);
  const dream = useHelix((s) => s.dream);
  const connectModal = useHelix((s) => s.connectModal);
  const lightbox = useHelix((s) => s.lightbox);
  const [menuOpen, setMenuOpen] = useState(false);
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
  useEffect(() => {
    let alive = true;
    const check = () => {
      void api.get<{ face?: { stale?: boolean }; backend?: { stale?: boolean } }>("/api/face/status")
        .then((s) => { if (alive) setUpdates(Boolean(s.face?.stale || s.backend?.stale)); })
        .catch(() => undefined);
    };
    check();
    const id = window.setInterval(check, 30000);
    return () => { alive = false; window.clearInterval(id); };
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
      {onConsole && (body.style === "cell" ? <Organism look={body.look} /> : <Orb />)}
      <StateColor />

      {/* reveal strip */}
      <nav
        className="absolute top-0 left-0 right-0 flex items-center px-5 py-3"
        style={{ zIndex: 29 }}
      >
        <button
          className="font-display text-glow-cyan text-[17px] font-bold tracking-[3px] bg-transparent border-none"
          style={{ color: "var(--cyan)" }}
          onClick={() => navigate({ name: "console" })}
        >
          ◉ HELIX
        </button>
        <div className="flex-1" />
        <div className="glass rounded-xl px-1 py-0.5 flex gap-0.5 items-center">
          <button className="btn-nav" style={page.name === "console" || page.name === "menu" || page.name === "board" ? { color: "var(--cyan)" } : undefined}
            onClick={() => navigate({ name: "console" })}>▦ Console</button>
          <button className="btn-nav" style={page.name === "talk" ? { color: "var(--cyan)" } : undefined}
            onClick={() => navigate({ name: "talk" })}>◉ Talk</button>
          <span style={{ width: 1, height: 18, background: "var(--line)", margin: "0 4px" }} />
          <div className="relative">
            <button className={`btn-nav${updates && page.name !== "settings" ? " nav-alert" : ""}`} title={updates ? "Updates waiting - open Settings" : "Menu"}
              onClick={() => setMenuOpen((m) => !m)}>☰</button>
            {menuOpen && (
              <div className="glass rounded-xl p-1 absolute right-0 mt-1 flex flex-col min-w-[190px]" style={{ zIndex: 40 }}
                onMouseLeave={() => setMenuOpen(false)}>
                {([
                  ["⚙ Settings" + (updates ? "  ●" : ""), { name: "settings" }],
                  ["◐ Dream journal", { name: "dream" }],
                ] as [string, Page][]).map(([label, target]) => (
                  <button key={label} className="btn-nav text-left" style={updates && target.name === "settings" ? { color: "var(--working)" } : undefined}
                    onClick={() => { setMenuOpen(false); navigate(target); }}>{label}</button>
                ))}
                <div className="px-3 pt-1 text-[10px] tracking-wider" style={{ color: "var(--muted)" }} title="When the page you are looking at was built (UTC)">
                  build {__HELIX_BUILD__}
                </div>
              </div>
            )}
          </div>
        </div>
      </nav>

      <main className="absolute inset-0" style={{ zIndex: 10, pointerEvents: "none" }}>
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
      {!onConsole && (
        <button className="orb-dock" title="Talk to HELIX" onClick={() => navigate({ name: "talk" })} style={{ zIndex: 25 }}>
          <span className="orb-dock-core" />
        </button>
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

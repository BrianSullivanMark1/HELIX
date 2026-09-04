// The shell: the orb behind everything, a reveal nav, one routed page, and the global panels.
import { useEffect, useRef, useState } from "react";
import Orb from "./components/Orb";
import CameraDock from "./components/CameraDock";
import ConnectModal from "./components/ConnectModal";
import StateColor from "./components/StateColor";
import Console from "./pages/Console";
import Menu from "./pages/Menu";
import Settings from "./pages/Settings";
import Studio from "./pages/Studio";
import Vault from "./pages/Vault";
import Viewer from "./pages/Viewer";
import { api, connectEvents, tokenUrl } from "./lib/api";
import { applyEvent, cameraFromEvent, useHelix, type Page } from "./lib/store";

interface Snapshot {
  authed: boolean;
  legend: never[];
  voice: never;
  busy: boolean;
  hue: never;
  status: string;
  greeting?: string;
  camera?: Record<string, unknown> | null;
}

export default function App() {
  const page = useHelix((s) => s.page);
  const navigate = useHelix((s) => s.navigate);
  const camera = useHelix((s) => s.camera);
  const connectModal = useHelix((s) => s.connectModal);
  const lightbox = useHelix((s) => s.lightbox);
  const [navShown, setNavShown] = useState(true);
  const navTimer = useRef<number>(0);

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

  // Nav reveal: shown at launch, tucked after 5s on the console; a strip at the top re-reveals.
  useEffect(() => {
    if (page.name === "console") {
      navTimer.current = window.setTimeout(() => setNavShown(false), 5000);
      return () => window.clearTimeout(navTimer.current);
    }
    setNavShown(true);
    return undefined;
  }, [page.name]);

  const onConsole = page.name === "console";

  return (
    <div className="h-full w-full relative overflow-hidden">
      <div className="atmosphere" style={{ zIndex: 1 }} />
      {onConsole && <Orb />}
      <StateColor />

      {/* reveal strip */}
      <div
        className="absolute top-0 left-0 right-0 h-3"
        style={{ zIndex: 30 }}
        onMouseEnter={() => setNavShown(true)}
      />
      <nav
        className="absolute top-0 left-0 right-0 flex items-center px-5 py-3 transition-transform duration-300"
        style={{ zIndex: 29, transform: navShown ? "translateY(0)" : "translateY(-110%)" }}
        onMouseLeave={() => onConsole && setNavShown(false)}
      >
        <button
          className="font-display text-glow-cyan text-[17px] font-bold tracking-[3px] bg-transparent border-none"
          style={{ color: "var(--cyan)" }}
          onClick={() => navigate({ name: "console" })}
        >
          ◉ HELIX
        </button>
        <div className="flex-1" />
        <div className="glass rounded-xl px-1 py-0.5 flex gap-0.5">
          {(
            [
              ["◉ Console", { name: "console" }],
              ["☰ Menu", { name: "menu" }],
              ["⚙ Settings", { name: "settings" }],
            ] as [string, Page][]
          ).map(([label, target]) => (
            <button
              key={label}
              className="btn-nav"
              style={page.name === target.name ? { color: "var(--cyan)" } : undefined}
              onClick={() => navigate(target)}
            >
              {label}
            </button>
          ))}
        </div>
      </nav>

      <main className="absolute inset-0" style={{ zIndex: 10, pointerEvents: "none" }}>
        {page.name === "console" && <Console />}
        {page.name === "menu" && <Menu />}
        {page.name === "settings" && <Settings />}
        {page.name === "vault" && <Vault slug={page.slug} title={page.title} />}
        {page.name === "studio" && <Studio slug={page.slug} title={page.title} />}
        {page.name === "viewer" && (
          <Viewer slug={page.slug} title={page.title} url={page.url} server={page.server} />
        )}
      </main>

      {/* The camera panel stays MOUNTED across pages (its stream and tracker keep running while
          you glance at the Menu or Settings) and simply hides off the Console, where the AR
          surface belongs. Keyed by session id so a re-open (new id) always remounts and can
          never 'stick' on a stale stream. */}
      {camera && <CameraDock key={camera.id} session={camera} hidden={!onConsole} />}
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

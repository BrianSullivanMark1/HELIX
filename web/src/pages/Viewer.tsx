// The embedded build viewer — an iframe over the app's own page (static or its local server),
// with the live-edit bar ("describe a change — HELIX updates this live").
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

export default function Viewer({
  slug, title, url, server,
}: { slug: string; title: string; url: string; server?: boolean }) {
  const navigate = useHelix((s) => s.navigate);
  const buildsVersion = useHelix((s) => s.buildsVersion);
  const [editOpen, setEditOpen] = useState(false);
  const [change, setChange] = useState("");
  const [status, setStatus] = useState("");
  const [ready, setReady] = useState(!server);
  const frame = useRef<HTMLIFrameElement>(null);
  const tries = useRef(0);

  // A backend app needs a moment to bind its port — poll before showing the frame.
  useEffect(() => {
    if (!server) return;
    setReady(false);
    tries.current = 0;
    const poll = window.setInterval(() => {
      tries.current += 1;
      void fetch(url, { mode: "no-cors" })
        .then(() => {
          window.clearInterval(poll);
          setReady(true);
        })
        .catch(() => {
          if (tries.current > 40) {
            window.clearInterval(poll);
            setStatus("It’s taking too long to start — try Reload.");
          }
        });
    }, 150);
    return () => window.clearInterval(poll);
  }, [url, server]);

  useEffect(() => {
    // A finished live-edit republishes the workspace — reload the page to show it.
    if (frame.current && ready) frame.current.src = frame.current.src;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildsVersion]);

  const back = () => {
    if (server) void api.post(`/api/builds/${slug}/stop`);
    navigate({ name: "menu" });
  };

  const submitEdit = () => {
    if (!change.trim()) return;
    void api.post(`/api/builds/${slug}/edit`, { change: change.trim() });
    setStatus("Updating live…");
    setChange("");
  };

  return (
    <div className="h-full pt-14 px-6 pb-4 flex flex-col" style={{ pointerEvents: "auto" }}>
      <div className="flex items-center gap-3 mb-2">
        <button className="btn-nav" onClick={back}>← Back</button>
        <span className="font-semibold" style={{ color: "var(--cyan)" }}>{title}</span>
        <span className="text-xs elide" style={{ color: "var(--muted)" }}>{status}</span>
        <div className="flex-1" />
        <button className="btn-nav" onClick={() => setEditOpen((v) => !v)}>✨ Edit</button>
        <button className="btn-nav" onClick={() => frame.current && (frame.current.src = frame.current.src)}>⟳</button>
        <a className="btn-nav" href={url} target="_blank" rel="noreferrer">↗</a>
      </div>
      <div className="flex-1 rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--line)", background: "#0a0e14" }}>
        {ready ? (
          <iframe ref={frame} src={url} title={title} className="w-full h-full"
            style={{ border: "none", background: "#0a0e14" }} />
        ) : (
          <div className="h-full flex items-center justify-center text-sm"
            style={{ color: "var(--muted)" }}>
            Starting {title}…
          </div>
        )}
      </div>
      {editOpen && (
        <div className="flex gap-2 mt-2">
          <input
            className="flex-1"
            placeholder="Describe a change — HELIX updates this live…"
            value={change}
            onChange={(e) => setChange(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submitEdit()}
          />
          <button className="btn btn-primary" onClick={submitEdit}>✨ Update</button>
        </div>
      )}
    </div>
  );
}

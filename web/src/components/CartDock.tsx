// The Amazon cart panel — the staged list, live. Every line the model stages shows here at once
// (picture, name, quantity with ± controls, the price HELIX read off the listing), with the running
// estimate and two buttons: "Hand to Amazon" (HELIX's own Chrome window adds each item at its
// quantity and opens Amazon's cart — nothing is bought) and "What's in Amazon's cart?" (reads the
// real cart back). The panel appears whenever something is staged and folds to a chip when you
// collapse it. Backend-driven: the server pushes a `cart` event on every change; the panel only
// sends gestures. Product titles are untrusted page text and render as text.
import { useState } from "react";
import { api } from "../lib/api";
import { useHelix, type CartSnapshot } from "../lib/store";

function money(v: number | null | undefined): string {
  return typeof v === "number" ? `$${v.toFixed(2)}` : "—";
}

export default function CartDock({ cart, hidden = false }: { cart: CartSnapshot; hidden?: boolean }) {
  const camera = useHelix((s) => s.camera);
  const cameraLayout = useHelix((s) => s.cameraLayout);
  const [folded, setFolded] = useState(false);
  const [busy, setBusy] = useState<string>("");
  const items = cart.items || [];
  const count = items.reduce((n, i) => n + (i.quantity || 0), 0);
  // The camera dock owns the right edge while it's up; the cart sits bottom-left then.
  const camDocked = Boolean(camera) && cameraLayout === "dock";
  if (hidden || items.length === 0) return null;

  const post = async (path: string, body?: unknown, label = "") => {
    setBusy(label || path);
    try {
      await api.post(path, body);
    } catch {
      /* the event stream tells the truth; a failed gesture just doesn't change it */
    } finally {
      setBusy("");
    }
  };

  const anchor: React.CSSProperties = camDocked
    ? { position: "fixed", left: 18, bottom: 128, zIndex: 25 }
    : { position: "fixed", right: 18, bottom: 128, zIndex: 25 };

  if (folded) {
    return (
      <button
        className="glass rounded-full px-4 py-2 text-[13px] flex items-center gap-2"
        style={{ ...anchor, pointerEvents: "auto", color: "var(--cyan)", border: "1px solid var(--cyan-dim)" }}
        onClick={() => setFolded(false)}
        title="Show the staged Amazon cart"
      >
        🛒 {count} staged{cart.estimated_total != null ? ` · ~${money(cart.estimated_total)}` : ""}
      </button>
    );
  }

  return (
    <div
      className="glass rounded-2xl fade-up flex flex-col"
      style={{
        ...anchor,
        width: 372, maxWidth: "calc(100vw - 36px)", maxHeight: "min(52vh, 560px)",
        pointerEvents: "auto", border: "1px solid var(--cyan-dim)",
      }}
    >
      <div className="flex items-center gap-2 px-4 pt-3 pb-2">
        <span className="text-[13px] font-semibold" style={{ color: "var(--cyan)" }}>🛒 Amazon cart</span>
        <span className="text-xs" style={{ color: "var(--muted)" }}>
          {items.length} product{items.length !== 1 ? "s" : ""} · {count} item{count !== 1 ? "s" : ""}
        </span>
        <div className="flex-1" />
        <button className="btn-nav text-xs" title="Collapse" onClick={() => setFolded(true)}>—</button>
      </div>

      <div className="overflow-y-auto px-3 flex flex-col gap-1.5" style={{ minHeight: 0 }}>
        {items.map((it) => (
          <div key={it.asin} className="flex gap-2.5 items-center rounded-xl px-2 py-1.5"
            style={{ background: "rgba(13,20,27,0.6)", border: "1px solid var(--line)" }}>
            {it.image ? (
              <img src={it.image} alt="" referrerPolicy="no-referrer"
                style={{ width: 44, height: 44, objectFit: "contain", borderRadius: 8, background: "#fff" }} />
            ) : (
              <div style={{ width: 44, height: 44, borderRadius: 8, background: "var(--panel-hi)" }} />
            )}
            <div className="flex-1 min-w-0">
              <a href={it.url} target="_blank" rel="noreferrer" className="text-[13px] elide block"
                style={{ color: "var(--text)" }} title={it.title || it.label}>
                {it.label || it.title}
              </a>
              <div className="text-xs elide" style={{ color: it.note ? "var(--amber)" : "var(--muted)" }}>
                {it.note ? it.note : `${money(it.price)} each${it.project ? ` · ${it.project}` : ""} · ${it.asin}`}
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button className="btn text-xs px-2 py-0.5" title="One fewer"
                onClick={() => void post("/api/cart/quantity", { asin: it.asin, quantity: it.quantity - 1 })}>−</button>
              <span className="text-[13px] w-6 text-center" style={{ color: "var(--text)" }}>{it.quantity}</span>
              <button className="btn text-xs px-2 py-0.5" title="One more"
                onClick={() => void post("/api/cart/quantity", { asin: it.asin, quantity: it.quantity + 1 })}>+</button>
              <button className="btn-nav text-xs" title="Remove"
                onClick={() => void post("/api/cart/remove", { asin: it.asin })}>✕</button>
            </div>
          </div>
        ))}
      </div>

      <div className="px-4 pt-2 pb-3 flex flex-col gap-2">
        <div className="text-xs" style={{ color: "var(--muted)" }}>
          {cart.estimated_total != null
            ? `About ${money(cart.estimated_total)}${cart.unpriced ? ` + ${cart.unpriced} unpriced` : ""} — Amazon's cart is the live truth.`
            : "No prices read yet."}
        </div>
        <div className="flex gap-2 items-center">
          <button className="btn btn-primary text-xs" disabled={Boolean(busy) || cart.opening}
            title={cart.driver
              ? "HELIX's own browser window adds each item at its quantity and opens Amazon's cart. Nothing is bought."
              : "Opens Amazon's add-to-cart link in your browser (Amazon may ask you to sign in first)."}
            onClick={() => void post("/api/cart/open", undefined, "open")}>
            {busy === "open" || cart.opening ? "Handing to Amazon…" : "Hand to Amazon"}
          </button>
          {cart.driver && (
            <button className="btn text-xs" disabled={Boolean(busy)} title="Read what Amazon's own cart holds right now"
              onClick={() => void post("/api/cart/check", undefined, "check")}>
              {busy === "check" ? "Reading…" : "Amazon's cart?"}
            </button>
          )}
          <div className="flex-1" />
          <button className="btn-nav text-xs" title="Clear the staged list"
            onClick={() => void post("/api/cart/clear")}>Clear</button>
        </div>
      </div>
    </div>
  );
}

// The Vault manager — notes, files, search, and the stored documents (newest first).
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

interface Doc { id: string; title: string; source: string; size: number }
interface Hit { title: string; text: string; score: number }

function fmtSize(n: number): string {
  return n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`;
}

export default function Vault({ slug, title }: { slug: string; title: string }) {
  const navigate = useHelix((s) => s.navigate);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [name, setName] = useState(title);
  const [note, setNote] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [openDoc, setOpenDoc] = useState<{ id: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const debounce = useRef(0);
  const seq = useRef(0);

  const load = useCallback(() => {
    void api.get<{ name: string; docs: Doc[] }>(`/api/vault/${slug}`).then((res) => {
      setName(res.name);
      setDocs(res.docs);
    });
  }, [slug]);
  useEffect(load, [load]);

  const saveNote = () => {
    if (!note.trim()) {
      setStatus("Type a note first.");
      return;
    }
    void api.post<{ ok: boolean; title: string }>(`/api/vault/${slug}/note`, { text: note }).then((res) => {
      setStatus(res.ok ? `Saved “${res.title}”.` : "That note was empty.");
      if (res.ok) {
        setNote("");
        load();
      }
    });
  };

  const addFiles = (files: FileList | File[]) => {
    if (busy) return;
    setBusy(true);
    setStatus("Reading…");
    void api.uploadMany(`/api/vault/${slug}/files`, Array.from(files))
      .then((res) => {
        const added = (res as { added: number }).added;
        setStatus(added ? `Added ${added} document${added === 1 ? "" : "s"}.`
          : "Nothing readable to add — text, Markdown, code, PDF, and Word files are supported.");
        load();
      })
      .catch(() => {
        setStatus("Something went wrong partway through reading those. Anything that made it in " +
          "is in the list below — want to try the rest again?");
        load();
      })
      .finally(() => setBusy(false));
  };

  const search = (q: string) => {
    setQuery(q);
    setStatus("");
    window.clearTimeout(debounce.current);
    const mySeq = ++seq.current;
    if (!q.trim()) {
      setHits(null);
      return;
    }
    debounce.current = window.setTimeout(() => {
      setStatus("Searching…");
      void api.get<{ hits: Hit[] }>(`/api/vault/${slug}/search?q=${encodeURIComponent(q)}`)
        .then((res) => {
          if (mySeq !== seq.current) return;
          setHits(res.hits);
          setStatus("");
        });
    }, 300);
  };

  const toggleDoc = (id: string) => {
    if (openDoc?.id === id) {
      setOpenDoc(null);
      return;
    }
    void api.get<{ text: string }>(`/api/vault/${slug}/doc/${id}`).then((res) =>
      setOpenDoc({ id, text: res.text }));
  };

  const removeDoc = (doc: Doc) => {
    if (!window.confirm(`Remove “${doc.title}” from this vault? This can’t be undone.`)) return;
    void api.del(`/api/vault/${slug}/doc/${doc.id}`).then(load);
  };

  return (
    <div
      className="h-full overflow-y-auto pt-16 px-8 pb-8"
      style={{ pointerEvents: "auto" }}
      onDrop={(e) => {
        e.preventDefault();
        if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
      }}
      onDragOver={(e) => e.preventDefault()}
    >
      <div className="max-w-[820px] mx-auto">
        <div className="flex items-center gap-3 mb-4">
          <button className="btn-nav" onClick={() => navigate({ name: "menu" })}>← Back</button>
          <span className="font-semibold text-[15px]" style={{ color: "var(--cyan)" }}>
            Vault › {name}
          </span>
          <span className="text-xs" style={{ color: "var(--muted)" }}>
            {hits ? `${hits.length} match${hits.length === 1 ? "" : "es"}` : `${docs.length} documents`}
          </span>
        </div>

        <textarea
          rows={3}
          className="w-full"
          placeholder="Paste or type a note to remember…  (or drop files onto this panel)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex gap-2 mt-2 mb-4 items-center">
          <button className="btn btn-primary text-[13px]" onClick={saveNote} disabled={busy}>
            ＋ Save note
          </button>
          <label className="btn text-[13px]" style={busy ? { opacity: 0.5 } : undefined}>
            📄 Add files
            <input type="file" multiple className="hidden" disabled={busy}
              onChange={(e) => {
                if (e.target.files?.length) addFiles(e.target.files);
                e.target.value = "";
              }} />
          </label>
          <input
            className="flex-1"
            placeholder="🔍 Search this vault…"
            value={query}
            onChange={(e) => search(e.target.value)}
          />
        </div>

        {status && <div className="text-xs mb-3" style={{ color: "var(--muted)" }}>{status}</div>}

        {hits ? (
          <div className="space-y-3">
            {hits.map((h, i) => (
              <div key={i} className="card p-4">
                <div className="text-[13px] font-semibold" style={{ color: "var(--cyan)" }}>{h.title}</div>
                <div className="text-xs mt-1" style={{ color: "var(--muted)", whiteSpace: "pre-wrap" }}>
                  {h.text.slice(0, 500)}
                </div>
              </div>
            ))}
            {hits.length === 0 && (
              <div className="text-sm" style={{ color: "var(--muted)" }}>
                No passages match “{query}”.
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2">
            {docs.map((d) => (
              <div key={d.id} className="card px-4 py-3">
                <div className="flex items-center gap-3">
                  <div className="flex-1 elide">
                    <span className="text-[13px] font-medium" style={{ color: "var(--cyan)" }}>
                      {d.title}
                    </span>
                    <span className="text-xs ml-2" style={{ color: "var(--muted)" }}>
                      {d.source} · {fmtSize(d.size)}
                    </span>
                  </div>
                  <button className="btn-nav text-xs" onClick={() => toggleDoc(d.id)}>
                    {openDoc?.id === d.id ? "Close" : "Open"}
                  </button>
                  <button className="btn-nav text-xs" style={{ color: "var(--error)" }}
                    onClick={() => removeDoc(d)}>
                    ✕
                  </button>
                </div>
                {openDoc?.id === d.id && (
                  <pre className="text-xs mt-2 max-h-[320px] overflow-y-auto"
                    style={{ color: "var(--muted)", whiteSpace: "pre-wrap", fontFamily: "inherit" }}>
                    {openDoc.text}
                  </pre>
                )}
              </div>
            ))}
            {docs.length === 0 && (
              <div className="text-sm py-6" style={{ color: "var(--muted)" }}>
                Nothing saved yet. Paste a note, add files, or drop files onto this panel.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

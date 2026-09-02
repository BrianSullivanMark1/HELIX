// The just-in-time key panel: masked fields, saved on this machine only, never into chat.
// Carries the mis-paste guard: a URL where a key belongs warns once and holds the save; an
// unchanged second Connect means the user insists.
import { useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

interface Modal {
  service: string;
  label: string;
  reason: string;
  fields: { key: string; label: string; hint: string }[];
}

function looksLikeMispaste(value: string): boolean {
  const v = value.trim().toLowerCase();
  return v.startsWith("http://") || v.startsWith("https://") ||
    (/^[a-z0-9.-]+\.[a-z]{2,}\//.test(v) && v.includes("/"));
}

export default function ConnectModal({ modal }: { modal: Modal }) {
  const set = useHelix((s) => s.set);
  const [values, setValues] = useState<Record<string, string>>({});
  const [warned, setWarned] = useState<string>("");
  const [note, setNote] = useState("");

  const close = () => set({ connectModal: null });

  const save = () => {
    const filled = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v.trim()),
    );
    if (!Object.keys(filled).length) {
      setNote("Paste the key first.");
      return;
    }
    const urlExempt = (key: string, label: string) =>
      /url|webhook/i.test(key) || /url|webhook/i.test(label);
    const bad = modal.fields.find(
      (f) => filled[f.key] && !urlExempt(f.key, f.label) && looksLikeMispaste(filled[f.key]),
    );
    const signature = JSON.stringify(filled);
    if (bad && warned !== signature) {
      setWarned(signature);
      setNote(`That looks like a web address, not a ${bad.label}. Connect again to save anyway.`);
      return;
    }
    void api.post(`/api/connect/${modal.service}`, { values: filled }).then(() => close());
  };

  return (
    <div className="fixed inset-0 flex items-center justify-center" style={{ zIndex: 50, background: "rgba(3,6,9,0.7)" }}>
      <div className="glass-hi rounded-2xl p-5 w-[460px] fade-up">
        <div className="font-display text-[15px] mb-1" style={{ color: "var(--cyan)" }}>
          CONNECT {modal.label.toUpperCase()}
        </div>
        {modal.reason && (
          <div className="text-[13px] mb-2" style={{ color: "var(--text)" }}>{modal.reason}</div>
        )}
        <div className="text-xs mb-4" style={{ color: "var(--muted)" }}>
          Paste the key below. It's saved on this machine only — never shown in chat.
        </div>
        {modal.fields.map((f) => (
          <div key={f.key} className="mb-3">
            <div className="text-xs mb-1">
              {f.label} {f.hint && <span style={{ color: "var(--muted)" }}>({f.hint})</span>}
            </div>
            <input
              type="password"
              className="w-full"
              value={values[f.key] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
              onKeyDown={(e) => e.key === "Enter" && save()}
            />
          </div>
        ))}
        {note && <div className="text-xs mb-3" style={{ color: "var(--working)" }}>{note}</div>}
        <div className="flex gap-3 justify-end">
          <button className="btn" onClick={close}>Cancel</button>
          <button className="btn btn-primary" onClick={save}>Connect</button>
        </div>
      </div>
    </div>
  );
}

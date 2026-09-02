// The Menu — Apps / Protocols / Agents / Holograms / Vault cards + the Suggested strip.
import { useCallback, useEffect, useState } from "react";
import { openBuild } from "../App";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

interface Row {
  slug: string;
  name: string;
  request: string;
  kind: string;
  status: string | null;
  needs_keys: boolean;
  missing_keys: boolean;
  docs: number;
}
interface AgentRow { name: string; goal: string; enabled: boolean }
interface MenuData {
  builds: Record<string, Row[]>;
  agents: AgentRow[];
  suggested: { slug: string; name: string; reason: string }[];
}

const TABS = [
  ["Apps", "apps"], ["Protocols", "tasks"], ["Agents", "agents"],
  ["Holograms", "models"], ["Vault", "knowledge"],
] as const;

function borderFor(status: string | null): string {
  if (status === "building") return "var(--working)";
  if (status === "done") return "var(--done)";
  if (status === "error") return "var(--error)";
  return "var(--line)";
}

export default function Menu() {
  const navigate = useHelix((s) => s.navigate);
  const buildsVersion = useHelix((s) => s.buildsVersion);
  const [tab, setTab] = useState<string>("apps");
  const [data, setData] = useState<MenuData | null>(null);
  const [status, setStatus] = useState("");
  const [agentName, setAgentName] = useState("");
  const [agentGoal, setAgentGoal] = useState("");

  const refresh = useCallback(() => {
    void api.get<MenuData>("/api/builds").then(setData).catch(() => undefined);
  }, []);
  useEffect(refresh, [refresh, buildsVersion]);

  const open = (row: Row) => void openBuild(row.slug, row.name, navigate);

  const editBuild = (row: Row) => {
    const change = window.prompt(`Describe the change to “${row.name}” — HELIX updates it live:`);
    if (!change?.trim()) return;
    void api.post(`/api/builds/${row.slug}/edit`, { change: change.trim() })
      .then(() => setStatus(`Updating ${row.name}…`));
  };

  const renameBuild = (row: Row) => {
    const name = window.prompt("New name:", row.name);
    if (!name?.trim() || name.trim() === row.name) return;
    void api.post(`/api/builds/${row.slug}/rename`, { name: name.trim() })
      .then(refresh)
      .catch(() => setStatus(`Couldn’t rename to “${name}”. That name may already be in use, or ` +
        `it’s open or building right now — close it (or wait a moment) and try again.`));
  };

  const removeBuild = (row: Row) => {
    if (!window.confirm(`Remove “${row.name}”? This permanently deletes its files and can’t be undone.`)) return;
    void api.del<{ ok: boolean }>(`/api/builds/${row.slug}`).then((res) => {
      if (!res.ok) setStatus(`Couldn’t remove “${row.name}” — it’s open or running right now. ` +
        `Close it (or wait a moment) and try again.`);
      refresh();
    });
  };

  const runProtocol = (row: Row) => {
    void api.post<{ ok: boolean }>(`/api/builds/${row.slug}/run`).then((res) => {
      setStatus(res.ok ? `Launched “${row.name}” in its own window.`
        : `Couldn’t launch “${row.name}” — it may be missing a runnable main.py.`);
    });
  };

  const showVersions = (row: Row) => {
    void api.get<{ versions: { sha: string; when: string }[] }>(`/api/builds/${row.slug}/versions`)
      .then((res) => {
        if (res.versions.length <= 1) {
          setStatus("No earlier versions yet.");
          return;
        }
        const options = res.versions.slice(1).map((v, i) => `${i + 1}. ${v.when}`).join("\n");
        const pick = window.prompt(
          `Revert “${row.name}” to an earlier version? Your current version is kept in history.\n${options}\n\nEnter a number:`);
        const idx = Number(pick) - 1;
        const target = res.versions.slice(1)[idx];
        if (!target) return;
        void api.post(`/api/builds/${row.slug}/revert`, { sha: target.sha }).then((r) => {
          setStatus((r as { ok: boolean }).ok ? `Reverted “${row.name}”.`
            : `Couldn’t revert “${row.name}” — it may be open or running right now.`);
          refresh();
        });
      });
  };

  const connectBuild = (row: Row) => {
    void api.get<{ connections: { key: string; label: string; hint: string; set: boolean; managed: boolean }[] }>(
      `/api/builds/${row.slug}/connections`).then((res) => {
      const values: Record<string, string> = {};
      for (const conn of res.connections) {
        const already = conn.set ? " (already connected — leave blank to keep)" : "";
        const v = window.prompt(`${conn.label} (${conn.hint})${already}:`, "");
        if (v?.trim()) values[conn.key] = v.trim();
      }
      if (Object.keys(values).length)
        void api.post(`/api/builds/${row.slug}/connections`, { values }).then(refresh);
    });
  };

  const saveAgent = () => {
    if (!agentName.trim() || !agentGoal.trim()) {
      setStatus("Give the agent a name and a goal.");
      return;
    }
    void api.post("/api/agents", { name: agentName.trim(), goal: agentGoal.trim() }).then(() => {
      setStatus(`Saved agent “${agentName.trim()}”.`);
      setAgentName("");
      setAgentGoal("");
      refresh();
    });
  };

  const rows = data?.builds[tab] ?? [];

  return (
    <div className="h-full overflow-y-auto pt-16 px-8 pb-8" style={{ pointerEvents: "auto" }}>
      <div className="max-w-[1000px] mx-auto">
        {(data?.suggested?.length ?? 0) > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-3">
            {data!.suggested.map((s) => (
              <button key={s.slug} className="glass rounded-full px-4 py-1.5 text-xs shrink-0 elide max-w-[260px]"
                title={`Open ${s.name}`}
                onClick={() => void openBuild(s.slug, s.name, navigate)}>
                {s.name} · <span style={{ color: "var(--muted)" }}>{s.reason}</span>
              </button>
            ))}
          </div>
        )}

        <div className="flex items-center gap-1 mb-5">
          {TABS.map(([label, key]) => (
            <button key={key}
              className={key === tab ? "btn btn-primary" : "btn-nav"}
              onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
          <div className="flex-1" />
          <button className="btn btn-primary" onClick={() => navigate({ name: "console" })}>
            ＋ New
          </button>
        </div>

        {status && <div className="text-xs mb-3" style={{ color: "var(--muted)" }}>{status}</div>}

        {tab === "agents" ? (
          <div>
            <div className="flex gap-2 mb-4">
              <input value={agentName} placeholder="Agent name" className="max-w-[220px]"
                onChange={(e) => setAgentName(e.target.value)} />
              <input value={agentGoal} placeholder="Goal" className="flex-1"
                onChange={(e) => setAgentGoal(e.target.value)} />
              <button className="btn btn-primary" onClick={saveAgent}>＋ Add agent</button>
            </div>
            <div className="grid grid-cols-2 gap-4">
              {(data?.agents ?? []).map((a) => (
                <div key={a.name} className="card p-4">
                  <div className="font-semibold text-[15px]" style={{ color: "var(--cyan)" }}>{a.name}</div>
                  <div className="text-xs mt-1 line-clamp-3" style={{ color: "var(--muted)" }}>{a.goal}</div>
                  <div className="flex gap-2 mt-3">
                    <button className="btn text-xs" onClick={() => {
                      setStatus(`Running “${a.name}”…`);
                      void api.post(`/api/agents/${encodeURIComponent(a.name)}/run`);
                    }}>▶ Run</button>
                    <button className="btn text-xs" onClick={() => {
                      const name = window.prompt("New name:", a.name);
                      if (name?.trim() && name.trim() !== a.name)
                        void api.post(`/api/agents/${encodeURIComponent(a.name)}/rename`, { name: name.trim() }).then(refresh);
                    }}>✎</button>
                    <button className="btn btn-danger text-xs" onClick={() => {
                      if (window.confirm(`Remove the agent “${a.name}”? This can’t be undone.`))
                        void api.del(`/api/agents/${encodeURIComponent(a.name)}`).then(refresh);
                    }}>✕</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            {rows.map((row) => (
              <div key={row.slug} className="card p-4"
                style={{ borderColor: borderFor(row.status), borderWidth: row.status ? 2 : 1 }}>
                <div className="font-semibold text-[15px] elide" style={{ color: "var(--cyan)" }}>
                  {row.name}
                </div>
                <div className="text-xs mt-1 line-clamp-2" style={{ color: "var(--muted)" }}>
                  {tab === "knowledge" ? `${row.docs} documents · searchable by the orb` : row.request}
                </div>
                <div className="flex gap-2 mt-3 flex-wrap">
                  {tab === "tasks" ? (
                    <button className="btn btn-primary text-xs" onClick={() => runProtocol(row)}>▶ Run</button>
                  ) : (
                    <button className="btn btn-primary text-xs" onClick={() => open(row)}>Open</button>
                  )}
                  {tab !== "knowledge" && (
                    <button className="btn text-xs" title="Describe a change" onClick={() => editBuild(row)}>✨ Edit</button>
                  )}
                  {row.needs_keys && (
                    <button className="btn text-xs"
                      style={{ color: row.missing_keys ? "var(--working)" : "var(--done)" }}
                      title={row.missing_keys ? "Set the API keys this build needs" : "API keys are set — click to edit"}
                      onClick={() => connectBuild(row)}>
                      🔑 {row.missing_keys ? "Connect" : "Keys set"}
                    </button>
                  )}
                  <button className="btn text-xs" onClick={() => showVersions(row)}>🕘</button>
                  <button className="btn text-xs" onClick={() => renameBuild(row)}>✎</button>
                  <button className="btn btn-danger text-xs" onClick={() => removeBuild(row)}>✕</button>
                </div>
              </div>
            ))}
            {rows.length === 0 && (
              <div className="text-sm py-8" style={{ color: "var(--muted)" }}>
                Nothing here yet — describe what you want on the Console and HELIX builds it.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

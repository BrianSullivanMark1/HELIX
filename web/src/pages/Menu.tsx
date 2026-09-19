// The Menu — Apps / Protocols / Agents / Holograms / Vault cards + the Suggested strip.
// Holograms shelve under PROJECT FOLDERS: a header per folder (rename it there), the loose ones last,
// and a 📁 button on each hologram card to file it, move it, or take it out. The folder is a tag in
// BuildService's sidecar (nothing moves on disk); with nothing filed the tab looks as it always did.
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
  project: string; // the project folder it sits in ('' = loose)
}
interface AgentRow { name: string; goal: string; enabled: boolean }
interface MenuData {
  builds: Record<string, Row[]>;
  agents: AgentRow[];
  suggested: { slug: string; name: string; reason: string }[];
  projects: string[]; // every folder with something in it, A–Z
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

/** BuildService.grouped's order, mirrored: folders A–Z first, then the loose ones under "" — and
 *  exactly one nameless group when nothing is filed, so an unfiled tab renders the plain grid. */
function groupByProject(rows: Row[]): [string, Row[]][] {
  const by = new Map<string, Row[]>();
  for (const row of rows) {
    const key = row.project || "";
    const list = by.get(key);
    if (list) list.push(row); else by.set(key, [row]);
  }
  const folders = [...by.keys()].filter(Boolean)
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  const out: [string, Row[]][] = folders.map((f) => [f, by.get(f) ?? []]);
  const loose = by.get("") ?? [];
  if (loose.length || !out.length) out.push(["", loose]);
  return out;
}

export default function Menu({ tab: tabProp, embedded = false }: { tab?: string; embedded?: boolean } = {}) {
  const navigate = useHelix((s) => s.navigate);
  const buildsVersion = useHelix((s) => s.buildsVersion);
  const [tabState, setTab] = useState<string>("apps");
  const tab = tabProp ?? tabState; // embedded in the Console, the Console owns the tab
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

  const fileBuild = (row: Row) => {
    const known = data?.projects ?? [];
    const hint = known.length ? `\nFolders so far: ${known.join(", ")}` : "";
    const project = window.prompt(
      `Project folder for “${row.name}” — an existing folder or a new name; leave blank to take it out.${hint}`,
      row.project || "");
    if (project === null || project.trim() === (row.project || "")) return;
    void api.post<{ ok: boolean; project: string }>(`/api/builds/${row.slug}/project`, { project: project.trim() })
      .then((res) => {
        setStatus(res.project ? `Filed “${row.name}” under “${res.project}”.`
          : `Took “${row.name}” out of its folder.`);
        refresh();
      })
      .catch(() => setStatus(`Couldn’t file “${row.name}” — it may have just been removed.`));
  };

  const renameProject = (project: string) => {
    const name = window.prompt("New folder name (every hologram in it moves with it):", project);
    if (!name?.trim() || name.trim() === project) return;
    void api.post<{ ok: boolean; moved: number }>("/api/projects/rename", { project, name: name.trim() })
      .then((res) => {
        if (!res.ok) setStatus(`Couldn’t rename the folder “${project}”.`);
        refresh();
      })
      .catch(() => setStatus(`Couldn’t rename the folder “${project}”.`));
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
  const groups: [string, Row[]][] = tab === "models" ? groupByProject(rows) : [["", rows]];
  const shelved = groups.some(([folder]) => Boolean(folder)); // headers only once a folder exists

  const card = (row: Row) => (
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
        {tab === "models" && (
          <button className="btn text-xs"
            title={row.project ? `In “${row.project}” — move it to another project folder or take it out`
              : "Put this hologram in a project folder"}
            onClick={() => fileBuild(row)}>📁</button>
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
  );

  return (
    <div className={embedded ? "" : "h-full overflow-y-auto pt-16 px-8 pb-8"} style={{ pointerEvents: "auto" }}>
      <div className={embedded ? "" : "max-w-[1000px] mx-auto"}>
        {!embedded && (data?.suggested?.length ?? 0) > 0 && (
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

        {!embedded && <div className="flex items-center gap-1 mb-5">
          {TABS.map(([label, key]) => (
            <button key={key}
              className={key === tab ? "btn btn-primary" : "btn-nav"}
              onClick={() => setTab(key)}>
              {label}
            </button>
          ))}
          <div className="flex-1" />
          <button className="btn" title="What HELIX found, verified, tried and applied overnight"
            onClick={() => navigate({ name: "dream" })}>
            ◐ Dream journal
          </button>
          <button className="btn btn-primary" onClick={() => navigate({ name: "talk" })}>
            ＋ New
          </button>
        </div>}

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
          <div>
            {groups.map(([folder, list]) => (
              <div key={folder || "·loose"} className={shelved ? "mb-5" : ""}>
                {shelved && (
                  <div className="flex items-center gap-2 mb-2 text-sm font-semibold"
                    style={{ color: folder ? "var(--cyan)" : "var(--muted)" }}>
                    <span>{folder ? `📁 ${folder} · ${list.length}` : "Not in a folder"}</span>
                    {folder && (
                      <button className="btn text-xs" title="Rename this folder — every hologram in it moves with it"
                        onClick={() => renameProject(folder)}>✎</button>
                    )}
                  </div>
                )}
                <div className="grid grid-cols-2 gap-4">
                  {list.map(card)}
                </div>
              </div>
            ))}
            {rows.length === 0 && (
              <div className="text-sm py-8" style={{ color: "var(--muted)" }}>
                Nothing here yet — open the orb (Dev on a card, or the orb bottom-right) and describe what you want; HELIX builds it.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

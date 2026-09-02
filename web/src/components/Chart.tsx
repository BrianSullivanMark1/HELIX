// Inline visuals for ```viz blocks: charts (bar/column/line/area/pie/donut) and tables — SVG/HTML,
// HELIX-palette, grow-in animated. Data is untrusted: everything renders as text, never as markup.
import { useEffect, useState } from "react";
import type { Visual } from "../lib/store";

const SERIES = ["#3fe0e0", "#f5a623", "#78c8ff", "#2ec496", "#c878ff", "#ff7896", "#96dc78"];

interface Datum {
  label: string;
  value: number;
}

function data24(spec: Visual): Datum[] {
  const raw = (spec.data as { label?: unknown; value?: unknown }[]) || [];
  return raw.slice(0, 24).map((d) => ({
    label: String(d.label ?? ""),
    value: Number(d.value ?? 0) || 0,
  }));
}

function useGrow(): number {
  const [t, setT] = useState(0);
  useEffect(() => {
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const x = Math.min(1, (now - start) / 640);
      setT(1 - Math.pow(1 - x, 3));
      if (x < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return t;
}

function Bars({ spec, vertical }: { spec: Visual; vertical: boolean }) {
  const data = data24(spec);
  const t = useGrow();
  const max = Math.max(...data.map((d) => Math.abs(d.value)), 1e-9);
  if (vertical) {
    const w = Math.max(280, data.length * 44);
    return (
      <svg width={w} height={200} role="img">
        {data.map((d, i) => {
          const h = (Math.abs(d.value) / max) * 140 * t;
          const x = 16 + i * 44;
          return (
            <g key={i}>
              <rect x={x} y={160 - h} width={28} height={h} rx={4} fill={SERIES[0]} opacity={0.85} />
              <text x={x + 14} y={156 - h} textAnchor="middle" fontSize={11} fill="var(--text)">
                {d.value}
              </text>
              <text x={x + 14} y={178} textAnchor="middle" fontSize={10} fill="var(--muted)">
                {d.label.slice(0, 8)}
              </text>
            </g>
          );
        })}
      </svg>
    );
  }
  return (
    <svg width={460} height={data.length * 28 + 8} role="img">
      {data.map((d, i) => {
        const bw = (Math.abs(d.value) / max) * 300 * t;
        const y = 4 + i * 28;
        return (
          <g key={i}>
            <text x={104} y={y + 15} textAnchor="end" fontSize={11} fill="var(--muted)">
              {d.label.slice(0, 16)}
            </text>
            <rect x={112} y={y + 3} width={bw} height={18} rx={4} fill={SERIES[0]} opacity={0.85} />
            <text x={118 + bw} y={y + 16} fontSize={11} fill="var(--text)">
              {d.value}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function Line({ spec, area }: { spec: Visual; area: boolean }) {
  const data = data24(spec);
  const t = useGrow();
  if (data.length < 2) return <Bars spec={spec} vertical={false} />;
  const w = 460;
  const h = 190;
  const max = Math.max(...data.map((d) => d.value), 1e-9);
  const min = Math.min(...data.map((d) => d.value), 0);
  const px = (i: number) => 24 + (i / (data.length - 1)) * (w - 48);
  const py = (v: number) => 16 + (1 - ((v - min) / (max - min || 1)) * t) * (h - 56);
  const path = data.map((d, i) => `${i ? "L" : "M"}${px(i)},${py(d.value)}`).join(" ");
  return (
    <svg width={w} height={h} role="img">
      {[0.25, 0.5, 0.75, 1].map((g) => (
        <line key={g} x1={24} x2={w - 24} y1={16 + (1 - g) * (h - 56)} y2={16 + (1 - g) * (h - 56)}
          stroke="var(--line)" strokeWidth={1} />
      ))}
      {area && (
        <path d={`${path} L${px(data.length - 1)},${h - 40} L${px(0)},${h - 40} Z`}
          fill={SERIES[0]} opacity={0.14} />
      )}
      <path d={path} fill="none" stroke={SERIES[0]} strokeWidth={5} opacity={0.25} />
      <path d={path} fill="none" stroke={SERIES[0]} strokeWidth={2} />
      {data.map((d, i) => (
        <circle key={i} cx={px(i)} cy={py(d.value)} r={2.6} fill={SERIES[0]} />
      ))}
      {data.map((d, i) =>
        data.length <= 8 || i % 2 === 0 ? (
          <text key={i} x={px(i)} y={h - 22} textAnchor="middle" fontSize={10} fill="var(--muted)">
            {d.label.slice(0, 7)}
          </text>
        ) : null,
      )}
    </svg>
  );
}

function Pie({ spec, donut }: { spec: Visual; donut: boolean }) {
  const data = data24(spec).filter((d) => d.value > 0);
  const t = useGrow();
  const total = data.reduce((a, d) => a + d.value, 0) || 1;
  const R = 78;
  let angle = -Math.PI / 2;
  const slices = data.map((d, i) => {
    const sweep = (d.value / total) * Math.PI * 2 * t;
    const a0 = angle;
    angle += sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const x0 = 100 + R * Math.cos(a0);
    const y0 = 100 + R * Math.sin(a0);
    const x1 = 100 + R * Math.cos(a0 + sweep);
    const y1 = 100 + R * Math.sin(a0 + sweep);
    return (
      <path
        key={i}
        d={`M100,100 L${x0},${y0} A${R},${R} 0 ${large} 1 ${x1},${y1} Z`}
        fill={SERIES[i % SERIES.length]}
        stroke="var(--bg)"
        strokeWidth={1}
      />
    );
  });
  return (
    <div className="flex items-center gap-4">
      <svg width={200} height={200} role="img">
        {slices}
        {donut && <circle cx={100} cy={100} r={R * 0.52} fill="var(--bg)" />}
      </svg>
      <div className="text-xs space-y-1">
        {data.slice(0, 8).map((d, i) => (
          <div key={i} className="flex items-center gap-2">
            <span className="inline-block w-2.5 h-2.5 rounded-sm"
              style={{ background: SERIES[i % SERIES.length] }} />
            <span style={{ color: "var(--muted)" }}>
              {d.label} — {Math.round((d.value / total) * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function TableViz({ spec }: { spec: Visual }) {
  const cols = ((spec.columns as unknown[]) || []).map(String);
  const rows = ((spec.rows as unknown[][]) || []).map((r) => (r || []).map(String));
  const numeric = (v: string) => /^-?[\d,.%$]+$/.test(v.trim());
  const copy = () => {
    const text = [cols.join(" | "), ...rows.map((r) => r.join(" | "))].join("\n");
    void navigator.clipboard.writeText(text);
  };
  return (
    <div className="max-w-[860px] overflow-x-auto">
      <table className="border-collapse text-[13px]">
        <thead>
          <tr>
            {cols.map((cell, i) => (
              <th key={i} className="text-left px-3 py-1.5 font-semibold"
                style={{ color: "var(--cyan)", borderBottom: "1px solid var(--cyan-dim)" }}>
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri} style={ri % 2 ? { background: "rgba(63,224,224,0.05)" } : undefined}>
              {row.map((cell, ci) => (
                <td key={ci} className="px-3 py-1.5"
                  style={{ textAlign: numeric(cell) ? "right" : "left" }}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn-nav text-xs mt-1" onClick={copy}>⧉ Copy</button>
    </div>
  );
}

export default function VisualBlock({ spec }: { spec: Visual }) {
  const title = typeof spec.title === "string" ? spec.title : "";
  let body: React.ReactNode;
  if (spec.type === "table") body = <TableViz spec={spec} />;
  else {
    const kind = String(spec.kind || "bar");
    if (kind === "line") body = <Line spec={spec} area={false} />;
    else if (kind === "area") body = <Line spec={spec} area />;
    else if (kind === "pie") body = <Pie spec={spec} donut={false} />;
    else if (kind === "donut") body = <Pie spec={spec} donut />;
    else body = <Bars spec={spec} vertical={kind === "column"} />;
  }
  return (
    <div className="glass rounded-xl px-4 py-3 fade-up" style={{ borderColor: "var(--cyan-dim)" }}>
      {title && (
        <div className="text-xs font-semibold mb-2" style={{ color: "var(--cyan)" }}>
          {title}
          {typeof spec.unit === "string" && spec.unit ? (
            <span style={{ color: "var(--muted)" }}> · {spec.unit}</span>
          ) : null}
        </div>
      )}
      {body}
    </div>
  );
}

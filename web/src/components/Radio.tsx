// HELIX RADIO — the company's music, from the bucket, playable from every page. One player lives
// outside React (a single <video> element) so a song keeps playing while you move between the
// Console, Talk and Settings. The deck slides down from the header button: now playing, the
// queue, shuffle, volume, the upload zone (theme + bpm typed by whoever uploads), the station
// name, and the video window for music videos (or the dancer, coming next). Hidden never deleted.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, tokenUrl, STALE_BACKEND } from "../lib/api";
import { SCENES, loadLook, saveLook, type Look } from "./Backdrop";
import "./radio.css";

export interface Track { id: string; title: string; artist: string; theme: string; bpm: number | null; kind: "audio" | "video"; mime: string; audio: string | null; video: string | null; uploaded_by: string; at: string }
interface Deck { bucket: string | null; ok: boolean; problem: string | null; station: string; stations: Record<string, string>; tracks: Track[]; count: number }

// ---------------------------------------------------------------- the player (one, for the app)
const media = document.createElement("video");
media.setAttribute("playsinline", "true");
media.preload = "auto";
media.style.cssText = "width:100%;height:100%;object-fit:contain;background:#000;display:block;";
let ctx: AudioContext | null = null, analyser: AnalyserNode | null = null, bins: Uint8Array | null = null;
function ensureAnalyser() {
  if (analyser) return;
  try {
    ctx = new AudioContext();
    const src = ctx.createMediaElementSource(media);
    analyser = ctx.createAnalyser(); analyser.fftSize = 256; analyser.smoothingTimeConstant = 0.72;
    src.connect(analyser); analyser.connect(ctx.destination);
    bins = new Uint8Array(analyser.frequencyBinCount);
  } catch { analyser = null; }
}
/** The beat, for anything that wants to dance: 0..1 low-band energy, a kick flag on the onset
 *  (energy jumping above its recent average), the raw bins, the theme. Read once per frame by
 *  everyone; the analysis is cached for the frame so ten readers cost one FFT. */
let beatFrame = -1;
let beatCache = { level: 0, kick: false, bins: null as Uint8Array | null, playing: false, theme: "" };
let energyAvg = 0, lastKick = 0;
export function radioBeat(): { level: number; kick: boolean; bins: Uint8Array | null; playing: boolean; theme: string } {
  const now = performance.now();
  if (now - beatFrame < 8) return beatCache;
  beatFrame = now;
  if (!analyser || !bins || media.paused) { beatCache = { level: 0, kick: false, bins: null, playing: false, theme: state.track?.theme || "" }; return beatCache; }
  analyser.getByteFrequencyData(bins as Uint8Array<ArrayBuffer>);
  let sum = 0; for (let i = 1; i < 12; i++) sum += bins[i];
  const level = Math.min(1, sum / 11 / 190);
  energyAvg = energyAvg * 0.96 + level * 0.04;
  const kick = level > 0.28 && level > energyAvg * 1.35 && now - lastKick > 220;
  if (kick) lastKick = now;
  beatCache = { level, kick, bins, playing: true, theme: state.track?.theme || "" };
  return beatCache;
}
// The element lives in the document for the whole session, in a hidden host: a media element that
// is REMOVED from the document pauses itself (that is the spec), which is why the radio used to
// stop when the deck closed. The deck borrows it into the video window and hands it back.
const parking = document.createElement("div");
parking.setAttribute("aria-hidden", "true");
parking.style.cssText = "position:fixed;width:1px;height:1px;left:-9999px;top:-9999px;overflow:hidden;";
parking.appendChild(media);
document.body.appendChild(parking);

interface PlayerState { track: Track | null; playing: boolean; queue: Track[]; shuffle: boolean; volume: number; showVideo: boolean; t: number; dur: number }
const listeners = new Set<() => void>();
const state: PlayerState = { track: null, playing: false, queue: [], shuffle: false, volume: 0.8, showVideo: true, t: 0, dur: 0 };
try { const v = localStorage.getItem("helix_radio"); if (v) { const s = JSON.parse(v); state.volume = s.volume ?? 0.8; state.shuffle = !!s.shuffle; state.showVideo = s.showVideo ?? true; } } catch { /* no storage */ }
const emit = () => { listeners.forEach((l) => l()); try { localStorage.setItem("helix_radio", JSON.stringify({ volume: state.volume, shuffle: state.shuffle, showVideo: state.showVideo })); } catch { /* no storage */ } };
media.volume = state.volume;
media.addEventListener("timeupdate", () => { state.t = media.currentTime; state.dur = media.duration || 0; emit(); });
media.addEventListener("play", () => { state.playing = true; emit(); });
media.addEventListener("pause", () => { state.playing = false; emit(); });
media.addEventListener("ended", () => { next(); });

export function play(track: Track, queue?: Track[]) {
  if (queue) state.queue = queue;
  state.track = track;
  ensureAnalyser(); void ctx?.resume();
  media.src = tokenUrl(`/api/radio/play/${track.id}`);
  void media.play().catch(() => undefined);
  emit();
}
export function toggle() { if (!state.track) return; if (media.paused) { ensureAnalyser(); void ctx?.resume(); void media.play().catch(() => undefined); } else media.pause(); }
export function next() {
  const q = state.queue; if (!q.length) return;
  const i = state.track ? q.findIndex((t) => t.id === state.track!.id) : -1;
  const n = state.shuffle ? q[Math.floor(Math.random() * q.length)] : q[(i + 1) % q.length];
  play(n);
}
export function prev() {
  const q = state.queue; if (!q.length) return;
  if (media.currentTime > 4) { media.currentTime = 0; return; }
  const i = state.track ? q.findIndex((t) => t.id === state.track!.id) : 0;
  play(q[(i - 1 + q.length) % q.length]);
}
function usePlayer() {
  const [, force] = useState(0);
  useEffect(() => { const l = () => force((n) => n + 1); listeners.add(l); return () => { listeners.delete(l); }; }, []);
  return state;
}
const fmt = (s: number) => (isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00");
const THEMES = ["chill", "hype", "dark", "happy", "focus", "epic"];

// ---------------------------------------------------------------- the header button
export function RadioButton({ onClick, open }: { onClick: () => void; open: boolean }) {
  const p = usePlayer();
  const bars = useRef<HTMLSpanElement[]>([]);
  useEffect(() => {
    let raf = 0;
    const step = () => {
      const { bins: b } = radioBeat();
      bars.current.forEach((el, i) => { if (!el) return; const v = b && p.playing ? b[2 + i * 5] / 255 : 0.12; el.style.transform = `scaleY(${0.15 + v * 0.85})`; });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [p.playing]);
  return (
    <button className={`btn-nav radio-btn${p.playing ? " on" : ""}${open ? " open" : ""}`} onClick={onClick} title={p.track ? `${p.track.title}${p.track.artist ? " - " + p.track.artist : ""}` : "HELIX RADIO"}>
      <span className="radio-bars" aria-hidden="true">{[0, 1, 2, 3].map((i) => <span key={i} ref={(el) => { if (el) bars.current[i] = el; }} />)}</span>
      <span className="radio-word">RADIO</span>
    </button>
  );
}

// ---------------------------------------------------------------- the deck
export function RadioDeck({ open, onClose, appKey = "default" }: { open: boolean; onClose: () => void; appKey?: string }) {
  const p = usePlayer();
  const [deck, setDeck] = useState<Deck | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [bucketEdit, setBucketEdit] = useState("");
  const [stationEdit, setStationEdit] = useState<string | null>(null);
  const [tab, setTab] = useState<"tracks" | "upload" | "settings">("tracks");
  const [look, setLookState] = useState<Look>(() => loadLook());
  const setLook = (patch: Partial<Look>) => { const next = { ...look, ...patch }; setLookState(next); saveLook(next); };
  const video = useRef<HTMLDivElement | null>(null);
  const load = useCallback(() => {
    void api.get<Deck>(`/api/radio?app_key=${encodeURIComponent(appKey)}`).then((d) => { setDeck(d); setErr(null); }).catch((e: Error) => setErr(e.message));
  }, [appKey]);
  useEffect(() => { if (open) load(); }, [open, load]);
  // the one <video> element is borrowed by the open deck, and PARKED (never detached) after
  useEffect(() => {
    const host = video.current;
    if (!host || !open) return;
    host.appendChild(media);
    return () => { parking.appendChild(media); };
  }, [open, p.track?.id, p.showVideo]);
  const tracks = useMemo(() => (deck?.tracks ?? []).filter((t) => !q || `${t.title} ${t.artist} ${t.theme} ${t.uploaded_by}`.toLowerCase().includes(q.toLowerCase())), [deck, q]);
  const isVideo = p.track?.kind === "video";
  if (!open) return null;
  return (
    <div className="radio-wrap" onClick={onClose}>
      <div className={`radio-deck${look.layout === "compact" ? " compact" : ""}`} onClick={(e) => e.stopPropagation()}>
        <div className="radio-head">
          <div>
            <div className="radio-kicker">HELIX RADIO{deck?.bucket ? ` · gs://${deck.bucket}` : ""}</div>
            <div className="radio-station">
              {stationEdit === null ? (
                <button className="radio-station-name" title="Rename this station" onClick={() => setStationEdit(deck?.station ?? "")}>{deck?.station ?? "HELIX RADIO"} <small>✎</small></button>
              ) : (
                <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); void api.put("/api/radio/station", { app: appKey, name: stationEdit }).then(() => { setStationEdit(null); load(); }).catch((x: Error) => setErr(x.message)); }}>
                  <input autoFocus value={stationEdit} onChange={(e) => setStationEdit(e.target.value)} maxLength={40} placeholder="Station name" />
                  <button className="btn text-xs" type="submit">Save</button>
                  <button className="btn text-xs" type="button" onClick={() => setStationEdit(null)}>Cancel</button>
                </form>
              )}
            </div>
          </div>
          <div className="flex-1" />
          <div className="radio-tabs">
            {(["tracks", "upload"] as const).map((k) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{k === "tracks" ? `Tracks${deck ? ` · ${deck.count}` : ""}` : "＋ Upload"}</button>)}
            <button className={`radio-gear${tab === "settings" ? " on" : ""}`} title="Radio settings: background, layout, station, bucket" onClick={() => setTab(tab === "settings" ? "tracks" : "settings")}>⚙</button>
          </div>
          <button className="btn text-xs" onClick={onClose}>✕</button>
        </div>

        {err && <div className="radio-problem">{err}</div>}
        {deck && !deck.ok && deck.problem && <div className="radio-problem">{deck.problem}</div>}
        {deck && !deck.bucket && (
          <form className="radio-bucket" onSubmit={(e) => { e.preventDefault(); void api.put("/api/radio/bucket", { bucket: bucketEdit }).then(() => load()).catch((x: Error) => setErr(x.message)); }}>
            <span>The bucket you made:</span>
            <input value={bucketEdit} onChange={(e) => setBucketEdit(e.target.value)} placeholder="helix-radio-mark1" />
            <button className="btn btn-primary text-xs" type="submit">Use it</button>
          </form>
        )}

        <div className="radio-body">
          {/* THE STAGE: the video, or the dancer's place */}
          <div className={`radio-stage${isVideo && p.showVideo ? " video" : ""}`}>
            <div ref={video} className="radio-video" style={{ display: isVideo && p.showVideo ? "block" : "none" }} />
            {!(isVideo && p.showVideo) && <Visualizer playing={p.playing} theme={p.track?.theme || ""} />}
            {isVideo && (
              <button className="radio-swap" onClick={() => { state.showVideo = !state.showVideo; emit(); }}>{p.showVideo ? "Show the dancer" : "Show the video"}</button>
            )}
          </div>

          {/* NOW PLAYING + transport */}
          <div className="radio-now">
            <div className="radio-title elide">{p.track ? p.track.title : "Nothing playing"}</div>
            <div className="radio-sub elide">{p.track ? [p.track.artist, p.track.theme, p.track.bpm ? `${p.track.bpm} bpm` : ""].filter(Boolean).join(" · ") || " " : "pick a track, or upload one"}</div>
            <div className="radio-seek" onClick={(e) => { const r = (e.currentTarget as HTMLDivElement).getBoundingClientRect(); if (p.dur) media.currentTime = ((e.clientX - r.left) / r.width) * p.dur; }}>
              <i style={{ width: `${p.dur ? (p.t / p.dur) * 100 : 0}%` }} />
            </div>
            <div className="radio-times"><span>{fmt(p.t)}</span><span>{fmt(p.dur)}</span></div>
            <div className="radio-transport">
              <button className={`radio-ctl${p.shuffle ? " on" : ""}`} title="Shuffle" onClick={() => { state.shuffle = !state.shuffle; emit(); }}>⤨</button>
              <button className="radio-ctl" title="Previous" onClick={prev}>⏮</button>
              <button className="radio-ctl big" title={p.playing ? "Pause" : "Play"} onClick={() => { if (!p.track && tracks.length) play(tracks[0], tracks); else toggle(); }}>{p.playing ? "❚❚" : "▶"}</button>
              <button className="radio-ctl" title="Next" onClick={next}>⏭</button>
              <input className="radio-vol" type="range" min={0} max={1} step={0.01} value={p.volume} title="Volume" onChange={(e) => { state.volume = Number(e.target.value); media.volume = state.volume; emit(); }} />
            </div>
          </div>

          {/* THE LIST / UPLOAD / STATION */}
          <div className="radio-side">
            {tab === "tracks" && (
              <>
                <input className="radio-search" placeholder="Find a track, artist, theme…" value={q} onChange={(e) => setQ(e.target.value)} />
                <div className="radio-list">
                  {deck === null && !err && <div className="radio-muted">Reading the catalog…</div>}
                  {deck && tracks.length === 0 && <div className="radio-muted">{deck.count === 0 ? "No songs yet. Upload the first one." : "Nothing matches."}</div>}
                  {tracks.map((t, i) => (
                    <div key={t.id} className={`radio-row${p.track?.id === t.id ? " on" : ""}`} onDoubleClick={() => play(t, tracks)}>
                      <button className="radio-row-play" onClick={() => (p.track?.id === t.id ? toggle() : play(t, tracks))}>{p.track?.id === t.id && p.playing ? "❚❚" : "▶"}</button>
                      <div className="min-w-0 flex-1">
                        <div className="elide radio-row-title">{t.kind === "video" ? "▣ " : ""}{t.title}</div>
                        <div className="elide radio-row-sub">{[t.artist, t.theme, t.bpm ? `${t.bpm} bpm` : "", t.uploaded_by.split("@")[0]].filter(Boolean).join(" · ")}</div>
                      </div>
                      <span className="radio-row-n">{String(i + 1).padStart(2, "0")}</span>
                      <button className="radio-row-hide" title="Hide from the deck (the file stays in the bucket)" onClick={() => void api.put("/api/radio/hide", { id: t.id }).then(load)}>✕</button>
                    </div>
                  ))}
                </div>
              </>
            )}
            {tab === "upload" && <Upload onDone={() => { setTab("tracks"); load(); }} />}
            {tab === "settings" && (
              <div className="radio-form">
                <div className="radio-kicker">BACKGROUND · behind the whole app while music plays</div>
                <div className="radio-scenes">
                  {SCENES.map((sc) => (
                    <button key={sc.key} className={`radio-scene${!look.surprise && look.scene === sc.key ? " on" : ""}`} title={sc.blurb} onClick={() => setLook({ scene: sc.key, surprise: false })}>{sc.name}</button>
                  ))}
                  <button className={`radio-scene surprise${look.surprise ? " on" : ""}`} title="A different scene for every song, rolled fresh each time" onClick={() => setLook({ surprise: !look.surprise })}>⚄ Surprise me</button>
                </div>
                <label><span>Intensity <small>how hard the pages move to the music</small></span>
                  <input type="range" min={0.2} max={1.5} step={0.05} value={look.intensity} onChange={(e) => setLook({ intensity: Number(e.target.value) })} /></label>
                <div className="radio-kicker mt-2">LAYOUT</div>
                <div className="radio-scenes">
                  <button className={`radio-scene${look.layout === "full" ? " on" : ""}`} onClick={() => setLook({ layout: "full" })}>Full deck</button>
                  <button className={`radio-scene${look.layout === "compact" ? " on" : ""}`} onClick={() => setLook({ layout: "compact" })}>Compact</button>
                </div>
                <div className="radio-kicker mt-2">THIS STATION</div>
                <p className="radio-muted">The name shows on every copy of HELIX and in every app that keys on <b>{appKey}</b>. Each app can carry its own; a local name below wins on this PC only.</p>
                <label><span>Name in the catalog ({appKey})</span><input defaultValue={deck?.stations?.[appKey] ?? ""} placeholder={deck?.stations?.default ?? "HELIX RADIO"} onBlur={(e) => void api.put("/api/radio/station", { app: appKey, name: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <label><span>Local name, this PC only</span><input placeholder="leave empty to use the catalog" onBlur={(e) => void api.put("/api/radio/station", { local: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <div className="radio-kicker mt-3">THE BUCKET</div>
                <label><span>gs://</span><input defaultValue={deck?.bucket ?? ""} placeholder="helix-radio-mark1" onBlur={(e) => void api.put("/api/radio/bucket", { bucket: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <p className="radio-muted">Private bucket, read and written through your own gcloud login. Nothing is ever deleted from here.</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Upload({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState(""); const [artist, setArtist] = useState(""); const [theme, setTheme] = useState("chill"); const [bpm, setBpm] = useState("");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null); const [over, setOver] = useState(false);
  const pick = (f: File | null) => { setFile(f); if (f && !title) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ")); };
  const go = async () => {
    if (!file) return;
    setBusy(true); setErr(null);
    const form = new FormData();
    form.append("file", file, file.name); form.append("title", title); form.append("artist", artist); form.append("theme", theme); form.append("bpm", bpm);
    try {
      const res = await fetch("/api/radio/upload", { method: "POST", headers: { "X-Helix-Token": sessionStorage.getItem("helix_token") || "" }, body: form });
      if (!res.ok) { let d = `${res.status}`; try { d = (await res.json()).error || d; } catch { d = res.status === 405 || res.status === 404 ? STALE_BACKEND : d; } throw new Error(d); }
      onDone();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div className="radio-form">
      <div className={`radio-drop${over ? " over" : ""}${file ? " has" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0] || null); }}>
        <input type="file" accept="audio/*,video/mp4,video/webm,.mp3,.m4a,.aac,.ogg,.wav,.flac,.mp4,.webm" onChange={(e) => pick(e.target.files?.[0] || null)} />
        <div className="radio-drop-ic">{file ? (file.type.startsWith("video") ? "▣" : "♫") : "⇪"}</div>
        <div>{file ? file.name : "Drop a song or a music video here, or click to pick one"}</div>
        <div className="radio-muted">mp3 · m4a · aac · ogg · wav · flac · mp4 · webm{file ? ` · ${(file.size / 1048576).toFixed(1)} MB` : ""}</div>
      </div>
      <label><span>Title</span><input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={80} /></label>
      <label><span>Artist</span><input value={artist} onChange={(e) => setArtist(e.target.value)} maxLength={80} /></label>
      <div className="grid grid-cols-2 gap-3">
        <label><span>Theme <small>the dancer's cue</small></span>
          <select value={theme} onChange={(e) => setTheme(e.target.value)}>{THEMES.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
        <label><span>BPM <small>if you know it</small></span><input value={bpm} onChange={(e) => setBpm(e.target.value.replace(/\D/g, "").slice(0, 3))} placeholder="96" inputMode="numeric" /></label>
      </div>
      {err && <div className="radio-problem">{err}</div>}
      <div className="flex items-center gap-3">
        <button className="btn btn-primary" disabled={!file || busy} onClick={() => void go()}>{busy ? "Uploading…" : "⇪ Upload to the station"}</button>
        <span className="radio-muted">goes to the bucket, then into the catalog for everyone</span>
      </div>
    </div>
  );
}

/** Bars that dance to the sound - the stage while there is no video (the dancer replaces this). */
function Visualizer({ playing, theme }: { playing: boolean; theme: string }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    let raf = 0, t = 0;
    const hue = theme === "hype" ? 15 : theme === "dark" ? 275 : theme === "happy" ? 45 : theme === "focus" ? 200 : theme === "epic" ? 330 : 185;
    const step = () => {
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== w * 2 || c.height !== h * 2) { c.width = w * 2; c.height = h * 2; }
      g.setTransform(2, 0, 0, 2, 0, 0); g.clearRect(0, 0, w, h);
      const { bins: b, level } = radioBeat();
      const n = 48, bw = w / n;
      t += 0.016;
      for (let i = 0; i < n; i++) {
        const v = b && playing ? b[Math.floor(i * (b.length * 0.6) / n)] / 255 : 0.08 + 0.05 * Math.sin(t * 2 + i * 0.4);
        const bh = Math.max(2, v * h * 0.85);
        const grad = g.createLinearGradient(0, h - bh, 0, h);
        grad.addColorStop(0, `hsla(${hue + i * 1.5}, 90%, ${60 + level * 25}%, 0.95)`); grad.addColorStop(1, `hsla(${hue + i * 1.5}, 90%, 40%, 0.15)`);
        g.fillStyle = grad; g.fillRect(i * bw + 1, h - bh, bw - 2, bh);
        g.fillStyle = `hsla(${hue + i * 1.5}, 100%, 85%, ${v})`; g.fillRect(i * bw + 1, h - bh - 2, bw - 2, 2);
      }
      const r = 28 + level * 34;
      const rg = g.createRadialGradient(w / 2, h * 0.42, 0, w / 2, h * 0.42, r * 2);
      rg.addColorStop(0, `hsla(${hue}, 100%, 75%, ${0.35 + level * 0.5})`); rg.addColorStop(1, "transparent");
      g.fillStyle = rg; g.beginPath(); g.arc(w / 2, h * 0.42, r * 2, 0, Math.PI * 2); g.fill();
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, theme]);
  return <canvas ref={ref} className="radio-viz" />;
}

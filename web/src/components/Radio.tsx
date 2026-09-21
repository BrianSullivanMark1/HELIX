// HELIX RADIO — the company's music, from the bucket, playable from every page. One player lives
// outside React (a single <video> element) so a song keeps playing while you move between the
// Console, Talk and Settings. The deck slides down from the header button: now playing, the
// queue, shuffle, volume, the upload zone (theme + bpm typed by whoever uploads), the station
// name, and the video window for music videos (or the dancer, coming next). Hidden never deleted.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, getToken, tokenUrl, STALE_BACKEND } from "../lib/api";
import Backdrop, { SCENES, loadLook, saveLook, type Look } from "./Backdrop";
import Organism, { type OrganismLook } from "./Organism";
import HelixMark from "./HelixMark";
import Strandbar from "./Strandbar";
import { useJobs } from "../lib/jobs";
import "./radio.css";

export interface Track { id: string; title: string; artist: string; theme: string; bpm: number | null; kind: "audio" | "video"; mime: string; audio: string | null; video: string | null; uploaded_by: string; at: string; folder?: string; cached?: boolean; bytes?: number }
interface Deck { bucket: string | null; ok: boolean; problem: string | null; station: string; stations: Record<string, string>; folders: string[]; tracks: Track[]; count: number; cache?: { files: number; bytes: number; max_gb: number } }

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

interface PlayerState { track: Track | null; playing: boolean; queue: Track[]; shuffle: boolean; volume: number; showVideo: boolean; t: number; dur: number; phase: "dj" | "intro" | "video" | "peek"; deckOpen: boolean }
const listeners = new Set<() => void>();
const state: PlayerState = { track: null, playing: false, queue: [], shuffle: false, volume: 0.8, showVideo: true, t: 0, dur: 0, phase: "dj", deckOpen: false };
try { const v = localStorage.getItem("helix_radio"); if (v) { const s = JSON.parse(v); state.volume = s.volume ?? 0.8; state.shuffle = !!s.shuffle; state.showVideo = s.showVideo ?? true; } } catch { /* no storage */ }
const emit = () => { listeners.forEach((l) => l()); try { localStorage.setItem("helix_radio", JSON.stringify({ volume: state.volume, shuffle: state.shuffle, showVideo: state.showVideo })); } catch { /* no storage */ } };
media.volume = state.volume;
media.addEventListener("timeupdate", () => { state.t = media.currentTime; state.dur = media.duration || 0; emit(); });
media.addEventListener("play", () => { state.playing = true; emit(); });
media.addEventListener("pause", () => { state.playing = false; emit(); });
media.addEventListener("ended", () => { next(); });

let introTimer = 0, phaseTimer = 0;
/** The DJ's performance around a music video: a wink, the zoom-off, a curtain of neural net that
 *  forms over the stage and dissolves into the video (which starts under it). Only when the deck
 *  is open and the video is the thing on the stage; otherwise the song just plays. */
function cue(c: "video-in" | "resume" | "back" | "wink") { window.dispatchEvent(new CustomEvent("helix-dj", { detail: { cue: c } })); }
export function play(track: Track, queue?: Track[]) {
  if (queue) state.queue = queue;
  state.track = track;
  ensureAnalyser(); void ctx?.resume();
  media.src = tokenUrl(`/api/radio/play/${track.id}`);
  window.clearTimeout(introTimer); window.clearTimeout(phaseTimer);
  const perform = track.kind === "video" && state.showVideo && state.deckOpen;
  if (perform) {
    state.phase = "intro"; cue("video-in");
    introTimer = window.setTimeout(() => { void media.play().catch(() => undefined); }, 1250);
    phaseTimer = window.setTimeout(() => { state.phase = "video"; cue("back"); emit(); }, 2100);
  } else {
    state.phase = track.kind === "video" && state.showVideo ? "video" : "dj";
    void media.play().catch(() => undefined);
  }
  // the next track starts caching now, so it is on this PC before its turn
  const q = state.queue; const i = q.findIndex((t) => t.id === track.id);
  const nx = q.length > 1 && !state.shuffle ? q[(i + 1) % q.length] : null;
  if (nx && !nx.cached) void api.post("/api/radio/prefetch", { id: nx.id }).catch(() => undefined);
  emit();
}
export function toggle() {
  if (!state.track) return;
  if (media.paused) {
    ensureAnalyser(); void ctx?.resume(); void media.play().catch(() => undefined);
    // a resumed video: the DJ peeks in from the side, snaps a nod, and the picture ripples on
    if (state.track.kind === "video" && state.showVideo && state.deckOpen && media.currentTime > 0.5) {
      window.clearTimeout(phaseTimer);
      state.phase = "peek"; cue("resume"); emit();
      phaseTimer = window.setTimeout(() => { state.phase = "video"; cue("back"); emit(); }, 1700);
    }
  } else media.pause();
}
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
  const bars = useRef<HTMLSpanElement[]>([]);   // the five rungs of the logo, driven by the bands
  useEffect(() => {
    let raf = 0;
    const step = () => {
      const { bins: b } = radioBeat();
      bars.current.forEach((el, i) => { if (!el) return; const v = b && p.playing ? b[2 + i * 5] / 255 : i < 2 ? 0.8 : 0; el.style.opacity = String(i < 2 ? 0.55 + v * 0.45 : v); el.style.transform = i >= 2 ? `translateY(${-v * 3}px)` : ""; });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [p.playing]);
  return (
    <button className={`btn-nav radio-btn${p.playing ? " on" : ""}${open ? " open" : ""}`} onClick={onClick} data-tip={p.track ? `${p.track.title}${p.track.artist ? " - " + p.track.artist : ""}` : "HELIX RADIO - the company's music"}>
      <span className="radio-logo" aria-hidden="true">
        <svg viewBox="0 0 40 40" width="30" height="30">
          <defs><linearGradient id="rlgA" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#3fe0e0" /><stop offset="1" stopColor="#2a8cff" /></linearGradient></defs>
          {/* the head */}
          <circle cx="20" cy="21" r="11" fill="url(#rlgA)" opacity="0.35" />
          <circle cx="20" cy="21" r="11" fill="none" stroke="url(#rlgA)" strokeWidth="1.6" />
          {/* eyes + grin */}
          <ellipse cx="16" cy="19.5" rx="2" ry="1.4" fill="#dffbff" className="rl-eye" />
          <ellipse cx="24" cy="19.5" rx="2" ry="1.4" fill="#dffbff" className="rl-eye" />
          <path d="M15.5 25 Q20 29 24.5 25" fill="none" stroke="#dffbff" strokeWidth="1.6" strokeLinecap="round" />
          {/* the headset: band + cups, the cups pulse with the bands */}
          <path d="M8 21 A12 12 0 0 1 32 21" fill="none" stroke="#dffbff" strokeWidth="2.2" strokeLinecap="round" />
          <rect x="6" y="19" width="5" height="9" rx="2.5" fill="#dffbff" ref={(el) => { if (el) bars.current[0] = el as unknown as HTMLSpanElement; }} />
          <rect x="29" y="19" width="5" height="9" rx="2.5" fill="#dffbff" ref={(el) => { if (el) bars.current[1] = el as unknown as HTMLSpanElement; }} />
          {/* three notes rising when it plays */}
          <g className="rl-notes" fill="#dffbff"><circle cx="33" cy="9" r="1.4" ref={(el) => { if (el) bars.current[2] = el as unknown as HTMLSpanElement; }} /><circle cx="36" cy="5" r="1.1" ref={(el) => { if (el) bars.current[3] = el as unknown as HTMLSpanElement; }} /><circle cx="30" cy="4" r="1" ref={(el) => { if (el) bars.current[4] = el as unknown as HTMLSpanElement; }} /></g>
        </svg>
      </span>
      <span className="radio-word">RADIO</span>
    </button>
  );
}

// ---------------------------------------------------------------- the neural curtain (video intro)
/** Nodes fly in from every edge of the stage and knit into a lattice that covers it; it holds a
 *  beat, flashes, then every node falls into the middle and the picture underneath shows. */
function NeuralCurtain({ onDone }: { onDone: () => void }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current; if (!c) return;
    const g = c.getContext("2d"); if (!g) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = c.getBoundingClientRect(); const w = Math.max(10, r.width), h = Math.max(10, r.height);
    c.width = w * dpr; c.height = h * dpr;
    const N = 110;
    const nodes = Array.from({ length: N }, () => {
      const side = Math.floor(Math.random() * 4);
      const sx = side === 0 ? -20 : side === 1 ? w + 20 : Math.random() * w, sy = side === 2 ? -20 : side === 3 ? h + 20 : Math.random() * h;
      return { sx, sy, tx: Math.random() * w, ty: Math.random() * h, x: sx, y: sy, d: Math.random() * 0.25 };
    });
    let t = 0, last = performance.now(), raf = 0, done = false;
    const LINK = Math.min(w, h) * 0.22;
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
      const knit = Math.min(1, Math.max(0, (t - 0.0) / 0.7));            // fly in 0..0.7
      const hold = Math.min(1, Math.max(0, (t - 0.7) / 0.35));           // brighten 0.7..1.05
      const fall = Math.min(1, Math.max(0, (t - 1.05) / 0.5));           // collapse 1.05..1.55
      const veil = 1 - fall;
      g.fillStyle = `rgba(4, 7, 10, ${(0.85 * veil).toFixed(3)})`; g.fillRect(0, 0, w, h);
      for (const n of nodes) {
        const k = Math.min(1, Math.max(0, (knit - n.d) / (1 - n.d)));
        const e = 1 - Math.pow(1 - k, 3);
        let x = n.sx + (n.tx - n.sx) * e, y = n.sy + (n.ty - n.sy) * e;
        if (fall > 0) { const f = fall * fall; x += (w / 2 - x) * f; y += (h / 2 - y) * f; }
        n.x = x; n.y = y;
      }
      g.globalCompositeOperation = "lighter";
      for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
        const a = nodes[i], b = nodes[j]; const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d > LINK) continue;
        g.strokeStyle = `rgba(63,224,224,${((1 - d / LINK) * (0.25 + 0.6 * hold) * veil).toFixed(3)})`; g.lineWidth = 1 + hold;
        g.beginPath(); g.moveTo(a.x, a.y); g.lineTo(b.x, b.y); g.stroke();
      }
      for (const n of nodes) {
        g.fillStyle = `rgba(${Math.round(120 + 135 * hold)},255,255,${(0.9 * veil).toFixed(3)})`;
        g.shadowColor = "#3fe0e0"; g.shadowBlur = 8 + 14 * hold;
        g.beginPath(); g.arc(n.x, n.y, 1.6 + 1.6 * hold, 0, Math.PI * 2); g.fill();
      }
      g.shadowBlur = 0;
      if (hold > 0 && fall < 0.2) { g.fillStyle = `rgba(200,255,255,${(0.18 * hold * (1 - fall * 5)).toFixed(3)})`; g.fillRect(0, 0, w, h); }
      if (fall > 0) { const gr = g.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.6 * fall); gr.addColorStop(0, `rgba(230,255,255,${(0.5 * (1 - fall)).toFixed(3)})`); gr.addColorStop(1, "rgba(63,224,224,0)"); g.fillStyle = gr; g.fillRect(0, 0, w, h); }
      g.globalCompositeOperation = "source-over";
      if (t < 1.6) raf = requestAnimationFrame(step); else if (!done) { done = true; onDone(); }
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [onDone]);
  return <canvas ref={ref} className="radio-curtain" aria-hidden="true" />;
}

// ---------------------------------------------------------------- the deck
const fmtBytes = (b: number) => b > 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)} GB` : `${Math.round(b / 1024 ** 2)} MB`;
const inShelf = (t: Track, shelf: string) => !shelf || (t.folder || "") === shelf || (t.folder || "").startsWith(shelf + "/");

export function RadioDeck({ open, onClose, appKey = "default", look }: { open: boolean; onClose: () => void; appKey?: string; look?: OrganismLook }) {
  const p = usePlayer();
  const [deck, setDeck] = useState<Deck | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [bucketEdit, setBucketEdit] = useState("");
  const [stationEdit, setStationEdit] = useState<string | null>(null);
  const [tab, setTab] = useState<"tracks" | "upload" | "settings">("tracks");
  const [shelf, setShelf] = useState<string>("");
  const [lookState, setLookState] = useState<Look>(() => loadLook());
  const setLook = (patch: Partial<Look>) => { const next = { ...lookState, ...patch }; setLookState(next); saveLook(next); };
  const video = useRef<HTMLDivElement | null>(null);
  const [curtain, setCurtain] = useState(false);
  const load = useCallback(() => {
    void api.get<Deck>(`/api/radio?app_key=${encodeURIComponent(appKey)}`).then((d) => { setDeck(d); setErr(null); }).catch((e: Error) => setErr(e.message));
  }, [appKey]);
  useEffect(() => { if (open) load(); state.deckOpen = open; if (!open && state.phase !== "dj") { state.phase = state.track?.kind === "video" && state.showVideo ? "video" : "dj"; } }, [open, load]);
  // a finished upload or cache refreshes the list
  const jobs = useJobs((s) => s.jobs);
  const lastDone = useRef("");
  useEffect(() => {
    const done = Object.values(jobs).filter((j) => (j.kind === "upload" || j.kind === "cache" || j.kind === "fetch") && j.state === "done").map((j) => j.id + j.finished).sort().pop() || "";
    if (done && done !== lastDone.current) { lastDone.current = done; if (open) load(); }
  }, [jobs, open, load]);
  // the one <video> element is borrowed by the open deck, and PARKED (never detached) after
  useEffect(() => {
    const host = video.current;
    if (!host || !open) return;
    host.appendChild(media);
    return () => { parking.appendChild(media); };
  }, [open, p.track?.id, p.showVideo]);
  useEffect(() => { if (p.phase === "intro") { const id = window.setTimeout(() => setCurtain(true), 600); return () => window.clearTimeout(id); } setCurtain(false); }, [p.phase]);
  const onCurtainDone = useCallback(() => setCurtain(false), []);
  const tracks = useMemo(() => (deck?.tracks ?? []).filter((t) => inShelf(t, shelf)).filter((t) => !q || `${t.title} ${t.artist} ${t.theme} ${t.uploaded_by} ${t.folder || ""}`.toLowerCase().includes(q.toLowerCase())), [deck, q, shelf]);
  const isVideo = p.track?.kind === "video";
  const showVideo = isVideo && p.showVideo;
  const djOnTop = !showVideo || p.phase === "intro" || p.phase === "peek";
  const faceLook: OrganismLook = look || { phase: "face", hue: 185, energy: 1 };
  if (!open) return null;
  const moveTrack = (id: string, folder: string) => void api.put("/api/radio/track", { id, folder }).then(load).catch((e: Error) => setErr(e.message));
  return (
    <div className="radio-wrap" onClick={onClose}>
      <div className={`radio-deck${lookState.layout === "compact" ? " compact" : ""}`} onClick={(e) => e.stopPropagation()}>
        <div className="radio-head">
          <div className="radio-mark" aria-hidden="true"><HelixMark size={48} radio /></div>
          <div>
            <div className="radio-kicker">{p.playing ? <span className="radio-onair">● ON AIR</span> : "HELIX RADIO"}{deck?.bucket ? ` · gs://${deck.bucket}` : ""}</div>
            <div className="radio-station">
              {stationEdit === null ? (
                <button className="radio-station-name" data-tip="Rename this station" onClick={() => setStationEdit(deck?.station ?? "")}>{deck?.station ?? "HELIX RADIO"} <small>✎</small></button>
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
            <button className={`radio-tab${tab === "tracks" ? " on" : ""}`} onClick={() => setTab("tracks")}><i className="radio-tab-ic">♫</i><span>Tracks</span>{deck ? <b>{deck.count}</b> : null}</button>
            <button className={`radio-tab${tab === "upload" ? " on" : ""}`} onClick={() => setTab("upload")}><i className="radio-tab-ic">⇪</i><span>Upload</span></button>
            <button className={`radio-gear${tab === "settings" ? " on" : ""}`} data-tip="Radio settings: background, layout, station, bucket, cache" onClick={() => setTab(tab === "settings" ? "tracks" : "settings")}>
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M19.4 13a7.6 7.6 0 0 0 .1-1 7.6 7.6 0 0 0-.1-1l2.1-1.6a.5.5 0 0 0 .1-.7l-2-3.4a.5.5 0 0 0-.6-.2l-2.5 1a7.3 7.3 0 0 0-1.7-1L14.4 2.4a.5.5 0 0 0-.5-.4h-4a.5.5 0 0 0-.5.4L9 5.1a7.3 7.3 0 0 0-1.7 1l-2.5-1a.5.5 0 0 0-.6.2l-2 3.4a.5.5 0 0 0 .1.7L4.6 11a7.6 7.6 0 0 0 0 2l-2.1 1.6a.5.5 0 0 0-.1.7l2 3.4a.5.5 0 0 0 .6.2l2.5-1a7.3 7.3 0 0 0 1.7 1l.4 2.7a.5.5 0 0 0 .5.4h4a.5.5 0 0 0 .5-.4l.4-2.7a7.3 7.3 0 0 0 1.7-1l2.5 1a.5.5 0 0 0 .6-.2l2-3.4a.5.5 0 0 0-.1-.7L19.4 13zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z" /></svg>
            </button>
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
          {/* THE STAGE: the DJ (the real face), or the video with the DJ's performance around it */}
          <div className={`radio-stage${showVideo ? " video" : ""} phase-${p.phase}`}>
            <div ref={video} className="radio-video" style={{ display: showVideo ? "block" : "none" }} />
            <div className={`radio-dj${djOnTop ? " on-top" : ""}${p.phase === "peek" ? " peek" : ""}${lookState.djScene !== "off" ? " scened" : ""}`}>
              {lookState.djScene !== "off" && p.phase !== "peek" && <div className="radio-dj-back"><Backdrop scene={lookState.djScene} boxed /></div>}
              <Organism look={faceLook} mode="dj" />
            </div>
            {curtain && <NeuralCurtain onDone={onCurtainDone} />}
            {p.phase === "peek" && <div className="radio-ripple" aria-hidden="true" />}
            {isVideo && (
              <button className="radio-swap" onClick={() => { state.showVideo = !state.showVideo; state.phase = state.showVideo ? "video" : "dj"; emit(); }}>{p.showVideo ? "Show the DJ" : "Show the video"}</button>
            )}
          </div>

          {/* NOW PLAYING + transport */}
          <div className="radio-now">
            <div className="radio-title elide">{p.track ? p.track.title : "Nothing playing"}</div>
            <div className="radio-sub elide">{p.track ? [p.track.artist, p.track.theme, p.track.bpm ? `${p.track.bpm} bpm` : "", p.track.folder ? `▤ ${p.track.folder}` : ""].filter(Boolean).join(" · ") || " " : "pick a track, or upload one"}</div>
            <div className="radio-seek" onClick={(e) => { const r = (e.currentTarget as HTMLDivElement).getBoundingClientRect(); if (p.dur) media.currentTime = ((e.clientX - r.left) / r.width) * p.dur; }}>
              <i style={{ width: `${p.dur ? (p.t / p.dur) * 100 : 0}%` }} />
            </div>
            <div className="radio-times"><span>{fmt(p.t)}</span><span>{fmt(p.dur)}</span></div>
            <div className="radio-transport">
              <button className={`radio-ctl${p.shuffle ? " on" : ""}`} data-tip="Shuffle" onClick={() => { state.shuffle = !state.shuffle; emit(); }}>⤨</button>
              <button className="radio-ctl" data-tip="Previous" onClick={prev}>⏮</button>
              <button className="radio-ctl big" data-tip={p.playing ? "Pause" : "Play"} onClick={() => { if (!p.track && tracks.length) play(tracks[0], tracks); else toggle(); }}>{p.playing ? "❚❚" : "▶"}</button>
              <button className="radio-ctl" data-tip="Next" onClick={next}>⏭</button>
              <input className="radio-vol" type="range" min={0} max={1} step={0.01} value={p.volume} data-tip="Volume" onChange={(e) => { state.volume = Number(e.target.value); media.volume = state.volume; emit(); }} />
            </div>
          </div>

          {/* THE LIST / UPLOAD / SETTINGS */}
          <div className="radio-side">
            {tab === "tracks" && (
              <div className="radio-library">
                <Shelves folders={deck?.folders ?? []} tracks={deck?.tracks ?? []} shelf={shelf} onPick={setShelf} onChanged={load} onError={setErr} onDropTrack={moveTrack} />
                <div className="radio-list-col">
                  <input className="radio-search" placeholder={shelf ? `Find in ${shelf}…` : "Find a track, artist, theme…"} value={q} onChange={(e) => setQ(e.target.value)} />
                  <div className="radio-list">
                    {deck === null && !err && <div className="radio-muted">Reading the catalog…</div>}
                    {deck && tracks.length === 0 && <div className="radio-muted">{deck.count === 0 ? "No songs yet. Upload the first one." : shelf ? "This shelf is empty. Drag tracks onto it." : "Nothing matches."}</div>}
                    {tracks.map((t, i) => (
                      <div key={t.id} className={`radio-row${p.track?.id === t.id ? " on" : ""}`} onDoubleClick={() => play(t, tracks)} draggable
                        onDragStart={(e) => { e.dataTransfer.setData("text/helix-track", t.id); e.dataTransfer.effectAllowed = "move"; }}>
                        <button className="radio-row-play" onClick={() => (p.track?.id === t.id ? toggle() : play(t, tracks))}>{p.track?.id === t.id && p.playing ? "❚❚" : "▶"}</button>
                        <div className="min-w-0 flex-1">
                          <div className="elide radio-row-title">{t.kind === "video" ? "▣ " : ""}{t.title}{t.cached ? <i className="radio-cached" data-tip="On this PC - plays at once" /> : null}</div>
                          <div className="elide radio-row-sub">{[t.artist, t.theme, t.bpm ? `${t.bpm} bpm` : "", !shelf && t.folder ? `▤ ${t.folder}` : "", t.uploaded_by.split("@")[0]].filter(Boolean).join(" · ")}</div>
                        </div>
                        <span className="radio-row-n">{String(i + 1).padStart(2, "0")}</span>
                        <select className="radio-row-move" value={t.folder || ""} data-tip="Move to a shelf" onChange={(e) => moveTrack(t.id, e.target.value)} onClick={(e) => e.stopPropagation()}>
                          <option value="">(top)</option>
                          {(deck?.folders ?? []).map((f) => <option key={f} value={f}>{f}</option>)}
                        </select>
                        <button className="radio-row-hide" data-tip="Hide from the deck (the file stays in the bucket)" onClick={() => void api.put("/api/radio/hide", { id: t.id }).then(load)}>✕</button>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
            {tab === "upload" && <Upload folders={deck?.folders ?? []} shelf={shelf} onDone={() => { setTab("tracks"); load(); }} />}
            {tab === "settings" && (
              <div className="radio-form">
                <div className="radio-kicker">BACKGROUND · behind the whole app while music plays</div>
                <div className="flex items-center gap-2 flex-wrap">
                  <label className="radio-select">
                    <select value={lookState.surprise ? "surprise" : lookState.scene} onChange={(e) => e.target.value === "surprise" ? setLook({ surprise: true }) : setLook({ scene: e.target.value as Look["scene"], surprise: false })}>
                      {SCENES.map((sc) => <option key={sc.key} value={sc.key}>{sc.name}</option>)}
                      <option value="surprise">⚄ Surprise me - a new scene every song</option>
                    </select>
                    <span className="radio-select-chev">▾</span>
                  </label>
                  <span className="radio-muted">{lookState.surprise ? "A different scene for every song, rolled fresh each time." : SCENES.find((sc) => sc.key === lookState.scene)?.blurb}</span>
                </div>
                <div className="radio-kicker mt-2">BEHIND THE DJ</div>
                <div className="radio-scenes">
                  {SCENES.map((sc) => <button key={sc.key} className={`radio-scene${lookState.djScene === sc.key ? " on" : ""}`} onClick={() => setLook({ djScene: sc.key })}>{sc.key === "off" ? "Just the helix" : sc.name}</button>)}
                </div>
                <label><span>Intensity <small>how hard the pages move to the music</small></span>
                  <input type="range" min={0.2} max={1.5} step={0.05} value={lookState.intensity} onChange={(e) => setLook({ intensity: Number(e.target.value) })} /></label>
                <div className="radio-kicker mt-2">LAYOUT</div>
                <div className="radio-scenes">
                  <button className={`radio-scene${lookState.layout === "full" ? " on" : ""}`} onClick={() => setLook({ layout: "full" })}>Full deck</button>
                  <button className={`radio-scene${lookState.layout === "compact" ? " on" : ""}`} onClick={() => setLook({ layout: "compact" })}>Compact</button>
                </div>
                <div className="radio-kicker mt-2">ON THIS PC · the cache</div>
                <p className="radio-muted" data-tip="Every song and video stays on this PC after its first play, so the next play is instant. Oldest-played goes first when the cap is reached.">{deck?.cache ? <b>{deck.cache.files} file{deck.cache.files === 1 ? "" : "s"} · {fmtBytes(deck.cache.bytes)} of {deck.cache.max_gb} GB</b> : "nothing cached yet"}</p>
                <div className="flex items-center gap-3 flex-wrap">
                  <button className="btn btn-primary text-xs" onClick={() => void api.post("/api/radio/cache").then(() => setErr(null)).catch((x: Error) => setErr(x.message))}>▤ Cache the whole station now</button>
                  <label className="radio-inline"><span>Cap, GB</span><input type="number" min={1} max={200} defaultValue={deck?.cache?.max_gb ?? 5} style={{ width: 70 }} onBlur={(e) => void api.put("/api/settings", { values: { radio_cache_gb: Number(e.target.value) || 5 } }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                </div>
                <div className="radio-kicker mt-2">THIS STATION</div>
                <label data-tip={`The name every copy of HELIX and every app keyed on ${appKey} shows. A local name wins on this PC only.`}><span>Name in the catalog ({appKey})</span><input defaultValue={deck?.stations?.[appKey] ?? ""} placeholder={deck?.stations?.default ?? "HELIX RADIO"} onBlur={(e) => void api.put("/api/radio/station", { app: appKey, name: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <label><span>Local name, this PC only</span><input placeholder="leave empty to use the catalog" onBlur={(e) => void api.put("/api/radio/station", { local: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <div className="radio-kicker mt-3">THE BUCKET</div>
                <label><span>gs://</span><input defaultValue={deck?.bucket ?? ""} placeholder="helix-radio-mark1" onBlur={(e) => void api.put("/api/radio/bucket", { bucket: e.target.value }).then(load).catch((x: Error) => setErr(x.message))} /></label>
                <p className="radio-muted">Private; your own gcloud login; nothing is ever deleted from here.</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- the shelves (folders)
function Shelves({ folders, tracks, shelf, onPick, onChanged, onError, onDropTrack }: { folders: string[]; tracks: Track[]; shelf: string; onPick: (f: string) => void; onChanged: () => void; onError: (e: string) => void; onDropTrack: (id: string, folder: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [over, setOver] = useState<string | null>(null);
  const count = (f: string) => tracks.filter((t) => inShelf(t, f)).length;
  const create = () => {
    const path = (shelf ? shelf + "/" : "") + name.trim();
    if (!name.trim()) return;
    void api.post("/api/radio/folder", { path }).then(() => { setAdding(false); setName(""); onChanged(); }).catch((e: Error) => onError(e.message));
  };
  const rename = (old: string) => {
    const parent = old.includes("/") ? old.slice(0, old.lastIndexOf("/") + 1) : "";
    void api.put("/api/radio/folder", { old, new: parent + newName.trim() }).then(() => { setRenaming(null); if (shelf === old) onPick(parent + newName.trim()); onChanged(); }).catch((e: Error) => onError(e.message));
  };
  const remove = (f: string) => void api.del(`/api/radio/folder?path=${encodeURIComponent(f)}`).then(() => { if (shelf === f) onPick(""); onChanged(); }).catch((e: Error) => onError(e.message));
  const drop = (f: string) => (e: React.DragEvent) => { e.preventDefault(); setOver(null); const id = e.dataTransfer.getData("text/helix-track"); if (id) onDropTrack(id, f); };
  const dragOver = (f: string) => (e: React.DragEvent) => { if (e.dataTransfer.types.includes("text/helix-track")) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; setOver(f); } };
  return (
    <div className="radio-shelves">
      <div className="radio-kicker" style={{ display: "flex", alignItems: "center", gap: 6 }}>SHELVES <span className="flex-1" /><button className="radio-shelf-add" data-tip={shelf ? `New shelf inside ${shelf}` : "New shelf"} onClick={() => { setAdding(true); setName(""); }}>＋</button></div>
      <button className={`radio-shelf${!shelf ? " on" : ""}${over === "" ? " over" : ""}`} onClick={() => onPick("")} onDragOver={dragOver("")} onDragLeave={() => setOver(null)} onDrop={drop("")}>
        <i>▤</i><span className="elide">Everything</span><b>{tracks.length}</b>
      </button>
      {folders.map((f) => {
        const depth = f.split("/").length - 1, leaf = f.split("/").pop() || f;
        return renaming === f ? (
          <form key={f} className="radio-shelf-edit" style={{ paddingLeft: 10 + depth * 14 }} onSubmit={(e) => { e.preventDefault(); rename(f); }}>
            <input autoFocus value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={40} />
            <button className="task-btn" type="submit">Save</button><button className="task-btn dim" type="button" onClick={() => setRenaming(null)}>✕</button>
          </form>
        ) : (
          <div key={f} className={`radio-shelf${shelf === f ? " on" : ""}${over === f ? " over" : ""}`} style={{ paddingLeft: 10 + depth * 14 }} role="button" onClick={() => onPick(f)}
            onDragOver={dragOver(f)} onDragLeave={() => setOver(null)} onDrop={drop(f)}>
            <i>{depth ? "└" : "▤"}</i><span className="elide">{leaf}</span><b>{count(f)}</b>
            <span className="radio-shelf-acts" onClick={(e) => e.stopPropagation()}>
              <button data-tip="Rename this shelf" onClick={() => { setRenaming(f); setNewName(leaf); }}>✎</button>
              {count(f) === 0 && <button data-tip="Remove this empty shelf" onClick={() => remove(f)}>✕</button>}
            </span>
          </div>
        );
      })}
      {adding && (
        <form className="radio-shelf-edit" onSubmit={(e) => { e.preventDefault(); create(); }}>
          <input autoFocus placeholder={shelf ? `inside ${shelf}` : "Rock, Late night, Kate's picks…"} value={name} onChange={(e) => setName(e.target.value)} maxLength={40} />
          <button className="task-btn" type="submit">Add</button><button className="task-btn dim" type="button" onClick={() => setAdding(false)}>✕</button>
        </form>
      )}
      <div className="radio-muted" style={{ fontSize: 10.5, marginTop: 6 }} data-tip="A shelf can hold shelves, four deep. Everyone sees the same shelves.">Drag a track onto a shelf.</div>
    </div>
  );
}

// ---------------------------------------------------------------- the upload (a task, with real progress)
function Upload({ onDone, folders, shelf }: { onDone: () => void; folders: string[]; shelf: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState(""); const [artist, setArtist] = useState(""); const [theme, setTheme] = useState("chill"); const [bpm, setBpm] = useState(""); const [folder, setFolder] = useState(shelf);
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null); const [over, setOver] = useState(false);
  const [sent, setSent] = useState(0);            // the sending half, from the browser's own count
  const [state, setState] = useState<"running" | "done" | "failed" | "cancelled">("running");
  const xhr = useRef<XMLHttpRequest | null>(null);
  const jobs = useJobs((s) => s.jobs);
  const cancelJob = useJobs((s) => s.cancel);
  useEffect(() => setFolder(shelf), [shelf]);
  const jobTitle = `Upload ${title.trim() || file?.name || ""}`;
  const job = useMemo(() => Object.values(jobs).filter((j) => j.kind === "upload" && j.title === jobTitle).sort((a, b) => (a.started < b.started ? 1 : -1))[0], [jobs, jobTitle]);
  const pick = (f: File | null) => { setFile(f); setErr(null); setState("running"); setSent(0); if (f && !title) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ")); };
  const go = () => {
    if (!file) return;
    setBusy(true); setErr(null); setState("running"); setSent(0);
    const qs = new URLSearchParams({ name: file.name, title, artist, theme, bpm, folder }).toString();
    const x = new XMLHttpRequest(); xhr.current = x;
    x.open("PUT", `/api/radio/upload?${qs}`);
    x.setRequestHeader("X-Helix-Token", getToken());
    x.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable) setSent(e.loaded / e.total); };
    x.onload = () => {
      setBusy(false);
      if (x.status >= 200 && x.status < 300) { setState("done"); window.setTimeout(onDone, 1400); return; }
      let d = `${x.status}`;
      try { d = JSON.parse(x.responseText).error || d; } catch { d = x.status === 405 || x.status === 404 ? STALE_BACKEND : d; }
      setErr(d); setState(x.status === 409 ? "cancelled" : "failed");
    };
    x.onerror = () => { setBusy(false); setErr("The upload broke off."); setState("failed"); };
    x.onabort = () => { setBusy(false); setState("cancelled"); };
    x.send(file);
  };
  const stop = () => { xhr.current?.abort(); if (job && job.state === "running") void cancelJob(job.id); };
  const progress = busy || state !== "running" ? (job && job.progress !== null && job.progress > 0.5 ? job.progress : sent * 0.5) : null;
  const note = state === "running" ? (job && job.progress !== null && job.progress > 0.5 ? job.note : sent < 1 ? `Sending to HELIX - ${Math.round(sent * 100)}%` : "Storing in the bucket") : job?.note || "";
  return (
    <div className="radio-form">
      <div className={`radio-drop${over ? " over" : ""}${file ? " has" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0] || null); }}>
        <input type="file" accept="audio/*,video/mp4,video/webm,.mp3,.m4a,.aac,.ogg,.wav,.flac,.mp4,.webm" onChange={(e) => pick(e.target.files?.[0] || null)} disabled={busy} />
        <div className="radio-drop-ic">{file ? (file.type.startsWith("video") ? "▣" : "♫") : "⇪"}</div>
        <div>{file ? file.name : "Drop a song or a music video here, or click to pick one"}</div>
        <div className="radio-muted">mp3 · m4a · aac · ogg · wav · flac · mp4 · webm{file ? ` · ${(file.size / 1048576).toFixed(1)} MB` : ""}</div>
      </div>
      <label><span>Title</span><input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={80} disabled={busy} /></label>
      <label><span>Artist</span><input value={artist} onChange={(e) => setArtist(e.target.value)} maxLength={80} disabled={busy} /></label>
      <div className="grid grid-cols-3 gap-3">
        <label><span>Theme <small>the DJ's cue</small></span>
          <select value={theme} onChange={(e) => setTheme(e.target.value)} disabled={busy}>{THEMES.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
        <label><span>BPM <small>if you know it</small></span><input value={bpm} onChange={(e) => setBpm(e.target.value.replace(/\D/g, "").slice(0, 3))} placeholder="96" inputMode="numeric" disabled={busy} /></label>
        <label><span>Shelf</span>
          <select value={folder} onChange={(e) => setFolder(e.target.value)} disabled={busy}><option value="">(top)</option>{folders.map((f) => <option key={f} value={f}>{f}</option>)}</select></label>
      </div>
      {(busy || state !== "running") && file && (
        <div className="radio-upload-bar">
          <Strandbar progress={progress} state={state} height={20} note={note} />
        </div>
      )}
      {err && <div className="radio-problem">{err}</div>}
      <div className="flex items-center gap-3" style={{ marginTop: busy || state !== "running" ? 14 : 0 }}>
        {!busy && <button className="btn btn-primary" disabled={!file || busy} onClick={() => { if (state !== "running") { setState("running"); setSent(0); } go(); }}>{state === "done" ? "✓ In the catalog" : state === "failed" || state === "cancelled" ? "⇪ Try again" : "⇪ Upload to the station"}</button>}
        {busy && <button className="task-btn stop" onClick={stop} data-tip="Stops sending. Nothing reaches the catalog.">Stop</button>}
        <span className="radio-muted">{busy ? "You can close the deck - it keeps going, and CURRENT TASKS shows it." : "goes to the bucket, then into the catalog for everyone"}</span>
      </div>
    </div>
  );
}

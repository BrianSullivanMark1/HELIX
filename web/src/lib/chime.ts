// THE CHIMES - HELIX's own sounds, synthesised (no files): a task finishing rings a rising fifth
// with a shimmer; a task failing lands a soft low two-note with a breath of noise; a task
// starting is one tick. Muted with the "sounds" switch (localStorage "helix_sounds" = "off").
// Every sound is short (< 1.2 s), quiet, and never stacks louder than one voice.
let ctx: AudioContext | null = null;
let last = 0;

function audio(): AudioContext | null {
  try {
    if (!ctx) ctx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch { return null; }
}

export function soundsOn(): boolean {
  try { return localStorage.getItem("helix_sounds") !== "off"; } catch { return true; }
}
export function setSounds(on: boolean): void {
  try { localStorage.setItem("helix_sounds", on ? "on" : "off"); } catch { /* fine */ }
}

function tone(c: AudioContext, at: number, hz: number, dur: number, gain: number, type: OscillatorType = "sine", glide?: number) {
  const o = c.createOscillator(); const g = c.createGain();
  o.type = type; o.frequency.setValueAtTime(hz, at);
  if (glide) o.frequency.exponentialRampToValueAtTime(glide, at + dur);
  g.gain.setValueAtTime(0.0001, at);
  g.gain.exponentialRampToValueAtTime(gain, at + 0.012);
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  o.connect(g).connect(c.destination);
  o.start(at); o.stop(at + dur + 0.02);
}

function breath(c: AudioContext, at: number, dur: number, gain: number, hz: number) {
  const n = c.createBufferSource();
  const buf = c.createBuffer(1, Math.floor(c.sampleRate * dur), c.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length);
  n.buffer = buf;
  const f = c.createBiquadFilter(); f.type = "bandpass"; f.frequency.value = hz; f.Q.value = 0.8;
  const g = c.createGain(); g.gain.setValueAtTime(gain, at); g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
  n.connect(f).connect(g).connect(c.destination); n.start(at); n.stop(at + dur);
}

/** A task finished: three rising notes and a shimmer (C5 - G5 - C6, then a high sparkle). */
export function chimeDone(): void {
  if (!soundsOn()) return;
  const c = audio(); if (!c) return;
  const now = c.currentTime;
  if (now - last < 0.25) return; last = now;
  tone(c, now, 523.25, 0.32, 0.09);
  tone(c, now + 0.11, 783.99, 0.34, 0.09);
  tone(c, now + 0.22, 1046.5, 0.55, 0.10);
  tone(c, now + 0.30, 2093, 0.5, 0.025, "triangle");
  tone(c, now + 0.34, 3135.96, 0.6, 0.012, "sine");
  breath(c, now + 0.25, 0.5, 0.02, 6000);
}

/** A task failed or was stopped: a soft low fall (A3 -> E3) with a breath. Never harsh. */
export function chimeFail(): void {
  if (!soundsOn()) return;
  const c = audio(); if (!c) return;
  const now = c.currentTime;
  if (now - last < 0.25) return; last = now;
  tone(c, now, 220, 0.42, 0.10, "triangle", 196);
  tone(c, now + 0.20, 164.81, 0.7, 0.10, "triangle", 146.83);
  breath(c, now + 0.05, 0.45, 0.03, 900);
}

/** A task began: one small tick, so a background start is felt and not missed. */
export function chimeStart(): void {
  if (!soundsOn()) return;
  const c = audio(); if (!c) return;
  const now = c.currentTime;
  if (now - last < 0.12) return; last = now;
  tone(c, now, 1318.5, 0.08, 0.035, "sine");
  tone(c, now + 0.03, 1760, 0.1, 0.02, "sine");
}

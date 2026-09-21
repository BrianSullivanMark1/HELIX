// Talk — the orb's home (formerly the Console page). Opened from a project's Dev button, from the
// docked orb, or from the menu; the merged Console (cards) is the app's front page now. Transparent over the orb; the transcript, the status pill, the
// voice button, attachments, and the input row. All the ordering rules live server-side in the
// ShellSession; this page renders events and sends gestures. The camera panel docks beside it (the
// transcript makes room) or takes the window over for AR work (the transcript steps aside; the
// input row stays so you can keep talking to what you're looking at).
import { useCallback, useEffect, useRef, useState } from "react";
import VisualBlock from "../components/Chart";
import { api, tokenUrl } from "../lib/api";
import { useHelix, type Attachment, type Bubble, type Murmur } from "../lib/store";
import { tablesToHtml, tablesToTabs } from "../lib/table";
import { copyRich } from "../lib/tableexport";

const STATE_LINES: Record<string, string> = {
  listening: "Listening…",
  transcribing: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

/** THE STATE SIGN (Brian, 2026-09-22: "reduce the need to read"): instead of the words
 *  Listening / Thinking / Speaking, an animation - ears = bars breathing with the mic level,
 *  thinking = three sparks orbiting, speaking = a waveform. The word stays as the tooltip. */
function StateSign({ state, level }: { state: string; level: number }) {
  const kind = state === "listening" || state === "transcribing" ? "ears" : state === "thinking" ? "think" : state === "speaking" ? "voice" : "";
  if (!kind) return null;
  return (
    <span className={`state-sign ${kind}`} data-tip={STATE_LINES[state]} aria-label={STATE_LINES[state]}>
      {kind === "ears" && [0, 1, 2, 3, 4].map((i) => <i key={i} style={{ animationDelay: `${i * 0.12}s`, height: `${6 + level * 14 + (i === 2 ? 4 : 0)}px` }} />)}
      {kind === "think" && [0, 1, 2].map((i) => <b key={i} style={{ animationDelay: `${i * 0.4}s` }} />)}
      {kind === "voice" && [0, 1, 2, 3, 4, 5, 6].map((i) => <i key={i} style={{ animationDelay: `${i * 0.09}s` }} />)}
    </span>
  );
}

const ICONS = {
  clip: <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" d="M21 11.5 12.5 20a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.5 3.5 0 0 1 5 5l-8.5 8.5a1.5 1.5 0 0 1-2.1-2.1L15.5 7" /></svg>,
  camera: <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" d="M4 8h3l1.5-2h7L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1Z" /><circle cx="12" cy="13" r="3.2" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>,
  video: <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><rect x="3" y="7" width="13" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" /><path fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" d="m16 10 5-2v8l-5-2z" /></svg>,
  say: <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M4 9v6h4l5 4V5L8 9H4Z" /><path fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" /></svg>,
  joke: <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" /><path fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" d="M8 10h.01M16 10h.01" /><path fill="currentColor" d="M7.5 13.5h9c-.6 3-2.6 4.5-4.5 4.5s-3.9-1.5-4.5-4.5Z" /></svg>,
};

/**
 * Sleep-talk: while HELIX dreams, each murmur (services/murmur.py) surfaces as one soft italic line
 * above the status pill — it condenses in, drifts upward for a few seconds, and thins away. The
 * newest murmur replaces the last; the star's own REM flicker (Orb.tsx) lands on the same beat.
 */
function SleepTalk() {
  const murmur = useHelix((s) => s.murmur);
  const dreaming = useHelix((s) => Boolean(s.dream?.running));
  const [shown, setShown] = useState<Murmur | null>(null);
  const [fading, setFading] = useState(false);
  const seq = murmur?.seq ?? 0;
  useEffect(() => {
    if (!murmur || !dreaming) {
      setShown(null);
      return undefined;
    }
    setShown(murmur);
    setFading(false);
    const hold = window.setTimeout(() => setFading(true), 9000);
    const gone = window.setTimeout(() => setShown(null), 11800);
    return () => {
      window.clearTimeout(hold);
      window.clearTimeout(gone);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq, dreaming]);
  if (!shown) return null;
  return (
    <div key={shown.seq} className={`sleep-talk ${fading ? "fading" : ""}`} aria-live="polite"
      title="HELIX, talking in its sleep">
      {shown.text}
    </div>
  );
}

function BubbleView({ b, idx }: { b: Bubble; idx: number }) {
  const useAction = useHelix((s) => s.useAction);
  const isUser = b.role === "user";
  const isSystem = b.role === "system";
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const fire = (id: string, label: string) => {
    useAction(b.id, label);
    void api.post("/api/shell/action", { id }).catch(() => undefined);
  };
  // Copy the bubble both ways: HTML, so a markdown table lands in Gmail/Word as a real table, and
  // tab-delimited text, so the same copy pastes into Slack or Excel as columns. Prose is untouched
  // either way (see lib/table.ts).
  const copy = () => void copyRich(tablesToHtml(b.text), tablesToTabs(b.text));
  return (
    <div className={`w-full flex ${isUser ? "justify-end" : "justify-start"} materialize`}
      style={{ "--i": idx % 4 } as React.CSSProperties}>
      <div
        className="group relative max-w-[560px] rounded-2xl px-4 py-2.5 text-[14px] leading-relaxed"
        style={{
          background: isUser ? "rgba(18,27,36,0.82)" : "rgba(13,20,27,0.82)",
          border: `1px solid ${isUser ? "var(--line)" : isSystem ? "var(--line)" : "var(--cyan-dim)"}`,
          color: isSystem ? "var(--muted)" : "var(--text)",
          backdropFilter: "blur(10px)",
        }}
      >
        {b.text && <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{b.text}</div>}
        {b.images.length > 0 && (
          <div className="flex gap-2 mt-2 flex-wrap">
            {b.images.map((url, i) =>
              url.startsWith("/api/images/") ? (
                <img
                  key={i}
                  src={tokenUrl(url)}
                  alt=""
                  className="rounded-lg"
                  style={{ height: isSystem ? 64 : 110, maxWidth: 220, objectFit: "cover", cursor: "zoom-in", border: "1px solid var(--line)" }}
                  onClick={() => useHelix.getState().set({ lightbox: url })}
                  title="Open"
                />
              ) : (
                <span key={i} className="text-xs px-2 py-1 rounded-md"
                  style={{ background: "var(--panel-hi)", color: "var(--muted)" }}>
                  🖼 image {b.images.length > 1 ? i + 1 : ""}
                </span>
              ),
            )}
          </div>
        )}
        {b.visuals.map((v, i) => (
          <div key={i} className="mt-2">
            <VisualBlock spec={v} />
          </div>
        ))}
        {b.sources.map((s, i) => (
          <div key={i} className="mt-1.5 text-xs" style={{ color: "var(--muted)" }}>
            {s.line}
          </div>
        ))}
        {b.actions.length > 0 && (
          <div className="flex gap-2 mt-2.5">
            {b.actions.map((a) => (
              <button
                key={a.id}
                className={a.style === "danger" ? "btn btn-danger text-xs" : "btn btn-primary text-xs"}
                onClick={() => fire(a.id, a.label)}
              >
                {a.label}
              </button>
            ))}
          </div>
        )}
        {b.used && (
          <div className="mt-2 text-xs" style={{ color: "var(--muted)" }}>✓ {b.used}</div>
        )}
        <button
          className="absolute -top-2 -right-2 hidden group-hover:block text-xs rounded-md px-1.5 py-0.5"
          style={{ background: "var(--panel-hi)", border: "1px solid var(--line)", color: "var(--muted)" }}
          onClick={copy}
          title="Copy"
        >
          ⧉
        </button>
      </div>
    </div>
  );
}

export default function Talk({ project }: { project?: string } = {}) {
  const navigate = useHelix((s) => s.navigate);
  const bubbles = useHelix((s) => s.bubbles);
  const status = useHelix((s) => s.status);
  const idleLine = useHelix((s) => s.idleLine);
  const busy = useHelix((s) => s.busy);
  const orb = useHelix((s) => s.orb);
  const level = useHelix((s) => s.level);
  const legend = useHelix((s) => s.legend);
  const voice = useHelix((s) => s.voice);
  const suggestion = useHelix((s) => s.suggestion);
  const attachments = useHelix((s) => s.attachments);
  const setAttachments = useHelix((s) => s.setAttachments);
  const keepInput = useHelix((s) => s.keepInput);
  const camera = useHelix((s) => s.camera);
  const cameraLayout = useHelix((s) => s.cameraLayout);
  const attachView = useHelix((s) => s.attachView);
  const cameraCapture = useHelix((s) => s.cameraCapture);

  const [text, setText] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pttRef = useRef<HTMLButtonElement>(null);
  const [ptt, setPtt] = useState(false);
  const docked = Boolean(camera) && cameraLayout === "dock";
  const arFull = Boolean(camera) && cameraLayout === "full";

  // While Hold-to-Talk is held, the mic level fills the gauge ring (scoped to the button).
  useEffect(() => {
    if (!ptt) return;
    let raf = 0;
    const tick = () => {
      pttRef.current?.style.setProperty("--level", String(useHelix.getState().level));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [ptt]);

  useEffect(() => {
    if (keepInput) {
      setText(keepInput);
      useHelix.getState().set({ keepInput: "" });
    }
  }, [keepInput]);

  useEffect(() => {
    const el = scroller.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [bubbles, arFull]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) follow.current = el.scrollTop >= el.scrollHeight - el.clientHeight - 40;
  };

  const send = useCallback(async () => {
    const t = text.trim();
    if (!t && attachments.length === 0) return;
    follow.current = true;
    setText("");
    setAttachments([]);
    const ids = attachments.map((a) => a.id);
    // The camera is open and the view rides along: what you type is about what it sees.
    if (t && camera && attachView && cameraCapture) {
      try {
        const shot = await cameraCapture();
        if (shot) {
          const up = await api.upload(shot.blobs[0], "camera-view.jpg", shot.frame);
          ids.push(up.id);
        }
      } catch {
        /* the message still goes, just without the picture */
      }
    }
    void api.post("/api/shell/submit", { text: t, attachments: ids });
  }, [text, attachments, setAttachments, camera, attachView, cameraCapture]);

  const stop = () => void api.post("/api/shell/stop");

  const attach = async (files: FileList | File[]) => {
    const added: Attachment[] = [];
    for (const f of Array.from(files)) {
      try {
        const res = await api.upload(f);
        const preview = res.image ? URL.createObjectURL(f) : undefined;
        added.push({ ...res, preview });
      } catch {
        /* one bad file doesn't sink the rest */
      }
    }
    setAttachments([...attachments, ...added]);
  };

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      if (document.activeElement === inputRef.current) return;
      const files = Array.from(e.clipboardData?.files || []);
      if (files.length) {
        e.preventDefault();
        void attach(files);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") stop();
    };
    window.addEventListener("paste", onPaste);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("paste", onPaste);
      window.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attachments]);

  // THE TEST LINE: ask for the line word-timed; play it here and hand the face the audio and the
  // word boundaries, so the lips follow the sound. If the backend fell back to its own voice, the
  // face performs on its own clock from the text.
  const sayLine = async (t: string) => {
    try {
      const r = await api.post<{ ok: boolean; spoken: string | boolean; url?: string; words?: { t: number; d: number; w: string }[]; seconds?: number }>("/api/say", { text: t });
      if (r.spoken === "page" && r.url) {
        const audio = new Audio(tokenUrl(r.url));
        audio.preload = "auto";
        window.dispatchEvent(new CustomEvent("helix-say", { detail: { text: t, words: r.words || [], audio } }));
        await audio.play();
      } else {
        window.dispatchEvent(new CustomEvent("helix-say", { detail: { text: t } }));
      }
    } catch {
      window.dispatchEvent(new CustomEvent("helix-say", { detail: { text: t } }));
    }
  };

  const toggleCamera = () => {
    if (camera) void api.post(`/api/camera/${camera.id}/cancel`).catch(() => undefined);
    else void api.post("/api/camera/open").catch(() => undefined);
  };
  // THE CAMERA MENU: photo or a video clip; both open the panel, the hint says which button
  const [camMenu, setCamMenu] = useState(false);
  const [camHint, setCamHint] = useState("");
  const openCamera = (clip: boolean) => {
    setCamMenu(false);
    if (!camera) void api.post("/api/camera/open").catch(() => undefined);
    setCamHint(clip ? "The panel is opening - press ⏺ Clip when it is live." : "The panel is opening - press Snap when it is live.");
    window.setTimeout(() => setCamHint(""), 6000);
  };
  // TELL A JOKE: a line from the web (filtered), said out loud by the voice, acted by the face
  const [joke, setJoke] = useState<{ text: string; busy: boolean } | null>(null);
  const tellJoke = async () => {
    setJoke({ text: "", busy: true });
    try {
      const r = await api.get<{ joke: string; source: string }>("/api/joke");
      setJoke({ text: r.joke, busy: false });
      await sayLine(r.joke);
      window.setTimeout(() => setJoke(null), Math.min(20000, 3000 + r.joke.length * 60));
    } catch (e) { setJoke({ text: (e as Error).message || "No joke came back.", busy: false }); window.setTimeout(() => setJoke(null), 4000); }
  };

  const statusLine = busy
    ? status
    : STATE_LINES[orb] ?? ((/Connect Claude in Settings/i.test(status) ? "" : status) || idleLine);


  return (
    <div
      className={`h-full flex flex-col items-center pt-12 pb-11 px-6 ${docked ? "console-docked" : ""}`}
      style={{ pointerEvents: "none", justifyContent: arFull ? "flex-end" : undefined }}
    >
      {/* where this conversation sits, and the way back to the Console */}
      <div className="w-full max-w-[900px] flex items-center gap-2 pb-2" style={{ pointerEvents: "auto" }}>
        <button className="glass rounded-full px-3 py-1 text-xs" title="Back to the Console" onClick={() => navigate({ name: "console" })}>◂ Console</button>
        {project && (
          <span className="glass rounded-full px-3 py-1 text-xs" style={{ color: "var(--cyan)", letterSpacing: 1 }}>
            DEV · {project}
          </span>
        )}
        {project && <span className="text-xs" style={{ color: "var(--muted)" }}>conversation scoped to this project comes with the deploy lane</span>}
      </div>
      {/* legend strip */}
      {legend.length > 0 && !arFull && (
        <div className="w-full max-w-[900px] overflow-x-auto flex gap-2 pb-1"
          style={{ pointerEvents: "auto" }}>
          {legend.map((item) => (
            <button
              key={item.slug}
              className="glass rounded-full px-3 py-1 text-xs elide max-w-[220px] shrink-0"
              style={{
                color: item.state === "building" ? "var(--working)"
                  : item.state === "done" ? "var(--done)" : "var(--error)",
              }}
              title={`${item.name} — ${item.state === "building" ? "in progress" : item.state}. Click to open it.`}
              onClick={() => window.dispatchEvent(new CustomEvent("helix-open-build", {
                detail: { slug: item.slug, name: item.name },
              }))}
            >
              ● {item.name}
            </button>
          ))}
        </div>
      )}

      {/* transcript — steps aside while the camera fills the window */}
      {!arFull && (
        <div
          ref={scroller}
          onScroll={onScroll}
          className="flex-1 w-full max-w-[820px] overflow-y-auto flex flex-col gap-3 py-4 px-2"
          style={{ pointerEvents: "auto" }}
        >
          {bubbles.map((b, i) => (
            <BubbleView key={b.id} b={b} idx={i} />
          ))}
        </div>
      )}

      {/* sleep-talk — the dreaming star's murmurs, above the pill */}
      {!arFull && <SleepTalk />}

      {/* status pill — a plasma conduit carrying the star's color; the busy arc IS the spinner */}
      <div
        className={`pill-conduit ${busy ? "busy" : ""} rounded-full px-5 py-1.5 text-[13px] elide max-w-[760px] mb-3`}
        style={{
          background: "rgba(8,11,15,0.92)",
          color: "var(--muted)",
          pointerEvents: "auto",
        }}
        title={statusLine.length > 40 ? statusLine : undefined}
      >
        {arFull && bubbles.length > 0 && bubbles[bubbles.length - 1].role === "helix"
          ? `${bubbles[bubbles.length - 1].text.slice(0, 160)}${bubbles[bubbles.length - 1].text.length > 160 ? "…" : ""}`
          : joke ? (joke.busy ? <StateSign state="thinking" level={0} /> : <span className="joke-line">{joke.text}</span>)
          : camHint ? camHint
          : !busy && STATE_LINES[orb] ? <StateSign state={orb} level={level} />
          : statusLine}
      </div>


      {/* suggestion chip */}
      {suggestion && !arFull && (
        <div className="glass rounded-xl px-4 py-2 mb-3 flex items-center gap-3 text-[13px]"
          style={{ pointerEvents: "auto" }}>
          <span>💡 {suggestion.text}</span>
          {suggestion.slug && (
            <button className="btn-nav" style={{ color: "var(--cyan)" }}
              onClick={() => {
                window.dispatchEvent(new CustomEvent("helix-open-build", {
                  detail: { slug: suggestion.slug, name: suggestion.text },
                }));
                void api.post("/api/shell/suggest_dismiss", { id: suggestion.id });
              }}>
              Open
            </button>
          )}
          <button className="btn-nav"
            onClick={() => void api.post("/api/shell/suggest_dismiss", { id: suggestion.id })}>
            ✕
          </button>
        </div>
      )}

      {/* attachment chips */}
      {attachments.length > 0 && (
        <div className="flex gap-2 mb-2 flex-wrap max-w-[820px]" style={{ pointerEvents: "auto" }}>
          {attachments.map((a) => (
            <span key={a.id} className={`attach-chip glass rounded-lg px-2.5 py-1 text-xs flex items-center gap-2${a.preview ? " has-img" : ""}`}>
              {a.preview ? (
                <img src={a.preview} alt="" className="attach-thumb" />
              ) : (
                <span className="attach-doc">📄</span>
              )}
              <span className="elide max-w-[160px]">{a.name}</span>
              <button className="btn-nav px-1"
                onClick={() => setAttachments(attachments.filter((x) => x.id !== a.id))}>
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      {/* input row */}
      <div className="talk-bar w-full max-w-[820px] flex items-end gap-2" style={{ pointerEvents: "auto" }}>
        {voice?.supported && (
          <button
            ref={pttRef}
            className={`mic ${voice.enabled && voice.listening && !voice.muted ? "live" : voice.enabled ? "asleep" : "off"} ${ptt ? "held" : ""} ${orb === "listening" || orb === "transcribing" ? "hearing" : ""}`}
            title={!voice.enabled ? "Voice is off - click to turn it on" : voice.muted ? "Asleep - click to wake" : "Listening for the wake word. Hold to talk right now."}
            onClick={() => { if (!ptt) void api.post("/api/shell/voice", { op: !voice.enabled ? "toggle" : voice.muted ? "wake" : "sleep" }); }}
            onMouseDown={(e) => {
              if (!(voice.enabled && voice.listening) || busy) return;
              e.preventDefault();
              pttRef.current?.setAttribute("data-held", "1");
              window.setTimeout(() => { if (pttRef.current?.getAttribute("data-held")) { setPtt(true); void api.post("/api/shell/voice", { op: "ptt_start" }); } }, 180);
            }}
            onMouseUp={() => { pttRef.current?.removeAttribute("data-held"); if (ptt) { setPtt(false); void api.post("/api/shell/voice", { op: "ptt_stop" }); } }}
            onMouseLeave={() => { pttRef.current?.removeAttribute("data-held"); if (ptt) { setPtt(false); void api.post("/api/shell/voice", { op: "ptt_stop" }); } }}
          >
            <i className="ring r1" /><i className="ring r2" /><i className="ring r3" />
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z"/></svg>
            <span className="lvl" />
          </button>
        )}
        <label className="talk-ic shrink-0" data-tip="Attach files - or drop them on the box, or paste">
          {ICONS.clip}
          <input
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) void attach(e.target.files);
              e.target.value = "";
            }}
          />
        </label>
        <span className="relative shrink-0">
          <button
            className={`talk-ic${camera ? " on" : ""}`}
            data-tip={camera ? "Close the camera" : "The camera: a photo, or a video clip"}
            onClick={() => (camera ? toggleCamera() : setCamMenu((m) => !m))}
          >
            {ICONS.camera}
          </button>
          {camMenu && !camera && (
            <div className="talk-menu" onMouseLeave={() => setCamMenu(false)}>
              <button onClick={() => openCamera(false)}>{ICONS.camera}<span><b>Photo</b><small>one frame, ask about it</small></span></button>
              <button onClick={() => openCamera(true)}>{ICONS.video}<span><b>Video clip</b><small>a few seconds, read in order</small></span></button>
            </div>
          )}
        </span>
        <textarea
          ref={inputRef}
          value={text}
          rows={Math.min(6, Math.max(1, text.split("\n").length))}
          placeholder={camera && attachView && cameraCapture ? "Ask about what the camera sees…" : "Talk to HELIX…"}
          className="flex-1 resize-none"
          style={{ background: "rgba(13,20,27,0.85)" }}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
              e.preventDefault();
              void send();
            }
          }}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData.files);
            if (files.length) {
              e.preventDefault();
              void attach(files);
            }
          }}
          onDrop={(e) => {
            e.preventDefault();
            if (e.dataTransfer.files.length) void attach(e.dataTransfer.files);
          }}
          onDragOver={(e) => e.preventDefault()}
        />
        {busy && (
          <button className="btn shrink-0" style={{ borderColor: "#e0663f", color: "#e0663f" }}
            onClick={stop}>
            ■ Stop
          </button>
        )}
        <button className={`talk-ic joke${joke?.busy ? " busy" : ""}`} data-tip="Tell a joke - the voice says one, the face acts it"
          disabled={Boolean(joke?.busy)} onClick={() => void tellJoke()}>
          {ICONS.joke}
        </button>
        <button className="talk-say shrink-0" data-tip="HELIX says exactly this, out loud, no model involved - a test line for the face"
          disabled={!text.trim()}
          onClick={() => { const t = text.trim(); if (!t) return; setText(""); void sayLine(t); }}>
          {ICONS.say}<span>Say</span>
        </button>
        <button className="btn btn-primary shrink-0" onClick={() => void send()}>
          Send
        </button>
      </div>
    </div>
  );
}

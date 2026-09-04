// The camera window — getUserMedia in the browser (a genuine upgrade over the Qt path): mirrored
// live preview, a camera picker, capture by button or by voice (the backend's camera session sends
// camera.capture when it hears "take the picture"). The captured frame is UN-mirrored (drawn
// straight from the stream) so printed markings read correctly; nothing touches disk here.
//
// It NEVER closes itself on a camera error. The old behaviour posted /cancel the instant
// getUserMedia threw — so a pending permission prompt, a stale saved device id, or a momentarily
// busy webcam made the window "open and close really quick". Now a failure keeps the window open
// with a plain reason and a Retry button, and the only ways out are the user's Cancel/Esc or a
// successful capture.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

export default function CameraModal({
  modal,
}: {
  modal: { id: string; prompt: string; ears: boolean; manual?: boolean };
}) {
  const shutter = useHelix((s) => s.cameraShutter);
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState(() => localStorage.getItem("helix_camera") || "");
  const [status, setStatus] = useState("Waking the camera…");
  const [error, setError] = useState("");
  const [haveFrame, setHaveFrame] = useState(false);
  const pendingCapture = useRef(false);
  const lastShutter = useRef(shutter);

  // Fully release the previous stream BEFORE asking for a new one. The webcam is a single-holder
  // device: on a quick close→reopen the browser can still consider it busy for a beat.
  const releaseStream = () => {
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    if (video.current) video.current.srcObject = null;
  };

  const listDevices = async () => {
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      setDevices(all.filter((d) => d.kind === "videoinput"));
    } catch {
      /* enumeration can fail before any permission — the picker just stays hidden */
    }
  };

  // Try the requested camera; if a SPECIFIC (saved) device id is stale — gone, or over-constrained —
  // fall back to "any camera" once and forget the bad id, so a remembered camera that no longer
  // exists can never lock the user out. A busy device gets a couple of quiet retries. Permission
  // and no-camera errors are surfaced as-is (retrying those just spams).
  const getStream = async (id: string): Promise<MediaStream> => {
    const ask = (v: MediaTrackConstraints | boolean) =>
      navigator.mediaDevices.getUserMedia({ video: v, audio: false });
    try {
      return await ask(id ? { deviceId: { exact: id } } : true);
    } catch (e) {
      const name = (e as DOMException)?.name || "";
      if (id && (name === "OverconstrainedError" || name === "NotFoundError")) {
        localStorage.removeItem("helix_camera");
        setDeviceId("");
        return await ask(true); // any camera
      }
      if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError") {
        for (let attempt = 0; attempt < 2; attempt++) {
          await new Promise((r) => setTimeout(r, 350));
          try {
            return await ask(id ? { deviceId: { exact: id } } : true);
          } catch {
            /* keep trying, then fall through to throw */
          }
        }
      }
      throw e;
    }
  };

  const reason = (e: unknown): string => {
    const name = (e as DOMException)?.name || "";
    if (name === "NotAllowedError" || name === "SecurityError")
      return "Camera access is blocked. Allow the camera for this page (the address-bar camera icon), then Retry.";
    if (name === "NotFoundError" || name === "OverconstrainedError")
      return "No camera found. Plug one in or pick another below, then Retry.";
    if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError")
      return "The camera is in use by another app (Zoom, Teams, another tab). Close it, then Retry.";
    return "The camera wouldn't start. Check it's connected and allowed, then Retry.";
  };

  const open = async (id: string) => {
    releaseStream();
    setHaveFrame(false);
    setError("");
    setStatus("Waking the camera…");
    try {
      const s = await getStream(id);
      stream.current = s;
      if (video.current) {
        video.current.srcObject = s;
        await video.current.play();
        setHaveFrame(true);
        setStatus("");
        if (pendingCapture.current) {
          pendingCapture.current = false;
          capture();
        }
      }
      await listDevices(); // labels are populated now that permission is granted
    } catch (e) {
      // DO NOT close the window — let the user fix it and Retry.
      setStatus("");
      setError(reason(e));
      void listDevices(); // still offer the picker so they can switch cameras
    }
  };

  useEffect(() => {
    void open(deviceId);
    return () => releaseStream();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const capture = () => {
    const v = video.current;
    if (!v || !haveFrame) {
      pendingCapture.current = true;
      setStatus("One moment — the camera is still waking…");
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    canvas.getContext("2d")!.drawImage(v, 0, 0); // un-mirrored: markings read correctly
    canvas.toBlob((blob) => {
      if (blob) {
        void api.sendFrame(modal.id, blob);
        if (deviceId) localStorage.setItem("helix_camera", deviceId);
      }
    }, "image/png");
  };

  // The backend heard "take the picture" — snap now.
  useEffect(() => {
    if (shutter !== lastShutter.current) {
      lastShutter.current = shutter;
      capture();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shutter]);

  const cancel = () => void api.post(`/api/camera/${modal.id}/cancel`);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && cancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="fixed inset-0 flex items-center justify-center" style={{ zIndex: 50, background: "rgba(3,6,9,0.7)" }}>
      <div className="glass-hi rounded-2xl p-5 w-[600px] fade-up">
        <div className="font-display text-[15px] mb-1" style={{ color: "var(--cyan)" }}>SHOW ME</div>
        <div className="text-[13px] mb-3" style={{ color: "var(--text)" }}>
          {modal.prompt || "Hold it up to the camera — take your time."}
        </div>
        <video
          ref={video}
          muted
          playsInline
          className="w-full rounded-xl"
          style={{ background: "#05080b", border: "1px solid #1b2730", transform: "scaleX(-1)", aspectRatio: "4/3" }}
        />
        {devices.length > 1 && (
          <select
            className="mt-3 w-full"
            value={deviceId}
            onChange={(e) => {
              pendingCapture.current = false; // the spoken word described the OLD camera
              setDeviceId(e.target.value);
              void open(e.target.value);
            }}
          >
            <option value="">Default camera</option>
            {devices.map((d) => (
              <option key={d.deviceId} value={d.deviceId}>{d.label || "Camera"}</option>
            ))}
          </select>
        )}
        {error ? (
          <div className="text-xs mt-3" style={{ color: "var(--amber, #e0a13f)" }}>{error}</div>
        ) : (
          <div className="text-xs mt-3" style={{ color: "var(--muted)" }}>
            {status ||
              (modal.ears
                ? "No rush — I'm listening. Say 'take the picture' when you're ready, or 'cancel' to close without one. The buttons work too."
                : "No rush — take the picture with the button when you're ready; Cancel (or Esc) closes without one.")}
          </div>
        )}
        <div className="flex gap-3 mt-4 justify-end">
          <button className="btn" onClick={cancel}>Cancel</button>
          {error ? (
            <button className="btn btn-primary" onClick={() => void open(deviceId)}>Retry</button>
          ) : (
            <button className="btn btn-primary" onClick={capture}>Take the picture</button>
          )}
        </div>
      </div>
    </div>
  );
}

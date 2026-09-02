// The camera window — getUserMedia in the browser (a genuine upgrade over the Qt path): mirrored
// live preview, a camera picker, capture by button or by voice (the backend's camera session sends
// camera.capture when it hears "take the picture"). The captured frame is UN-mirrored (drawn
// straight from the stream) so printed markings read correctly; nothing touches disk here.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useHelix } from "../lib/store";

export default function CameraModal({ modal }: { modal: { id: string; prompt: string; ears: boolean } }) {
  const shutter = useHelix((s) => s.cameraShutter);
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState(() => localStorage.getItem("helix_camera") || "");
  const [status, setStatus] = useState("Waking the camera…");
  const [haveFrame, setHaveFrame] = useState(false);
  const pendingCapture = useRef(false);
  const lastShutter = useRef(shutter);

  const open = async (id: string) => {
    stream.current?.getTracks().forEach((t) => t.stop());
    setHaveFrame(false);
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: id ? { deviceId: { exact: id } } : true,
        audio: false,
      });
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
      const all = await navigator.mediaDevices.enumerateDevices();
      setDevices(all.filter((d) => d.kind === "videoinput"));
    } catch {
      setStatus("The camera wouldn't start — another app may be using it, or camera access is off.");
      void api.post(`/api/camera/${modal.id}/cancel`);
    }
  };

  useEffect(() => {
    void open(deviceId);
    return () => stream.current?.getTracks().forEach((t) => t.stop());
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
        <div className="text-xs mt-3" style={{ color: "var(--muted)" }}>
          {status ||
            (modal.ears
              ? "No rush — I'm listening. Say 'take the picture' when you're ready, or 'cancel' to close without one. The buttons work too."
              : "No rush — take the picture with the button when you're ready; Cancel (or Esc) closes without one.")}
        </div>
        <div className="flex gap-3 mt-4 justify-end">
          <button className="btn" onClick={cancel}>Cancel</button>
          <button className="btn btn-primary" onClick={capture}>Take the picture</button>
        </div>
      </div>
    </div>
  );
}

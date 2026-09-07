// The one API/WS client. Token: the launch URL carries ?t=<token>; we stash it and scrub the URL.

let token = "";

export function initToken(): void {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("t");
  if (fromUrl) {
    token = fromUrl;
    sessionStorage.setItem("helix_token", fromUrl);
    url.searchParams.delete("t");
    window.history.replaceState({}, "", url.pathname + url.hash);
  } else {
    token = sessionStorage.getItem("helix_token") || "";
  }
}

export function getToken(): string {
  return token;
}

/** A URL the browser can fetch WITHOUT headers (an <img>, a download): the token rides as ?t=. */
export function tokenUrl(path: string): string {
  return `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(token)}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      "X-Helix-Token": token,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      detail = data.error || detail;
    } catch {
      /* body wasn't JSON */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export interface FramesMeta {
  rid?: string; // answers the model's look with this capture id
  caption?: string; // the user's question typed alongside their own shot
  mode?: "still" | "clip";
  seconds?: number;
  frame?: string; // the panel's frame id (AR callouts anchor to it)
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),

  /** Attach a file (or a blob with a name). `frame`: set when the blob IS the live camera view. */
  async upload(file: Blob, name?: string, frame?: string): Promise<{ id: string; name: string; image: boolean }> {
    const form = new FormData();
    form.append("file", file, name || (file instanceof File ? file.name : "image.png"));
    if (frame) form.append("frame", frame);
    const res = await fetch("/api/attachments", {
      method: "POST",
      headers: { "X-Helix-Token": token },
      body: form,
    });
    if (!res.ok) throw new Error("upload failed");
    return res.json();
  },

  async uploadMany(path: string, files: File[]): Promise<unknown> {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    const res = await fetch(path, {
      method: "POST",
      headers: { "X-Helix-Token": token },
      body: form,
    });
    if (!res.ok) throw new Error("upload failed");
    return res.json();
  },

  /** Frames from the camera panel — a still (one blob) or a clip (several, in time order). */
  async sendFrames(camId: string, blobs: Blob[], meta: FramesMeta = {}): Promise<boolean> {
    const form = new FormData();
    blobs.forEach((b, i) => form.append("frames", b, `frame-${i + 1}.jpg`));
    if (meta.rid) form.append("rid", meta.rid);
    if (meta.caption) form.append("caption", meta.caption);
    form.append("mode", meta.mode || (blobs.length > 1 ? "clip" : "still"));
    form.append("seconds", String(meta.seconds ?? 0));
    if (meta.frame) form.append("frame", meta.frame);
    const res = await fetch(`/api/camera/${camId}/frames`, {
      method: "POST",
      headers: { "X-Helix-Token": token },
      body: form,
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { ok?: boolean };
    return Boolean(data.ok);
  },
};

// ----- the event stream -----
export type WsHandler = (event: Record<string, unknown>) => void;

export function connectEvents(onEvent: WsHandler, onOpen?: () => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 800;

  const open = () => {
    if (closed) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/ws?t=${encodeURIComponent(token)}`);
    ws.onopen = () => {
      retry = 800;
      onOpen?.();
    };
    ws.onmessage = (msg) => {
      try {
        onEvent(JSON.parse(msg.data));
      } catch {
        /* malformed frame — drop it */
      }
    };
    ws.onclose = () => {
      if (!closed) setTimeout(open, retry = Math.min(retry * 1.5, 8000));
    };
    ws.onerror = () => ws?.close();
  };
  open();
  const ping = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
  return () => {
    closed = true;
    clearInterval(ping);
    ws?.close();
  };
}

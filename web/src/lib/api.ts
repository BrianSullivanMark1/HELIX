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

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),

  async upload(file: File): Promise<{ id: string; name: string; image: boolean }> {
    const form = new FormData();
    form.append("file", file, file.name);
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

  async sendFrame(camId: string, blob: Blob): Promise<void> {
    await fetch(`/api/camera/${camId}/frame`, {
      method: "POST",
      headers: { "X-Helix-Token": token, "Content-Type": "image/png" },
      body: blob,
    });
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

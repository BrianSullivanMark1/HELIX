import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Dev flow: `python main.py web --headless` serves the backend on 8737 (it prints the token URL);
// `npm run dev` fronts it here with hot reload — open http://localhost:5173/?t=<token>.
// The build stamp: shown in the menu and Settings -> Updates so "which build is this window
// running" has a one-glance answer (an old index.html cached by the window shows an old stamp).
const stamp = new Date().toISOString().slice(0, 16).replace("T", " ") + "Z";

export default defineConfig({
  define: { __HELIX_BUILD__: JSON.stringify(stamp) },
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8737", changeOrigin: true },
      "/builds": { target: "http://127.0.0.1:8737", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8737", ws: true, changeOrigin: true },
    },
  },
  build: { chunkSizeWarningLimit: 1600 },
});

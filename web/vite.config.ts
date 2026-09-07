import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Dev flow: `python main.py web --headless` serves the backend on 8737 (it prints the token URL);
// `npm run dev` fronts it here with hot reload — open http://localhost:5173/?t=<token>.
export default defineConfig({
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

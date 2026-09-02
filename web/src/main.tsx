import { createRoot } from "react-dom/client";
import App from "./App";
import { initToken } from "./lib/api";
import "./theme.css";

initToken();

// No StrictMode: its deliberate double-mount tears down and recreates every WebGL context, and the
// second R3F canvas (the Studio viewer) came back from that dance permanently blank. The suite of
// behaviors StrictMode guards is covered by the backend owning all real state.
createRoot(document.getElementById("root")!).render(<App />);

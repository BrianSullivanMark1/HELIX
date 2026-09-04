import { createRoot } from "react-dom/client";
import App from "./App";
import { initToken } from "./lib/api";
import { applyEvent, useHelix } from "./lib/store";
import "./theme.css";

initToken();

// A debugging seam: feed the store an event by hand from the console (`helixApply({t:"toast",text:"hi"})`).
(window as unknown as { helixApply: typeof applyEvent; helixStore: typeof useHelix }).helixApply = applyEvent;
(window as unknown as { helixStore: typeof useHelix }).helixStore = useHelix;

// No StrictMode: its deliberate double-mount tears down and recreates every WebGL context, and the
// second R3F canvas (the Studio viewer) came back from that dance permanently blank. The suite of
// behaviors StrictMode guards is covered by the backend owning all real state.
createRoot(document.getElementById("root")!).render(<App />);

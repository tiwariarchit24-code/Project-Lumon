import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

// Global styles. MapLibre's own stylesheet is loaded first so that our
// theme can adjust it afterwards.
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles/theme.css";
import "./styles/layout.css";
import "./styles/map.css";
import "./styles/panels.css";
import "./styles/boot.css";

// Entry point: find the <div id="root"> in index.html and draw the app in it.
const rootElement = document.getElementById("root");

if (rootElement) {
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

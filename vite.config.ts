import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite configuration for Project Lumon.
// The React plugin gives us JSX support and fast refresh during development.
// Nothing here talks to the internet: the app is built and served locally.
//
// /api requests are forwarded to the local Lumon API (FastAPI on port 8000),
// so the browser only ever talks to this machine.
const API = "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": API },
    // Allow access through a Cloudflare Quick Tunnel (*.trycloudflare.com).
    // A leading dot allows the domain and all of its subdomains.
    allowedHosts: [".trycloudflare.com"],
  },
  preview: {
    proxy: { "/api": API },
  },
  worker: {
    // MapLibre's background worker is an ES module, so build workers as ES
    // modules too (see src/utils/maplibreWorker.ts).
    format: "es",
  },
  build: {
    // MapLibre GL is a single ~1 MB library, so the default 500 kB warning
    // would always fire. This limit still warns if the bundle grows a lot.
    chunkSizeWarningLimit: 1500,
  },
});

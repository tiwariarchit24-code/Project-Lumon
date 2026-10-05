import { setWorkerUrl } from "maplibre-gl";

// MapLibre does its heavy lifting (preparing map data) in a background
// "web worker" script. By default it looks for that script next to its own
// file, but after Vite bundles the app the file is no longer there, and the
// map fails to draw.
//
// The "?worker&url" suffix asks Vite to bundle MapLibre's worker script
// (together with the code it imports) into its own local file and give us
// that file's URL. We then tell MapLibre where to find it. Everything stays
// local: no worker is loaded from the internet.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

setWorkerUrl(workerUrl);

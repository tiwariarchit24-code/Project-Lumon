// ---------------------------------------------------------------------------
// UI smoke test: opens LUMON COMMAND in a headless Chrome and checks that it
// renders without errors at the two target workstation sizes.
//
// Uses the Chrome DevTools protocol over Node's built-in WebSocket, so no
// npm packages are needed. Requires Google Chrome and a running app:
//   npm run api        (terminal 1)
//   npm run dev        (terminal 2)
//   node scripts/ui-smoke.mjs [http://localhost:5173/]
//
// Screenshots are written to data/qa/ (ignored by Git).
// Set CHROME=/path/to/chrome if Chrome is not in the default macOS location.
// ---------------------------------------------------------------------------

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const url = process.argv[2] ?? "http://localhost:5173/";
const chromePath = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const port = 9555;
const outDir = "data/qa";
mkdirSync(outDir, { recursive: true });

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Start Chrome with a throw-away profile inside data/qa (deleted at the end),
// so nothing outside the project folder is touched.
const profile = join(outDir, "chrome-profile");
const chrome = spawn(chromePath, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run", "about:blank"], { stdio: "ignore" });
await wait(1500);

// Check one window size: load, wait, collect errors and layout facts, screenshot.
async function checkSize(width, height) {
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const errors = [];
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    pending.set(id, resolve);
    socket.send(JSON.stringify({ id, method, params }));
  });
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.id && pending.has(data.id)) {
      pending.get(data.id)(data.result);
      pending.delete(data.id);
    }
    if (data.method === "Runtime.exceptionThrown") errors.push(data.params.exceptionDetails.exception?.description ?? "exception");
    if (data.method === "Runtime.consoleAPICalled" && data.params.type === "error") errors.push(data.params.args.map((a) => a.value ?? a.description).join(" "));
  };
  await new Promise((resolve) => (socket.onopen = resolve));
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  await wait(5000);
  const facts = await send("Runtime.evaluate", {
    returnByValue: true,
    expression: `({
      horizontalScroll: document.documentElement.scrollWidth > window.innerWidth,
      topbar: !!document.querySelector('.topbar'),
      map: !!document.querySelector('.maplibregl-canvas'),
      timeline: !!document.querySelector('.timeline'),
      intel: !!document.querySelector('.intel'),
      apiOnline: !document.querySelector('.offline-banner'),
      logo: !!document.querySelector('.logo__image'),
    })`,
  });
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, `smoke-${width}x${height}.png`), Buffer.from(shot.data, "base64"));
  socket.close();
  return { size: `${width}x${height}`, ...facts.result.value, errors };
}

let failed = false;
for (const [width, height] of [[1440, 900], [1280, 800]]) {
  const result = await checkSize(width, height);
  const ok = !result.horizontalScroll && result.topbar && result.map && result.timeline && result.intel && result.errors.length === 0;
  failed ||= !ok;
  console.log(`${ok ? "PASS" : "FAIL"} ${result.size}`, JSON.stringify(result));
}
chrome.kill();
await wait(500);
rmSync(profile, { recursive: true, force: true });
process.exit(failed ? 1 : 0);

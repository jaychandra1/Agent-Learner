// Builds the static site into dist/: copies the files and writes config.js.
//
//   BACKEND_URL=https://your-pq.onrender.com npm run build     -> publish the dist/ folder
//
// On Vercel the backend's address is NOT written into the site. The page calls its own
// address and api/chat.js passes the question on, reading BACKEND_URL on the server, so
// visitors never see where the backend is. On a host without functions (a plain static
// site) the page has to call the backend itself and config.js then holds the address;
// set PQ_PROXY=1 if that host forwards /api/chat to the backend on its own.
//
// No dependencies: needs only Node.js 18 or newer, which every static host provides.
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");
let backend = (process.env.BACKEND_URL || "").trim().replace(/\/+$/, "");

// "my-pq.onrender.com" is taken as "https://my-pq.onrender.com"
if (backend && !/^https?:\/\//i.test(backend)) {
  backend = "https://" + backend;
  console.warn('BACKEND_URL has no "https://" in front: added it.');
}
if (backend && !/^https?:\/\/[^\s/.]+(\.[^\s/.]+)+(:\d+)?(\/\S*)?$|^https?:\/\/localhost(:\d+)?$/i.test(backend)) {
  console.error("BACKEND_URL is not a valid address. Use the form https://your-pq.onrender.com");
  process.exit(1);
}

// Vercel sets VERCEL=1 during a build: questions go through api/chat.js there
const proxied = Boolean(process.env.VERCEL) || ["1", "true", "yes"].includes((process.env.PQ_PROXY || "").toLowerCase());
if (proxied && !backend) {
  console.warn("BACKEND_URL is not set: the site will build, but questions fail until it is set and redeployed.");
}

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist);
for (const file of ["index.html", "styles.css", "app.js", "markdown.js"]) {
  cpSync(join(here, file), join(dist, file));
}
writeFileSync(join(dist, "config.js"),
  "// Written by build.mjs from the BACKEND_URL environment variable.\n" +
  `window.PQ_CONFIG = { BACKEND_URL: ${JSON.stringify(proxied ? "" : backend)} };\n`);

console.log(proxied
  ? "Built dist/: the page calls its own address and the host passes questions on. The backend's address is not in the site."
  : backend
    ? "Built dist/: the page calls the backend directly, so its address is in config.js."
    : "Built dist/ without BACKEND_URL: the page will call its own address (/api/chat).");

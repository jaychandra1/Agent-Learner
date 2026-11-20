// Builds the static site into dist/: copies the files and writes config.js with the
// backend's address from the BACKEND_URL environment variable of the hosting service.
//
//   BACKEND_URL=https://your-pq.onrender.com npm run build     -> publish the dist/ folder
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
  console.warn(`BACKEND_URL has no "https://" in front: using ${backend}`);
}
if (backend && !/^https?:\/\/[^\s/.]+(\.[^\s/.]+)+(:\d+)?(\/\S*)?$|^https?:\/\/localhost(:\d+)?$/i.test(backend)) {
  console.error(`BACKEND_URL must be an address such as https://your-pq.onrender.com, not "${process.env.BACKEND_URL}"`);
  process.exit(1);
}

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist);
for (const file of ["index.html", "styles.css", "app.js", "markdown.js"]) {
  cpSync(join(here, file), join(dist, file));
}
writeFileSync(join(dist, "config.js"),
  "// Written by build.mjs from the BACKEND_URL environment variable.\n" +
  `window.PQ_CONFIG = { BACKEND_URL: ${JSON.stringify(backend)} };\n`);

console.log(backend
  ? `Built dist/ for the backend ${backend}`
  : "Built dist/ without BACKEND_URL: people must enter the backend's address under Settings.");

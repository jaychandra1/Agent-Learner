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
const backend = (process.env.BACKEND_URL || "").trim().replace(/\/+$/, "");

if (backend && !/^https?:\/\/[^\s/]+/i.test(backend)) {
  console.error(`BACKEND_URL must be a full address such as https://your-pq.onrender.com, not "${backend}"`);
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

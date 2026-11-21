// Server-side proxy for hosts that run functions next to the static site (Vercel).
//
// The page calls its own address (/api/chat); this function passes the question on to the
// PQ backend named in the BACKEND_URL environment variable. The backend's address is read
// here, on the server, so it never reaches the browser: not in the page, not in its
// scripts, not in the network tab.
export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ detail: "Method not allowed." });
  }

  let backend = (process.env.BACKEND_URL || "").trim().replace(/\/+$/, "");
  if (backend && !/^https?:\/\//i.test(backend)) backend = "https://" + backend;
  if (!backend) {
    console.error("BACKEND_URL is not set for this deployment");
    return res.status(503).json({ detail: "PQ is not connected yet. Please tell the administrator." });
  }

  // the person's address, so the backend's rate limit counts people and not this proxy
  const client = String(req.headers["x-forwarded-for"] || "").split(",")[0].trim();
  const headers = { "Content-Type": "application/json" };
  if (req.headers["x-access-code"]) headers["X-Access-Code"] = String(req.headers["x-access-code"]);
  if (client) headers["X-Forwarded-For"] = client;

  try {
    const body = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    const upstream = await fetch(backend + "/api/chat", { method: "POST", headers, body });
    const text = await upstream.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: "PQ answered with an error (" + upstream.status + ")." };  // e.g. a platform's HTML error page
    }
    return res.status(upstream.status).json(data);
  } catch (error) {
    console.error("PQ backend not reachable:", error && error.name);   // never the address
    return res.status(502).json({ detail: "PQ is not reachable right now. A sleeping service can take a minute " +
                                          "to wake up; please try again." });
  }
}

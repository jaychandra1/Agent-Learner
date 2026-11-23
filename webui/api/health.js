// Wakes the PQ backend (see chat.js for the idea of these proxies).
//
// The page calls this when it opens, and now and then while it stays open. A backend on a
// plan that sleeps when idle then starts while the person is still typing, instead of
// making the first question wait for it. The backend's address stays on the server.
export default async function handler(req, res) {
  let backend = (process.env.BACKEND_URL || "").trim().replace(/\/+$/, "");
  if (backend && !/^https?:\/\//i.test(backend)) backend = "https://" + backend;
  res.setHeader("Cache-Control", "no-store");
  if (!backend) return res.status(503).json({ status: "not connected" });
  try {
    const upstream = await fetch(backend + "/api/health", { signal: AbortSignal.timeout(90000) });
    const data = await upstream.json();
    // only what the page may know: whether PQ is up and whether it wants an access code
    return res.status(upstream.ok ? 200 : 502).json({
      status: upstream.ok ? "ok" : "down",
      access_code_required: Boolean(data && data.access_code_required),
    });
  } catch (error) {
    return res.status(502).json({ status: "down" });
  }
}

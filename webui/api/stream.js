// Server-side proxy for the streamed answer (see chat.js for the idea).
//
// The page calls its own address (/api/stream). This function passes the question on to the
// PQ backend named in BACKEND_URL and hands the answer back piece by piece, as the backend
// sends it. The backend's address never reaches the browser.
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
  const headers = { "Content-Type": "application/json", Accept: "text/event-stream" };
  if (req.headers["x-access-code"]) headers["X-Access-Code"] = String(req.headers["x-access-code"]);
  if (client) headers["X-Forwarded-For"] = client;

  // when the person presses Stop or closes the page, stop the backend too
  const abort = new AbortController();
  res.on("close", () => abort.abort());

  let upstream;
  try {
    const body = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
    upstream = await fetch(backend + "/api/stream", { method: "POST", headers, body, signal: abort.signal });
  } catch (error) {
    if (abort.signal.aborted) return res.end();
    console.error("PQ backend not reachable:", error && error.name);   // never the address
    return res.status(502).json({ detail: "PQ is not reachable right now. A sleeping service can take a minute " +
                                          "to wake up; please try again." });
  }

  const type = upstream.headers.get("content-type") || "";
  if (!upstream.ok || !type.includes("text/event-stream") || !upstream.body) {
    // an ordinary error reply (wrong access code, too many questions, an older backend, ...)
    let data;
    try {
      data = JSON.parse(await upstream.text());
    } catch {
      data = { detail: "PQ answered with an error (" + upstream.status + ")." };
    }
    return res.status(upstream.ok ? 502 : upstream.status).json(data);
  }

  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
  });
  try {
    for await (const chunk of upstream.body) res.write(chunk);
  } catch (error) {
    if (!abort.signal.aborted) {
      console.error("PQ stream broke:", error && error.name);
      res.write('data: {"error": "The connection to PQ was lost. Please try again."}\n\n');
    }
  }
  res.end();
}

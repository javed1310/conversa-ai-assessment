import { randomUUID } from "node:crypto";

export const dynamic = "force-dynamic";

async function proxy(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const requestId = randomUUID();
  try {
    const origin = new URL(process.env.ASSESSMENT_API_ORIGIN ?? "http://127.0.0.1:4000");
    if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) {
      throw new Error("Invalid API origin");
    }
    const { path } = await context.params;
    const isMessage = request.method === "POST" && path.length === 5
      && path[0] === "businesses" && path[2] === "sessions" && path[4] === "messages";
    const isIngestion = request.method === "POST" && path.length === 3
      && path[0] === "businesses" && path[2] === "documents";
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = request.headers.get("X-Session-Token");
    if (token) headers.set("X-Session-Token", token);
    const target = new URL(`/api/v1/${path.map(encodeURIComponent).join("/")}`, origin);
    target.search = new URL(request.url).search;
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      signal: request.method === "GET"
        ? AbortSignal.any([request.signal, AbortSignal.timeout(15000)])
        : AbortSignal.timeout(isMessage || isIngestion ? 105000 : 15000),
      redirect: "error",
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
        "X-Request-ID": upstream.headers.get("X-Request-ID") ?? requestId,
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json({ error: { code: "BACKEND_UNAVAILABLE", message: "The backend could not be reached.", retryable: true, requestId } }, {
      status: 503, headers: { "X-Request-ID": requestId, "Cache-Control": "no-store" },
    });
  }
}

export { proxy as GET, proxy as POST };

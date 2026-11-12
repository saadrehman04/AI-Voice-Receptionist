import { NextResponse, type NextRequest } from "next/server";

import { ApiError, apiFetch } from "@/lib/api";
import { getToken } from "@/lib/session";
import type { Me } from "@/lib/types";

// The dashboard's test call goes through here to the voice agent (Pipecat's runner):
//   POST  /api/test-call/start                      -> agent /start
//   POST  /api/test-call/sessions/<id>/api/offer    -> agent WebRTC offer
//   PATCH /api/test-call/sessions/<id>/api/offer    -> agent ICE candidates
// The business is taken from the owner's session, never from the browser, so an owner can only
// test-call their own receptionist. Only these paths are forwarded.

const AGENT_URL = (process.env.AGENT_URL ?? "http://127.0.0.1:7860").replace(/\/$/, "");
const OFFER = /^sessions\/[0-9a-f-]{36}\/api\/offer$/;

async function owner(): Promise<Me | NextResponse> {
  const token = await getToken();
  if (!token) return NextResponse.json({ error: "not signed in" }, { status: 401 });
  try {
    return await apiFetch<Me>("/v1/dashboard/me", { token });
  } catch (err) {
    const status = err instanceof ApiError ? err.status : 502;
    return NextResponse.json({ error: "session check failed" }, { status });
  }
}

async function forward(method: string, path: string, body: string): Promise<NextResponse> {
  const upstream = await fetch(`${AGENT_URL}/${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body,
    cache: "no-store",
  }).catch(() => null);
  if (!upstream)
    return NextResponse.json({ error: "the voice agent isn't running (make agent)" }, { status: 502 });
  return new NextResponse(await upstream.text(), {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

async function handle(request: NextRequest, ctx: RouteContext<"/api/test-call/[...path]">) {
  const path = (await ctx.params).path.join("/");
  const me = await owner();
  if (me instanceof NextResponse) return me;
  if (path === "start" && request.method === "POST") {
    return forward("POST", "start", JSON.stringify({ transport: "webrtc", body: { tenant: me.tenant_slug } }));
  }
  if (OFFER.test(path)) return forward(request.method, path, await request.text());
  return NextResponse.json({ error: "not found" }, { status: 404 });
}

export const POST = handle;
export const PATCH = handle;

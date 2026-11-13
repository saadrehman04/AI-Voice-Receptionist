import { NextResponse, type NextRequest } from "next/server";

import { forwardToAgent, OFFER, startBody } from "@/lib/agent-proxy";
import { ApiError, apiFetch } from "@/lib/api";
import { getToken } from "@/lib/session";
import type { Me } from "@/lib/types";

// The dashboard's test call (lib/agent-proxy.ts forwards it). The business is taken from the
// owner's session, never from the browser, so an owner can only test-call their own receptionist.

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

async function handle(request: NextRequest, ctx: RouteContext<"/api/test-call/[...path]">) {
  const path = (await ctx.params).path.join("/");
  const me = await owner();
  if (me instanceof NextResponse) return me;
  if (path === "start" && request.method === "POST") return forwardToAgent("POST", "start", startBody(me.tenant_slug));
  if (OFFER.test(path)) return forwardToAgent(request.method, path, await request.text());
  return NextResponse.json({ error: "not found" }, { status: 404 });
}

export const POST = handle;
export const PATCH = handle;

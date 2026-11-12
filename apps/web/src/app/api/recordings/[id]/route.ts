import { NextResponse, type NextRequest } from "next/server";

import { API_URL } from "@/lib/api";
import { getToken } from "@/lib/session";

/**
 * Streams a call recording from the API. An <audio> tag can't send our bearer token, so it
 * points here and this handler adds it from the session cookie. The API checks the call
 * belongs to the token's business.
 */
export async function GET(request: NextRequest, ctx: RouteContext<"/api/recordings/[id]">) {
  const { id } = await ctx.params;
  const token = await getToken();
  if (!token) return new NextResponse("not signed in", { status: 401 });
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  const range = request.headers.get("range");
  if (range) headers.Range = range; // lets the player seek
  const upstream = await fetch(
    `${API_URL}/v1/dashboard/calls/${encodeURIComponent(id)}/recording`,
    { headers, cache: "no-store" },
  ).catch(() => null);
  if (!upstream || !upstream.ok || !upstream.body)
    return new NextResponse("recording not found", { status: upstream?.status === 401 ? 401 : 404 });
  const pass = new Headers();
  for (const h of ["content-type", "content-length", "content-range", "accept-ranges"]) {
    const v = upstream.headers.get(h);
    if (v) pass.set(h, v);
  }
  pass.set("cache-control", "private, no-store");
  return new NextResponse(upstream.body, { status: upstream.status, headers: pass });
}

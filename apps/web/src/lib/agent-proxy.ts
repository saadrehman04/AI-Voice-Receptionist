import "server-only";

import { NextResponse } from "next/server";

// Browser calls reach the voice agent (Pipecat's runner) through the dashboard's own routes:
//   POST  .../start                      -> agent /start (the business is set here, never by the browser)
//   POST  .../sessions/<id>/api/offer    -> agent WebRTC offer
//   PATCH .../sessions/<id>/api/offer    -> agent ICE candidates
// Used by /api/test-call (an owner's own receptionist) and /api/demo-call (the public demo).

const AGENT_URL = (process.env.AGENT_URL ?? "http://127.0.0.1:7860").replace(/\/$/, "");
// In production the agent sits behind Caddy, which lets call set-up through only with this key
// (deploy/Caddyfile): the agent's /start checks no login itself. Unset in local development.
const AGENT_KEY = process.env.AGENT_KEY;

/** The only other path forwarded: a session's offer and ICE candidates. */
export const OFFER = /^sessions\/[0-9a-f-]{36}\/api\/offer$/;

export async function forwardToAgent(method: string, path: string, body: string): Promise<NextResponse> {
  const upstream = await fetch(`${AGENT_URL}/${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...(AGENT_KEY ? { "X-Agent-Key": AGENT_KEY } : {}) },
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

/** The agent's /start body for a call to one business (by its slug). */
export function startBody(tenant: string): string {
  return JSON.stringify({ transport: "webrtc", body: { tenant } });
}

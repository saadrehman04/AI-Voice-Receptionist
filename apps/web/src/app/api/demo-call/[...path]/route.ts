import { NextResponse, type NextRequest } from "next/server";

import { forwardToAgent, OFFER, startBody } from "@/lib/agent-proxy";

// The public demo call on the home page (/demo): no sign-in, and always the demo clinic. The
// business is fixed here, never taken from the browser, so this can't reach a real business's
// receptionist. Spend is capped by that business's calls-per-hour limit (D38, D44).
const DEMO_TENANT = process.env.DEMO_TENANT ?? "demo-dental";

async function handle(request: NextRequest, ctx: RouteContext<"/api/demo-call/[...path]">) {
  const path = (await ctx.params).path.join("/");
  if (path === "start" && request.method === "POST") return forwardToAgent("POST", "start", startBody(DEMO_TENANT));
  if (OFFER.test(path)) return forwardToAgent(request.method, path, await request.text());
  return NextResponse.json({ error: "not found" }, { status: 404 });
}

export const POST = handle;
export const PATCH = handle;

import { NextResponse, type NextRequest } from "next/server";

// An optimistic check only: no cookie -> straight to /login, without rendering the page.
// The real check is the API's, on every request (an expired or forged cookie gets a 401
// there, and the page redirects to /login itself).

const SESSION_COOKIE = "rx_session"; // = lib/session.ts (which is server-only)

export function proxy(request: NextRequest) {
  const signedIn = request.cookies.has(SESSION_COOKIE);
  const { pathname } = request.nextUrl;
  if (!signedIn && pathname !== "/login") {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  if (signedIn && pathname === "/login") {
    return NextResponse.redirect(new URL("/calls", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/login",
    "/calls/:path*",
    "/bookings/:path*",
    "/messages/:path*",
    "/analytics/:path*",
    "/settings/:path*",
    "/test-call/:path*",
    "/welcome/:path*",
  ],
};

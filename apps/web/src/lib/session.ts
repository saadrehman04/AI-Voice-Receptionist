import "server-only";

import { cookies } from "next/headers";

// The session is the API's own signed JWT (12 h), kept in an httpOnly cookie: page scripts
// can't read it, and every API call is made from the Next.js server with it as the bearer
// token. The API checks the signature and takes the tenant from it, so a tampered cookie
// gets a 401, never someone else's data.

export const SESSION_COOKIE = "rx_session";
const MAX_AGE_SECS = 12 * 60 * 60; // the API token's lifetime

export async function getToken(): Promise<string | undefined> {
  return (await cookies()).get(SESSION_COOKIE)?.value;
}

export async function setSession(token: string): Promise<void> {
  (await cookies()).set(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: MAX_AGE_SECS,
  });
}

export async function clearSession(): Promise<void> {
  (await cookies()).delete(SESSION_COOKIE);
}

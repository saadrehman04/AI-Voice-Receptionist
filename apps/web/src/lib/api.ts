import "server-only";

import { redirect } from "next/navigation";

import { getToken } from "./session";
import type {
  Booking,
  CalendarStatus,
  CallDetail,
  CallItem,
  Me,
  Message,
  Stats,
  TenantConfig,
  Usage,
} from "./types";

// Server-side client for the FastAPI app. Only the Next.js server talks to the API, so the
// API never needs CORS and the token never reaches page scripts.

export const API_URL = (process.env.API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: string,
  ) {
    super(detail);
  }
}

/** A readable message out of the API's error body (`{detail: {code, detail}}` or a 422). */
export function errorFrom(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as { code?: string; detail?: string };
    return new ApiError(status, d.code ?? "error", d.detail ?? "something went wrong");
  }
  if (Array.isArray(detail) && detail.length) {
    // FastAPI validation errors: [{loc: [...], msg}]
    const first = detail[0] as { loc?: (string | number)[]; msg?: string };
    const where = (first.loc ?? []).filter((p) => p !== "body").join(" › ");
    return new ApiError(status, "invalid", `${where ? `${where}: ` : ""}${first.msg ?? "invalid"}`);
  }
  return new ApiError(status, "error", `the API answered ${status}`);
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<T> {
  const { token, headers, ...rest } = init;
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...rest,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
    });
  } catch {
    throw new ApiError(503, "api_unreachable", "can't reach the API; is `make api` running?");
  }
  const body: unknown = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw errorFrom(response.status, body);
  return body as T;
}

/** Calls a dashboard route as the signed-in owner; no session (or an expired one) -> /login. */
async function owner<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await getToken();
  if (!token) redirect("/login");
  try {
    return await apiFetch<T>(`/v1/dashboard${path}`, { ...init, token });
  } catch (err) {
    // Pages can't change cookies, so a route handler clears the dead one (else the proxy
    // would bounce /login straight back here).
    if (err instanceof ApiError && err.status === 401) redirect("/session/expired");
    throw err;
  }
}

export const dashboard = {
  me: () => owner<Me>("/me"),
  calls: (limit = 50) => owner<CallItem[]>(`/calls?limit=${limit}`),
  call: (id: string) => owner<CallDetail>(`/calls/${encodeURIComponent(id)}`),
  bookings: (upcoming = true) => owner<Booking[]>(`/bookings?upcoming=${upcoming}`),
  messages: () => owner<Message[]>("/messages"),
  setMessage: (id: string, status: "new" | "done") =>
    owner<Message>(`/messages/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  stats: (days = 7) => owner<Stats>(`/stats?days=${days}`),
  usage: () => owner<Usage>("/usage"),
  settings: () => owner<TenantConfig>("/settings"),
  saveSettings: (config: TenantConfig) =>
    owner<TenantConfig>("/settings", { method: "PUT", body: JSON.stringify(config) }),
  calendar: () => owner<CalendarStatus>("/calendar"),
  saveCalendar: (calendarId: string | null) =>
    owner<CalendarStatus>("/calendar", {
      method: "PUT",
      body: JSON.stringify({ calendar_id: calendarId }),
    }),
};

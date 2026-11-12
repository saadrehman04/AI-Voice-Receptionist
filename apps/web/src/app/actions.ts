"use server";

// Server Actions: the only way the browser changes anything. Each one runs on the Next.js
// server, reads the session cookie itself (never trusts an id from the form) and calls the API.

import { refresh } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, apiFetch, dashboard } from "@/lib/api";
import { clearSession, setSession } from "@/lib/session";
import type { FormResult, TenantConfig, WebsiteDraft } from "@/lib/types";

function failure(err: unknown): { ok: false; error: string } {
  if (err instanceof ApiError) return { ok: false, error: err.detail };
  throw err; // redirects and real bugs keep propagating
}

// ---------------------------------------------------------------- auth

export async function login(_prev: FormResult | null, form: FormData): Promise<FormResult> {
  const email = String(form.get("email") ?? "").trim();
  const password = String(form.get("password") ?? "");
  if (!email || !password) return { ok: false, error: "Enter your email and password." };
  try {
    const { access_token } = await apiFetch<{ access_token: string }>("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    await setSession(access_token);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401)
      return { ok: false, error: "Wrong email or password." };
    return failure(err);
  }
  redirect("/calls");
}

export async function logout(): Promise<void> {
  await clearSession();
  redirect("/login");
}

// ---------------------------------------------------------------- onboarding

export type DraftResult = { ok: true; draft: WebsiteDraft } | { ok: false; error: string };

export async function draftFromWebsite(url: string): Promise<DraftResult> {
  if (!url.trim()) return { ok: false, error: "Enter your website address." };
  try {
    const draft = await apiFetch<WebsiteDraft>("/v1/onboarding/draft", {
      method: "POST",
      body: JSON.stringify({ url: url.trim() }),
    });
    return { ok: true, draft };
  } catch (err) {
    return failure(err);
  }
}

export async function createBusiness(input: {
  tenant: Record<string, unknown>;
  email: string;
  password: string;
}): Promise<FormResult> {
  try {
    const { access_token } = await apiFetch<{ access_token: string }>("/v1/onboarding", {
      method: "POST",
      body: JSON.stringify({
        tenant: input.tenant,
        owner_email: input.email.trim(),
        password: input.password,
      }),
    });
    await setSession(access_token);
    return { ok: true };
  } catch (err) {
    if (err instanceof ApiError && err.code === "tenant_exists")
      return { ok: false, error: "That web address name is taken; change it on the first step." };
    if (err instanceof ApiError && err.code === "email_taken")
      return { ok: false, error: "An account with that email exists. Log in instead." };
    return failure(err);
  }
}

// ---------------------------------------------------------------- dashboard

export async function saveSettings(payload: Record<string, unknown>): Promise<FormResult> {
  try {
    await dashboard.saveSettings(payload as unknown as TenantConfig);
  } catch (err) {
    return failure(err);
  }
  refresh();
  return { ok: true, message: "Saved. The next call uses these settings." };
}

export async function saveCalendar(calendarId: string | null): Promise<FormResult> {
  try {
    await dashboard.saveCalendar(calendarId);
  } catch (err) {
    return failure(err);
  }
  refresh();
  return {
    ok: true,
    message: calendarId ? "Connected. New bookings go into this calendar." : "Disconnected.",
  };
}

export async function setMessageStatus(id: string, status: "new" | "done"): Promise<void> {
  await dashboard.setMessage(id, status);
  refresh();
}

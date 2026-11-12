// Display helpers. Times show in the business's time zone (Asia/Karachi by default), since
// that's the clock the owner and their callers live by, whatever the viewer's laptop says.

import type { Outcome } from "./types";

export const DEFAULT_TZ = "Asia/Karachi";

export function formatDateTime(iso: string, timeZone = DEFAULT_TZ): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone,
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

export function formatTime(iso: string, timeZone = DEFAULT_TZ): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(iso));
}

export function formatDay(isoDate: string): string {
  // A bare "2026-11-11" is a calendar day already in the business's zone: format it as UTC
  // so the viewer's own zone can't shift it to the day before.
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

export function formatDuration(secs: number | null): string {
  if (secs === null) return "in progress";
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function formatPrice(price: string | number | null | undefined, currency = "PKR"): string {
  if (price === null || price === undefined || price === "") return "on request";
  const n = Number(price);
  return `${currency} ${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n)}`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

export function formatPercent(rate: number | null): string {
  return rate === null ? "–" : `${Math.round(rate * 100)}%`;
}

const OUTCOMES: Record<string, string> = {
  booked: "Booked",
  rescheduled: "Rescheduled",
  cancelled: "Cancelled",
  message: "Message taken",
  handoff: "Handed to staff",
  no_action: "Answered",
  pending: "Summarising…",
};

export function outcomeLabel(outcome: Outcome | string | null): string {
  return OUTCOMES[outcome ?? "pending"] ?? outcome ?? "";
}

/** Who called: their number, or "Browser call" for the dashboard's test calls (no number). */
export function callerLabel(phone: string | null, channel: string): string {
  if (!phone && channel === "webrtc") return "Browser call";
  return formatPhone(phone);
}

/** "+923001234567" -> "0300 1234567" (how Pakistani numbers are read out); others unchanged. */
export function formatPhone(phone: string | null): string {
  if (!phone) return "unknown number";
  const m = /^\+92(3\d{2})(\d{7})$/.exec(phone);
  return m ? `0${m[1]} ${m[2]}` : phone;
}

/** Stage keys of the agent's turn timings (apps/agent/.../timing.py), in the order a turn runs. */
export const STAGES: { key: string; label: string }[] = [
  { key: "stt", label: "Transcript ready (from caller stop)" },
  { key: "end_of_turn", label: "Turn released (from caller stop)" },
  { key: "llm", label: "LLM first words" },
  { key: "tts", label: "Voice first audio" },
  { key: "total", label: "Caller stops → agent speaks" },
];

/** How far through its monthly minutes a business is: the meter's fill and what to say. */
export function usageLevel(minutes: number, limit: number): {
  share: number;
  level: "ok" | "near" | "over";
  label: string;
} {
  const share = limit > 0 ? Math.min(1, minutes / limit) : 1;
  if (minutes >= limit)
    return { share, level: "over", label: "Limit reached: callers are told who to call instead" };
  if (share >= 0.8) return { share, level: "near", label: "Close to this month's limit" };
  return { share, level: "ok", label: "Within this month's limit" };
}

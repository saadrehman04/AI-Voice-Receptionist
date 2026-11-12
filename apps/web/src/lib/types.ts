// Shapes of the API's JSON (apps/api/src/receptionist_api/routes/owners.py and setup.py).
// Hand-written and kept small: the API is the source of truth, and tests/lib cover the
// conversions that matter.

export type Weekday =
  | "monday"
  | "tuesday"
  | "wednesday"
  | "thursday"
  | "friday"
  | "saturday"
  | "sunday";

export const WEEKDAYS: Weekday[] = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];

export type Language = "en" | "ur";

export interface TimeRange {
  start: string; // "09:00:00" from the API; "09:00" accepted on the way in
  end: string;
}

export interface Service {
  name: string;
  name_ur?: string | null;
  duration_min: number;
  price?: string | number | null; // Decimal comes back as a string
  price_note?: string | null;
  staff?: string[];
}

export interface FaqEntry {
  question: string;
  answer: string;
  answer_ur?: string | null;
  keywords?: string[];
}

export interface TenantConfig {
  slug: string;
  business_name: string;
  business_name_ur?: string | null;
  business_type: string;
  languages: Language[];
  default_language: Language;
  timezone: string;
  currency: string;
  address?: string | null;
  opening_hours: Partial<Record<Weekday, TimeRange[]>>;
  services: Service[];
  policies?: {
    cancellation?: string | null;
    late_arrival?: string | null;
    payment?: string | null;
    other?: string[];
  };
  greeting: { en: string; ur?: string | null };
  handoff: { name: string; phone: string };
  faq: FaqEntry[];
  booking_buffer_min?: number;
  min_notice_min?: number;
  max_days_ahead?: number;
  calendar_id?: string | null;
}

export interface Me {
  email: string;
  tenant_slug: string;
  business_name: string;
}

export type Outcome =
  | "booked"
  | "rescheduled"
  | "cancelled"
  | "message"
  | "handoff"
  | "no_action";

export interface CallItem {
  id: string;
  call_id: string;
  channel: string;
  caller_phone: string | null;
  started_at: string;
  ended_at: string | null;
  duration_secs: number | null;
  status: string;
  outcome: Outcome | null;
  intent: string | null;
  sentiment: string | null;
  summary: string | null;
  language: string | null;
}

export interface Turn {
  idx: number;
  speaker: "caller" | "agent" | string;
  text: string;
  interrupted: boolean;
  timings: Record<string, number> | null;
  spoken_at: string;
}

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
  ok: boolean;
  duration_ms: number;
  called_at: string;
}

export interface CallDetail extends CallItem {
  turns: Turn[];
  tool_calls: ToolCall[];
  has_recording: boolean;
}

export interface Booking {
  code: string;
  service: string;
  customer_name: string;
  customer_phone: string;
  start: string;
  end: string;
  status: string;
  call_id: string | null;
}

export interface Message {
  id: string;
  kind: string;
  caller_name: string | null;
  caller_phone: string | null;
  body: string;
  status: "new" | "done";
  created_at: string;
  call_id: string | null;
}

export interface StageStats {
  n: number;
  p50: number | null;
  p95: number | null;
}

export interface Stats {
  days: number;
  calls: number;
  by_outcome: Record<string, number>;
  latency_ms: Record<string, StageStats>;
  per_day: { day: string; calls: number; booked: number }[];
  booking_rate: number | null;
  after_hours: number;
}

export interface DraftService {
  name: string;
  price: string | number | null;
  price_note: string | null;
  duration_min: number | null;
  evidence: string;
}

export interface WebsiteDraft {
  business_name: string | null;
  address: string | null;
  opening_hours_text: string | null;
  services: DraftService[];
  faq: { question: string; answer: string; evidence: string }[];
  dropped: string[];
}

export interface CalendarStatus {
  provider: "google" | "fake";
  share_with: string | null;
  calendar_id: string | null;
}

/** What a form action hands back to its form. */
export type FormResult = { ok: true; message?: string } | { ok: false; error: string };

// Pure helpers behind the settings editor and the sign-up wizard (tested in tenant-form.test.ts).
// They mirror the API's TenantConfig rules so the owner sees problems before saving; the API
// validates again and stays the source of truth.

import {
  WEEKDAYS,
  type FaqEntry,
  type Service,
  type TenantConfig,
  type Weekday,
  type WebsiteDraft,
} from "./types";

export type HoursForm = Record<Weekday, { start: string; end: string }[]>;

/** "09:00:00" -> "09:00" (the API returns seconds; inputs of type=time want HH:MM). */
export function hhmm(time: string): string {
  return time.slice(0, 5);
}

export function hoursToForm(hours: TenantConfig["opening_hours"]): HoursForm {
  const out = {} as HoursForm;
  for (const day of WEEKDAYS) {
    out[day] = (hours[day] ?? []).map((r) => ({ start: hhmm(r.start), end: hhmm(r.end) }));
  }
  return out;
}

/** The API accepts `monday: ["09:00-13:00"]`; an empty list means closed. */
export function hoursFromForm(form: HoursForm): Record<Weekday, string[]> {
  const out = {} as Record<Weekday, string[]>;
  for (const day of WEEKDAYS) {
    out[day] = [...form[day]]
      .sort((a, b) => a.start.localeCompare(b.start))
      .map((r) => `${r.start}-${r.end}`);
  }
  return out;
}

export function slugify(name: string): string {
  const slug = name
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");
  return slug.length >= 2 ? slug : `business-${slug || "new"}`;
}

export const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,62}$/;

export function greetingFor(name: string): { en: string } {
  return { en: `Thank you for calling ${name}. How can I help you today?` };
}

/** The owner's own time zone (from their browser), the usual home of their business. */
export function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** The owner's country from their browser's language setting ("en-PK" -> "PK"), else "US". */
export function localCountry(): string {
  try {
    const region = typeof navigator === "undefined" ? undefined : new Intl.Locale(navigator.language).maximize().region;
    return region && /^[A-Z]{2}$/.test(region) ? region : "US";
  } catch {
    return "US";
  }
}

/** A starting point for the wizard: no services, no prices, nothing the owner didn't enter. */
export function starterConfig(businessName: string, businessType: string): TenantConfig {
  const weekday = [{ start: "10:00", end: "19:00" }];
  return {
    slug: slugify(businessName),
    business_name: businessName,
    business_type: businessType,
    languages: ["en"],
    default_language: "en",
    // Fixed here; the wizard swaps in the owner's own time zone and country once it runs in the
    // browser. Reading them here would differ between Vercel's server (UTC) and the browser, and
    // React would refuse the page (hydration mismatch).
    timezone: "UTC",
    currency: "USD",
    country: "US",
    address: null,
    opening_hours: {
      monday: weekday,
      tuesday: weekday,
      wednesday: weekday,
      thursday: weekday,
      friday: weekday,
      saturday: weekday,
      sunday: [],
    },
    services: [],
    greeting: greetingFor(businessName),
    handoff: { name: "", phone: "" },
    faq: [],
  };
}

/**
 * Fold a website draft into the config. Only what the API's grounding kept arrives here;
 * a service without a stated duration gets 0, which `validateConfig` flags so the owner
 * must fill it in (we don't guess how long a service takes).
 */
export function applyDraft(config: TenantConfig, draft: WebsiteDraft): TenantConfig {
  const known = new Set(config.services.map((s) => s.name.toLowerCase()));
  const services: Service[] = [
    ...config.services,
    ...draft.services
      .filter((s) => !known.has(s.name.toLowerCase()))
      .map((s) => ({
        name: s.name,
        duration_min: s.duration_min ?? 0,
        price: s.price ?? null,
        price_note: s.price_note ?? null,
      })),
  ];
  const askedAlready = new Set(config.faq.map((f) => f.question.toLowerCase()));
  const faq: FaqEntry[] = [
    ...config.faq,
    ...draft.faq
      .filter((f) => !askedAlready.has(f.question.toLowerCase()))
      .map((f) => ({ question: f.question, answer: f.answer })),
  ];
  return {
    ...config,
    business_name: config.business_name || draft.business_name || "",
    address: config.address || draft.address || null,
    services,
    faq,
  };
}

function toMinutes(t: string): number {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

export type Area = "business" | "services" | "hours" | "voice";

/** Problems an owner can fix, in plain words, by the part of the setup they belong to. */
export function problemsByArea(config: TenantConfig, hours?: HoursForm): Record<Area, string[]> {
  const out: Record<Area, string[]> = { business: [], services: [], hours: [], voice: [] };
  let area: Area = "business";
  const errors = { push: (e: string) => out[area].push(e) };
  if (!config.business_name.trim()) errors.push("Enter the business name.");
  const wa = config.whatsapp_phone_number_id?.trim();
  if (wa && !/^\d{5,30}$/.test(wa))
    errors.push("The WhatsApp phone number ID is the long number from Meta's API Setup page (digits only).");
  if (!SLUG_RE.test(config.slug))
    errors.push("The web address name may only use lowercase letters, digits and dashes.");
  area = "voice";
  if (!config.languages.length) errors.push("Pick at least one language.");
  if (!config.languages.includes(config.default_language))
    errors.push("The default language must be one of the languages you picked.");
  if (!config.greeting.en.trim()) errors.push("Enter the English greeting.");
  if (!config.handoff.name.trim() || !config.handoff.phone.trim())
    errors.push("Enter who callers are handed to when the receptionist can't help, and their phone.");

  area = "services";
  if (!config.services.length) errors.push("Add at least one service.");
  const names = new Set<string>();
  for (const s of config.services) {
    const label = s.name.trim() || "a service";
    if (!s.name.trim()) errors.push("Every service needs a name.");
    if (names.has(s.name.trim().toLowerCase())) errors.push(`"${label}" is listed twice.`);
    names.add(s.name.trim().toLowerCase());
    if (!Number.isInteger(s.duration_min) || s.duration_min <= 0 || s.duration_min > 600)
      errors.push(`Set how many minutes "${label}" takes.`);
    if (s.price !== null && s.price !== undefined && s.price !== "" && Number(s.price) < 0)
      errors.push(`The price of "${label}" can't be negative.`);
    if (s.price !== null && s.price !== undefined && s.price !== "" && Number.isNaN(Number(s.price)))
      errors.push(`The price of "${label}" must be a number.`);
  }
  for (const f of config.faq) {
    if (f.question.trim().length < 3 || !f.answer.trim())
      errors.push("Every FAQ entry needs a question and an answer.");
  }

  area = "hours";
  const form = hours ?? hoursToForm(config.opening_hours);
  for (const day of WEEKDAYS) {
    const ranges = [...form[day]].sort((a, b) => a.start.localeCompare(b.start));
    for (const r of ranges) {
      if (!r.start || !r.end || toMinutes(r.start) >= toMinutes(r.end))
        errors.push(`On ${day}, opening time must be before closing time.`);
    }
    for (let i = 1; i < ranges.length; i++) {
      if (toMinutes(ranges[i].start) < toMinutes(ranges[i - 1].end))
        errors.push(`The opening hours on ${day} overlap.`);
    }
  }
  if (WEEKDAYS.every((d) => form[d].length === 0)) errors.push("Open on at least one day.");
  for (const key of Object.keys(out) as Area[]) out[key] = [...new Set(out[key])];
  return out;
}

/** Every problem, in form order. Empty = ready to save. */
export function validateConfig(config: TenantConfig, hours?: HoursForm): string[] {
  const p = problemsByArea(config, hours);
  return [...p.business, ...p.voice, ...p.services, ...p.hours];
}

/** The JSON the API expects: hours as strings, empty prices as null. */
export function toPayload(config: TenantConfig, hours: HoursForm): Record<string, unknown> {
  return {
    ...config,
    business_name: config.business_name.trim(),
    address: config.address?.trim() || null,
    whatsapp_phone_number_id: config.whatsapp_phone_number_id?.trim() || null,
    opening_hours: hoursFromForm(hours),
    services: config.services.map((s) => ({
      ...s,
      name: s.name.trim(),
      price: s.price === "" || s.price === undefined || s.price === null ? null : Number(s.price),
      price_note: s.price_note?.trim() || null,
    })),
    faq: config.faq.map((f) => ({ ...f, question: f.question.trim(), answer: f.answer.trim() })),
    greeting: {
      en: config.greeting.en.trim(),
      ur: config.languages.includes("ur") ? config.greeting.ur?.trim() || null : config.greeting.ur || null,
    },
    handoff: { name: config.handoff.name.trim(), phone: config.handoff.phone.trim() },
  };
}

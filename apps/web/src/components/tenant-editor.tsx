"use client";

// The business's setup as editable sections. The settings page shows all of them; the sign-up
// wizard shows one or two per step. State lives in the parent (config + hours), so a section
// is just a view with onChange.

import type { ReactNode } from "react";

import { type HoursForm } from "@/lib/tenant-form";
import { WEEKDAYS, type FaqEntry, type Language, type Service, type TenantConfig } from "@/lib/types";

import { Card, CardTitle, Field, buttonClass, inputClass } from "./ui";

export type EditorProps = {
  config: TenantConfig;
  onChange: (next: TenantConfig) => void;
  hours: HoursForm;
  onHoursChange: (next: HoursForm) => void;
};

const DAY_LABEL: Record<string, string> = {
  monday: "Monday",
  tuesday: "Tuesday",
  wednesday: "Wednesday",
  thursday: "Thursday",
  friday: "Friday",
  saturday: "Saturday",
  sunday: "Sunday",
};

function Section({ title, hint, id, children }: { title: string; hint?: ReactNode; id?: string; children: ReactNode }) {
  return (
    <Card id={id}>
      <CardTitle hint={hint}>{title}</CardTitle>
      {children}
    </Card>
  );
}

export function BusinessSection({ config, onChange, showSlug = false }: EditorProps & { showSlug?: boolean }) {
  return (
    <Section title="Business" hint="What callers hear the receptionist call you, and where you are.">
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Business name">
          <input
            className={inputClass}
            value={config.business_name}
            onChange={(e) => onChange({ ...config, business_name: e.target.value })}
            required
          />
        </Field>
        <Field label="Name in Urdu" hint="Optional; used when the call is in Urdu.">
          <input
            dir="rtl"
            className={inputClass}
            value={config.business_name_ur ?? ""}
            onChange={(e) => onChange({ ...config, business_name_ur: e.target.value || null })}
          />
        </Field>
        <Field label="Kind of business" hint="e.g. hair salon, dental clinic, car workshop">
          <input
            className={inputClass}
            value={config.business_type}
            onChange={(e) => onChange({ ...config, business_type: e.target.value })}
          />
        </Field>
        <Field label="Address">
          <input
            className={inputClass}
            value={config.address ?? ""}
            onChange={(e) => onChange({ ...config, address: e.target.value || null })}
          />
        </Field>
        {showSlug ? (
          <Field label="Short name" hint="Lowercase letters, digits and dashes; identifies your business.">
            <input
              className={inputClass}
              value={config.slug}
              onChange={(e) => onChange({ ...config, slug: e.target.value.toLowerCase() })}
            />
          </Field>
        ) : null}
      </div>
    </Section>
  );
}

export function HoursSection({ hours, onHoursChange }: EditorProps) {
  function setDay(day: keyof HoursForm, ranges: HoursForm[keyof HoursForm]) {
    onHoursChange({ ...hours, [day]: ranges });
  }
  return (
    <Section title="Opening hours" hint="The receptionist only offers appointments inside these hours.">
      <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {WEEKDAYS.map((day) => {
          const ranges = hours[day];
          return (
            <li key={day} className="flex flex-col gap-2 py-2 sm:flex-row sm:items-center">
              <span className="w-28 text-sm font-medium">{DAY_LABEL[day]}</span>
              <div className="flex flex-1 flex-wrap items-center gap-2">
                {ranges.length === 0 ? <span className="text-sm text-zinc-500">Closed</span> : null}
                {ranges.map((r, i) => (
                  <span key={i} className="flex items-center gap-1">
                    <input
                      type="time"
                      aria-label={`${DAY_LABEL[day]} opens`}
                      className={`${inputClass} w-28`}
                      value={r.start}
                      onChange={(e) => setDay(day, ranges.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)))}
                    />
                    <span className="text-zinc-400">–</span>
                    <input
                      type="time"
                      aria-label={`${DAY_LABEL[day]} closes`}
                      className={`${inputClass} w-28`}
                      value={r.end}
                      onChange={(e) => setDay(day, ranges.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)))}
                    />
                    <button
                      type="button"
                      aria-label="Remove these hours"
                      className={buttonClass.ghost}
                      onClick={() => setDay(day, ranges.filter((_, j) => j !== i))}
                    >
                      ✕
                    </button>
                  </span>
                ))}
                <button
                  type="button"
                  className={buttonClass.ghost}
                  onClick={() => {
                    const last = ranges.at(-1);
                    setDay(day, [...ranges, last ? { start: last.end, end: last.end < "20:00" ? "20:00" : "23:00" } : { start: "10:00", end: "19:00" }]);
                  }}
                >
                  {ranges.length ? "+ break / second shift" : "+ open"}
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

export function ServicesSection({ config, onChange }: EditorProps) {
  const services = config.services;
  function set(i: number, patch: Partial<Service>) {
    onChange({ ...config, services: services.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  }
  return (
    <Section
      title="Services and prices"
      hint="The receptionist quotes only these prices. Leave a price empty and it says the price is on request."
    >
      <ul className="space-y-3">
        {services.map((s, i) => (
          <li key={i} className="grid gap-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800 sm:grid-cols-[2fr_1fr_1fr_auto] sm:items-end">
            <Field label="Service">
              <input className={inputClass} value={s.name} onChange={(e) => set(i, { name: e.target.value })} />
            </Field>
            <Field label="Minutes">
              <input
                type="number"
                min={5}
                max={600}
                step={5}
                className={`${inputClass} ${s.duration_min > 0 ? "" : "border-amber-500"}`}
                value={s.duration_min || ""}
                placeholder="set"
                onChange={(e) => set(i, { duration_min: Number(e.target.value) || 0 })}
              />
            </Field>
            <Field label={`Price (${config.currency})`}>
              <input
                inputMode="decimal"
                className={inputClass}
                value={s.price ?? ""}
                placeholder="on request"
                onChange={(e) => set(i, { price: e.target.value.replace(/[^\d.]/g, "") || null })}
              />
            </Field>
            <button
              type="button"
              className={buttonClass.danger}
              onClick={() => onChange({ ...config, services: services.filter((_, j) => j !== i) })}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className={`${buttonClass.secondary} mt-3`}
        onClick={() => onChange({ ...config, services: [...services, { name: "", duration_min: 30, price: null }] })}
      >
        + Add a service
      </button>
    </Section>
  );
}

export function FaqSection({ config, onChange }: EditorProps) {
  const faq = config.faq;
  function set(i: number, patch: Partial<FaqEntry>) {
    onChange({ ...config, faq: faq.map((f, j) => (j === i ? { ...f, ...patch } : f)) });
  }
  return (
    <Section
      title="Questions callers ask"
      hint="Parking, payment, walk-ins… The receptionist answers from these and nothing else; anything not here, it offers to take a message."
    >
      <ul className="space-y-3">
        {faq.map((f, i) => (
          <li key={i} className="space-y-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
            <Field label="Question">
              <input className={inputClass} value={f.question} onChange={(e) => set(i, { question: e.target.value })} />
            </Field>
            <Field label="Answer">
              <textarea rows={2} className={inputClass} value={f.answer} onChange={(e) => set(i, { answer: e.target.value })} />
            </Field>
            <Field label="Answer in Urdu" hint="Optional.">
              <textarea
                dir="rtl"
                rows={2}
                className={inputClass}
                value={f.answer_ur ?? ""}
                onChange={(e) => set(i, { answer_ur: e.target.value || null })}
              />
            </Field>
            <button type="button" className={buttonClass.danger} onClick={() => onChange({ ...config, faq: faq.filter((_, j) => j !== i) })}>
              Remove
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className={`${buttonClass.secondary} mt-3`}
        onClick={() => onChange({ ...config, faq: [...faq, { question: "", answer: "" }] })}
      >
        + Add a question
      </button>
    </Section>
  );
}

const LANGS: { code: Language; label: string }[] = [
  { code: "en", label: "English" },
  { code: "ur", label: "Urdu" },
];

export function VoiceSection({ config, onChange }: EditorProps) {
  function toggle(code: Language) {
    const on = config.languages.includes(code);
    const languages = on ? config.languages.filter((l) => l !== code) : [...config.languages, code];
    const default_language = languages.includes(config.default_language) ? config.default_language : (languages[0] ?? "en");
    onChange({ ...config, languages, default_language });
  }
  return (
    <Section title="Languages, greeting and hand-off" hint="How calls start, and who callers reach when the receptionist can't help.">
      <div className="space-y-4">
        <fieldset>
          <legend className="mb-1 text-sm font-medium">Languages</legend>
          <div className="flex flex-wrap gap-4">
            {LANGS.map((l) => (
              <label key={l.code} className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={config.languages.includes(l.code)} onChange={() => toggle(l.code)} />
                {l.label}
              </label>
            ))}
          </div>
          {config.languages.length > 1 ? (
            <label className="mt-2 flex items-center gap-2 text-sm">
              Start calls in
              <select
                className={`${inputClass} w-auto`}
                value={config.default_language}
                onChange={(e) => onChange({ ...config, default_language: e.target.value as Language })}
              >
                {LANGS.filter((l) => config.languages.includes(l.code)).map((l) => (
                  <option key={l.code} value={l.code}>
                    {l.label}
                  </option>
                ))}
              </select>
              <span className="text-xs text-zinc-500">(it switches when the caller does)</span>
            </label>
          ) : null}
        </fieldset>
        <Field label="Greeting (English)">
          <input className={inputClass} value={config.greeting.en} onChange={(e) => onChange({ ...config, greeting: { ...config.greeting, en: e.target.value } })} />
        </Field>
        {config.languages.includes("ur") ? (
          <Field label="Greeting (Urdu)">
            <input
              dir="rtl"
              className={inputClass}
              value={config.greeting.ur ?? ""}
              onChange={(e) => onChange({ ...config, greeting: { ...config.greeting, ur: e.target.value } })}
            />
          </Field>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Hand off to (name)" hint="The person the receptionist refers callers to.">
            <input className={inputClass} value={config.handoff.name} onChange={(e) => onChange({ ...config, handoff: { ...config.handoff, name: e.target.value } })} />
          </Field>
          <Field label="Their phone">
            <input
              type="tel"
              className={inputClass}
              value={config.handoff.phone}
              onChange={(e) => onChange({ ...config, handoff: { ...config.handoff, phone: e.target.value } })}
            />
          </Field>
        </div>
      </div>
    </Section>
  );
}

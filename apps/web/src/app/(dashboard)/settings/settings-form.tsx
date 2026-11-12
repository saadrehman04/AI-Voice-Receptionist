"use client";

import { useState, useTransition } from "react";

import { saveSettings } from "@/app/actions";
import {
  BusinessSection,
  FaqSection,
  HoursSection,
  ServicesSection,
  VoiceSection,
} from "@/components/tenant-editor";
import { ErrorNote, buttonClass } from "@/components/ui";
import { hoursToForm, toPayload, validateConfig } from "@/lib/tenant-form";
import type { FormResult, TenantConfig } from "@/lib/types";

export function SettingsForm({ initial }: { initial: TenantConfig }) {
  const [config, setConfig] = useState(initial);
  const [hours, setHours] = useState(() => hoursToForm(initial.opening_hours));
  const [result, setResult] = useState<FormResult | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [pending, start] = useTransition();
  const props = {
    config,
    onChange: (c: TenantConfig) => {
      setConfig(c);
      setResult(null);
    },
    hours,
    onHoursChange: (h: typeof hours) => {
      setHours(h);
      setResult(null);
    },
  };

  function save() {
    const problems = validateConfig(config, hours);
    setErrors(problems);
    if (problems.length) return;
    start(async () => setResult(await saveSettings(toPayload(config, hours))));
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <BusinessSection {...props} />
      <ServicesSection {...props} />
      <HoursSection {...props} />
      <FaqSection {...props} />
      <VoiceSection {...props} />
      <div className="sticky bottom-0 -mx-4 flex flex-col gap-2 border-t border-zinc-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95 sm:mx-0 sm:rounded-xl sm:border">
        {errors.length ? (
          <ErrorNote>
            <span className="block font-medium">Fix these first:</span>
            <span className="block">{errors.join(" ")}</span>
          </ErrorNote>
        ) : null}
        {result && !result.ok ? <ErrorNote>{result.error}</ErrorNote> : null}
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-emerald-700 dark:text-emerald-400" role="status">
            {result?.ok ? result.message : ""}
          </p>
          <button type="submit" disabled={pending} className={buttonClass.primary}>
            {pending ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </form>
  );
}

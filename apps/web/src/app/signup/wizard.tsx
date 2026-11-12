"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { createBusiness, draftFromWebsite } from "@/app/actions";
import {
  BusinessSection,
  FaqSection,
  HoursSection,
  ServicesSection,
  VoiceSection,
} from "@/components/tenant-editor";
import { Card, CardTitle, ErrorNote, Field, buttonClass, cx, inputClass } from "@/components/ui";
import {
  type Area,
  applyDraft,
  greetingFor,
  hoursToForm,
  problemsByArea,
  slugify,
  starterConfig,
  toPayload,
} from "@/lib/tenant-form";
import type { TenantConfig, WebsiteDraft } from "@/lib/types";

// Steps 1-5 build the setup in memory; nothing is saved until "Create my receptionist".
// Then the owner lands on /welcome (signed in) to connect the calendar and make a test call.

const STEPS: { title: string; areas: Area[] }[] = [
  { title: "Your business", areas: ["business"] },
  { title: "Services and FAQ", areas: ["services"] },
  { title: "Opening hours", areas: ["hours"] },
  { title: "Languages", areas: ["voice"] },
  { title: "Your account", areas: [] },
];

export function Wizard() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState<TenantConfig>(() => starterConfig("", ""));
  const [hours, setHours] = useState(() => hoursToForm(config.opening_hours));
  const [slugEdited, setSlugEdited] = useState(false);
  const [website, setWebsite] = useState("");
  const [draft, setDraft] = useState<WebsiteDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, start] = useTransition();

  // Name changes flow into the slug and greetings until the owner edits those themselves.
  function onChange(next: TenantConfig) {
    let updated = next;
    if (next.business_name !== config.business_name) {
      const oldGreeting = greetingFor(config.business_name);
      const greeting = { ...next.greeting };
      const fresh = greetingFor(next.business_name);
      if (!next.greeting.en || next.greeting.en === oldGreeting.en) greeting.en = fresh.en;
      if (!next.greeting.ur || next.greeting.ur === oldGreeting.ur) greeting.ur = fresh.ur;
      updated = { ...updated, greeting, slug: slugEdited ? next.slug : slugify(next.business_name) };
    }
    if (next.slug !== config.slug && next.business_name === config.business_name) setSlugEdited(true);
    setConfig(updated);
    setProblems([]);
  }
  const props = { config, onChange, hours, onHoursChange: setHours };

  function readWebsite() {
    setError(null);
    start(async () => {
      const result = await draftFromWebsite(website);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setDraft(result.draft);
      onChange(applyDraft(config, result.draft));
    });
  }

  function next() {
    const found = problemsByArea(config, hours);
    const blocking = STEPS[step].areas.flatMap((a) => found[a]);
    setProblems(blocking);
    if (!blocking.length) {
      setStep(step + 1);
      window.scrollTo({ top: 0 });
    }
  }

  function create() {
    const found = problemsByArea(config, hours);
    const firstBad = STEPS.findIndex((s) => s.areas.some((a) => found[a].length));
    if (firstBad !== -1) {
      setStep(firstBad);
      setProblems(STEPS[firstBad].areas.flatMap((a) => found[a]));
      return;
    }
    if (password.length < 10) {
      setError("Use a password of at least 10 characters.");
      return;
    }
    setError(null);
    start(async () => {
      const result = await createBusiness({ tenant: toPayload(config, hours), email, password });
      if (!result.ok) {
        setError(result.error);
        return;
      }
      router.push("/welcome");
    });
  }

  return (
    <div className="mt-6">
      <ol className="mb-6 flex flex-wrap gap-x-4 gap-y-2 text-sm" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li key={s.title}>
            <button
              type="button"
              disabled={i > step}
              onClick={() => setStep(i)}
              aria-current={i === step ? "step" : undefined}
              className={cx(
                "flex items-center gap-2",
                i === step ? "font-semibold" : i < step ? "text-zinc-700 hover:underline dark:text-zinc-300" : "text-zinc-400",
              )}
            >
              <span
                className={cx(
                  "flex h-6 w-6 items-center justify-center rounded-full text-xs",
                  i <= step ? "bg-emerald-600 text-white" : "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
                )}
              >
                {i < step ? "✓" : i + 1}
              </span>
              {s.title}
            </button>
          </li>
        ))}
      </ol>

      <div className="space-y-4">
        {step === 0 ? (
          <>
            <BusinessSection {...props} showSlug />
            <Card>
              <CardTitle hint="Optional. We read your site and suggest services, prices and FAQ for you to check. Only what your site actually says is kept.">
                Start from your website
              </CardTitle>
              <form
                className="flex flex-col gap-2 sm:flex-row"
                onSubmit={(e) => {
                  e.preventDefault();
                  readWebsite();
                }}
              >
                <input
                  className={inputClass}
                  inputMode="url"
                  placeholder="www.yourbusiness.pk"
                  value={website}
                  onChange={(e) => setWebsite(e.target.value)}
                  aria-label="Website address"
                />
                <button type="submit" disabled={pending || !website.trim()} className={buttonClass.secondary}>
                  {pending ? "Reading…" : "Read my website"}
                </button>
              </form>
              {draft ? (
                <div className="mt-3 space-y-2 text-sm">
                  <p className="text-emerald-700 dark:text-emerald-400">
                    Found {draft.services.length} services and {draft.faq.length} FAQ answers. Check them on the next step.
                  </p>
                  {draft.opening_hours_text ? (
                    <p>
                      Your site lists these hours: <q>{draft.opening_hours_text}</q>. Set them on step 3.
                    </p>
                  ) : null}
                  {draft.dropped.length ? (
                    <details>
                      <summary className="cursor-pointer text-zinc-600 dark:text-zinc-400">
                        {draft.dropped.length} things left for you to fill in (not confirmed on your site)
                      </summary>
                      <ul className="mt-1 list-disc pl-5 text-zinc-600 dark:text-zinc-400">
                        {draft.dropped.map((d) => (
                          <li key={d}>{d}</li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </div>
              ) : null}
            </Card>
          </>
        ) : null}
        {step === 1 ? (
          <>
            <ServicesSection {...props} />
            <FaqSection {...props} />
          </>
        ) : null}
        {step === 2 ? <HoursSection {...props} /> : null}
        {step === 3 ? <VoiceSection {...props} /> : null}
        {step === 4 ? (
          <Card>
            <CardTitle hint="You'll use these to log in to your dashboard.">Your account</CardTitle>
            <form
              id="account"
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                create();
              }}
            >
              <Field label="Email">
                <input type="email" autoComplete="email" required className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} />
              </Field>
              <Field label="Password" hint="At least 10 characters.">
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={10}
                  className={inputClass}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field>
            </form>
          </Card>
        ) : null}

        {problems.length ? (
          <ErrorNote>
            <span className="block font-medium">Before you continue:</span>
            <span className="block">{problems.join(" ")}</span>
          </ErrorNote>
        ) : null}
        {error ? <ErrorNote>{error}</ErrorNote> : null}

        <div className="flex justify-between gap-3">
          <button type="button" className={buttonClass.secondary} disabled={step === 0 || pending} onClick={() => setStep(step - 1)}>
            Back
          </button>
          {step < STEPS.length - 1 ? (
            <button type="button" className={buttonClass.primary} onClick={next}>
              Next
            </button>
          ) : (
            <button type="submit" form="account" className={buttonClass.primary} disabled={pending}>
              {pending ? "Creating…" : "Create my receptionist"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

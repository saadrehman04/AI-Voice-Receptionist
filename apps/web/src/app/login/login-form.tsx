"use client";

import { useSearchParams } from "next/navigation";
import { useActionState } from "react";

import { login } from "@/app/actions";
import { ErrorNote, Field, buttonClass, inputClass } from "@/components/ui";

export function LoginForm() {
  const [state, action, pending] = useActionState(login, null);
  const expired = useSearchParams().get("expired") === "1";
  return (
    <form action={action} className="mt-6 space-y-4">
      {expired && !state ? <ErrorNote>Your session ended. Please log in again.</ErrorNote> : null}
      {state && !state.ok ? <ErrorNote>{state.error}</ErrorNote> : null}
      <Field label="Email">
        <input name="email" type="email" autoComplete="email" required className={inputClass} />
      </Field>
      <Field label="Password">
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className={inputClass}
        />
      </Field>
      <button type="submit" disabled={pending} className={`${buttonClass.primary} w-full`}>
        {pending ? "Logging in…" : "Log in"}
      </button>
    </form>
  );
}

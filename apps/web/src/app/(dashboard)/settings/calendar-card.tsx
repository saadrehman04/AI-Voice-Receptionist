"use client";

import { useState, useTransition } from "react";

import { saveCalendar } from "@/app/actions";
import { Badge, Card, CardTitle, ErrorNote, Field, buttonClass, inputClass } from "@/components/ui";
import type { CalendarStatus, FormResult } from "@/lib/types";

/** Connect Google Calendar: share it with our service account, paste its id, we check it. */
export function CalendarCard({ status, compact = false }: { status: CalendarStatus; compact?: boolean }) {
  const [calendarId, setCalendarId] = useState(status.calendar_id ?? "");
  const [result, setResult] = useState<FormResult | null>(null);
  const [pending, start] = useTransition();
  const connected = Boolean(status.calendar_id);

  return (
    <Card id="calendar">
      <CardTitle
        hint={
          status.provider === "fake"
            ? "This server uses a practice calendar (CALENDAR_PROVIDER=fake): bookings are kept in our database only. Any calendar id is accepted."
            : "Bookings go straight into your Google Calendar, and anything already in it blocks that time."
        }
      >
        <span className="flex items-center gap-2">
          Google Calendar {connected ? <Badge tone="green">connected</Badge> : <Badge>not connected</Badge>}
        </span>
      </CardTitle>
      {!compact || !connected ? (
        <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-zinc-700 dark:text-zinc-300">
          <li>
            Open Google Calendar → Settings → your calendar → <em>Share with specific people</em>.
          </li>
          <li>
            Add{" "}
            {status.share_with ? (
              <code className="rounded bg-zinc-100 px-1 py-0.5 text-xs dark:bg-zinc-800">{status.share_with}</code>
            ) : (
              "the receptionist's service account"
            )}{" "}
            with <em>Make changes to events</em>.
          </li>
          <li>
            Copy the <em>Calendar ID</em> (under <em>Integrate calendar</em>; for your main calendar it&apos;s your Gmail address) and paste it here.
          </li>
        </ol>
      ) : null}
      <form
        className="flex flex-col gap-2 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          start(async () => setResult(await saveCalendar(calendarId.trim() || null)));
        }}
      >
        <Field label="Calendar ID" className="flex-1">
          <input
            className={inputClass}
            value={calendarId}
            placeholder="you@gmail.com"
            onChange={(e) => {
              setCalendarId(e.target.value);
              setResult(null);
            }}
          />
        </Field>
        <button type="submit" disabled={pending} className={buttonClass.primary}>
          {pending ? "Checking…" : connected ? "Update" : "Connect"}
        </button>
      </form>
      <div className="mt-2">
        {result && !result.ok ? <ErrorNote>{result.error}</ErrorNote> : null}
        {result?.ok ? <p className="text-sm text-emerald-700 dark:text-emerald-400">{result.message}</p> : null}
      </div>
    </Card>
  );
}

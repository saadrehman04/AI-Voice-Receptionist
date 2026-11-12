import Link from "next/link";
import { Suspense } from "react";

import { Badge, Empty, PageHeader, Skeleton, outcomeTone } from "@/components/ui";
import { businessTimeZone, dashboard } from "@/lib/api";
import { callerLabel, formatDateTime, formatDuration, outcomeLabel } from "@/lib/format";

export const metadata = { title: "Calls · Voice AI Receptionist" };

async function CallList() {
  const calls = await dashboard.calls(100);
  const tz = await businessTimeZone();
  if (!calls.length)
    return (
      <Empty title="No calls yet" action={{ href: "/test-call", label: "Make a test call" }}>
        Calls show up here as soon as they end, with a transcript, a summary and what the
        receptionist did.
      </Empty>
    );
  return (
    <ul className="divide-y divide-zinc-200 overflow-hidden rounded-xl border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
      {calls.map((c) => (
        <li key={c.id}>
          <Link
            href={`/calls/${c.id}`}
            className="flex flex-col gap-1 px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-800/60 sm:flex-row sm:items-center sm:gap-4"
          >
            <div className="flex items-center justify-between gap-3 sm:w-64 sm:shrink-0 sm:justify-start">
              <span className="text-sm font-medium tabular-nums">{formatDateTime(c.started_at, tz)}</span>
              <Badge tone={outcomeTone(c.outcome)}>{outcomeLabel(c.outcome)}</Badge>
            </div>
            <p className="min-w-0 flex-1 truncate text-sm text-zinc-700 dark:text-zinc-300">
              {c.summary ?? (c.status === "ended" ? "Summary on its way…" : "Call in progress")}
            </p>
            <div className="flex gap-3 text-xs text-zinc-500 dark:text-zinc-400 sm:w-48 sm:justify-end">
              <span>{callerLabel(c.caller_phone, c.channel)}</span>
              <span className="tabular-nums">{formatDuration(c.duration_secs)}</span>
              {c.channel === "whatsapp" ? <Badge tone="green">WhatsApp</Badge> : null}
              {c.language ? <span className="uppercase">{c.language}</span> : null}
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function CallsPage() {
  return (
    <>
      <PageHeader title="Calls" subtitle="Every call the receptionist answered, newest first." />
      <Suspense fallback={<Skeleton rows={6} />}>
        <CallList />
      </Suspense>
    </>
  );
}

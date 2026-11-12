import Link from "next/link";
import { Suspense } from "react";

import { Card, CardTitle, PageHeader, Skeleton, cx } from "@/components/ui";
import { dashboard } from "@/lib/api";
import { STAGES, formatMs, formatPercent, outcomeLabel, usageLevel } from "@/lib/format";
import type { Usage } from "@/lib/types";

import { CallsChart } from "./calls-chart";

export const metadata = { title: "Analytics · Voice AI Receptionist" };

const RANGES = [7, 30, 90];

function Tile({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <Card>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">{label}</p>
      <p className="mt-1 text-3xl font-semibold tabular-nums">{value}</p>
      <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">{hint}</p>
    </Card>
  );
}

const LEVEL = {
  ok: { bar: "#059669", icon: "✓" },
  near: { bar: "#d97706", icon: "!" },
  over: { bar: "#dc2626", icon: "✕" },
} as const;

function UsageCard({ usage }: { usage: Usage }) {
  const { share, level, label } = usageLevel(usage.minutes, usage.monthly_minutes_limit);
  const month = new Date(`${usage.month}-01T00:00:00`).toLocaleString("en-GB", { month: "long" });
  return (
    <Card className="mt-4">
      <CardTitle hint="Your plan's limits. Over a limit, callers hear who to call instead of being cut off.">
        Usage in {month}
      </CardTitle>
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
        <span>
          <span className="text-2xl font-semibold tabular-nums">{Math.round(usage.minutes)}</span>
          <span className="text-zinc-600 dark:text-zinc-400"> of {usage.monthly_minutes_limit} minutes</span>
        </span>
        <span className="tabular-nums text-zinc-600 dark:text-zinc-400">
          {usage.calls} calls this month · {usage.calls_this_hour} of {usage.calls_per_hour_limit} this hour
        </span>
      </div>
      <div
        role="meter"
        aria-label="Minutes used this month"
        aria-valuemin={0}
        aria-valuemax={usage.monthly_minutes_limit}
        aria-valuenow={Math.round(usage.minutes)}
        className="mt-2 h-2 rounded-full bg-zinc-100 dark:bg-zinc-800"
      >
        <div className="h-2 rounded-full" style={{ width: `${Math.max(share * 100, 1)}%`, background: LEVEL[level].bar }} />
      </div>
      <p className="mt-2 text-xs text-zinc-600 dark:text-zinc-400">
        <span aria-hidden="true">{LEVEL[level].icon} </span>
        {label}
      </p>
    </Card>
  );
}

async function Report({ searchParams }: { searchParams: Promise<{ days?: string }> }) {
  const { days: raw } = await searchParams;
  const days = RANGES.includes(Number(raw)) ? Number(raw) : 7;
  const [stats, usage] = await Promise.all([
    dashboard.stats(days),
    dashboard.usage().catch(() => null), // an API without usage counters: skip the card
  ]);
  const outcomes = Object.entries(stats.by_outcome).sort((a, b) => b[1] - a[1]);
  const top = Math.max(1, ...outcomes.map(([, n]) => n));
  const booked = stats.by_outcome.booked ?? 0;

  return (
    <>
      <div className="mb-4 flex gap-1" role="group" aria-label="Time range">
        {RANGES.map((r) => (
          <Link
            key={r}
            href={`/analytics?days=${r}`}
            aria-current={r === days ? "true" : undefined}
            className={cx(
              "rounded-lg px-3 py-1.5 text-sm font-medium",
              r === days
                ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800",
            )}
          >
            {r} days
          </Link>
        ))}
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Tile label="Calls answered" value={String(stats.calls)} hint={`in the last ${days} days`} />
        <Tile
          label="Booking rate"
          value={formatPercent(stats.booking_rate)}
          hint={`${booked} booked, of the calls summarised so far`}
        />
        <Tile
          label="Missed calls saved"
          value={String(stats.after_hours)}
          hint="answered while you were closed"
        />
      </div>
      {usage ? <UsageCard usage={usage} /> : null}
      <div className="mt-4 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardTitle hint="Hover or tab to a day for its numbers.">Calls per day</CardTitle>
          <CallsChart days={stats.per_day} />
        </Card>
        <Card>
          <CardTitle>What callers got</CardTitle>
          {outcomes.length ? (
            <ul className="space-y-2">
              {outcomes.map(([outcome, n]) => (
                <li key={outcome} className="text-sm">
                  <div className="flex justify-between">
                    <span>{outcomeLabel(outcome)}</span>
                    <span className="tabular-nums text-zinc-600 dark:text-zinc-400">{n}</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded-full bg-zinc-100 dark:bg-zinc-800">
                    <div className="h-1.5 rounded-full" style={{ width: `${(n / top) * 100}%`, background: "#059669" }} />
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-zinc-500">No calls in this period.</p>
          )}
        </Card>
      </div>
      <Card className="mt-4">
        <CardTitle hint="How long callers wait, per agent turn. P50 is a typical turn; P95 is a slow one.">
          Response time
        </CardTitle>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-zinc-500">
              <tr>
                <th className="py-2 pr-4 font-medium">Stage</th>
                <th className="py-2 pr-4 text-right font-medium">P50</th>
                <th className="py-2 pr-4 text-right font-medium">P95</th>
                <th className="py-2 text-right font-medium">Turns</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
              {STAGES.map(({ key, label }) => {
                const s = stats.latency_ms[key];
                return (
                  <tr key={key} className={key === "total" ? "font-semibold" : ""}>
                    <td className="py-2 pr-4">{label}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{formatMs(s?.p50)}</td>
                    <td className="py-2 pr-4 text-right tabular-nums">{formatMs(s?.p95)}</td>
                    <td className="py-2 text-right tabular-nums text-zinc-500">{s?.n ?? 0}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

export default function AnalyticsPage(props: PageProps<"/analytics">) {
  return (
    <>
      <PageHeader title="Analytics" subtitle="How the receptionist is doing for your business." />
      <Suspense fallback={<Skeleton rows={4} />}>
        <Report searchParams={props.searchParams as Promise<{ days?: string }>} />
      </Suspense>
    </>
  );
}

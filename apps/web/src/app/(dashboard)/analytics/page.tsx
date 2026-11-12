import Link from "next/link";
import { Suspense } from "react";

import { Card, CardTitle, PageHeader, Skeleton, cx } from "@/components/ui";
import { dashboard } from "@/lib/api";
import { STAGES, formatMs, formatPercent, outcomeLabel } from "@/lib/format";

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

async function Report({ searchParams }: { searchParams: Promise<{ days?: string }> }) {
  const { days: raw } = await searchParams;
  const days = RANGES.includes(Number(raw)) ? Number(raw) : 7;
  const stats = await dashboard.stats(days);
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

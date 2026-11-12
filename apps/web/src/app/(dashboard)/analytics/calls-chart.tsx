"use client";

import { useState } from "react";

import { formatDay } from "@/lib/format";

// Calls per day as thin bars, one series (so no legend: the card title names it).
// Bar colour #059669 passes the dataviz palette checks on both the light and dark surface.
// Every day column is a hover/focus target taller than its bar; a table copy is below for
// screen readers.

const BAR = "#059669";
const H = 160;

export function CallsChart({ days }: { days: { day: string; calls: number; booked: number }[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...days.map((d) => d.calls));
  const ticks = max <= 4 ? Array.from({ length: max + 1 }, (_, i) => i) : [0, Math.round(max / 2), max];
  const labelEvery = days.length > 14 ? Math.ceil(days.length / 7) : 1;

  return (
    <div>
      <div className="relative" style={{ height: H + 24 }}>
        {/* grid + y labels */}
        {ticks.map((t) => (
          <div
            key={t}
            className="absolute inset-x-0 flex items-center gap-2"
            style={{ bottom: 24 + (t / max) * H - 6 }}
            aria-hidden
          >
            <span className="w-6 text-right text-[11px] tabular-nums text-zinc-500">{t}</span>
            <span className="h-px flex-1 bg-zinc-100 dark:bg-zinc-800" />
          </div>
        ))}
        <div className="absolute inset-y-0 left-8 right-0 flex items-end gap-[2px]" style={{ bottom: 24 }}>
          {days.map((d, i) => (
            <button
              key={d.day}
              type="button"
              aria-label={`${formatDay(d.day)}: ${d.calls} calls, ${d.booked} booked`}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
              className="group relative flex h-full flex-1 items-end justify-center focus:outline-none"
              style={{ bottom: 0 }}
            >
              <span
                className="block w-full max-w-6 rounded-t"
                style={{
                  height: d.calls ? Math.max(3, (d.calls / max) * H) : 0,
                  background: BAR,
                  opacity: hover === null || hover === i ? 1 : 0.45,
                }}
              />
              {hover === i ? (
                <span
                  role="status"
                  className={`pointer-events-none absolute top-0 z-10 whitespace-nowrap rounded-lg border border-zinc-200 bg-white px-3 py-2 text-left text-xs shadow-md dark:border-zinc-700 dark:bg-zinc-900 ${
                    i < days.length / 2 ? "left-0" : "right-0"
                  }`}
                >
                  <span className="block font-medium">{formatDay(d.day)}</span>
                  <span className="block tabular-nums">
                    {d.calls} calls · {d.booked} booked
                  </span>
                </span>
              ) : null}
              {i % labelEvery === 0 || i === days.length - 1 ? (
                <span className="absolute -bottom-5 whitespace-nowrap text-[11px] text-zinc-500">{formatDay(d.day)}</span>
              ) : null}
            </button>
          ))}
        </div>
      </div>
      <table className="sr-only">
        <caption>Calls per day</caption>
        <thead>
          <tr>
            <th>Day</th>
            <th>Calls</th>
            <th>Booked</th>
          </tr>
        </thead>
        <tbody>
          {days.map((d) => (
            <tr key={d.day}>
              <td>{d.day}</td>
              <td>{d.calls}</td>
              <td>{d.booked}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

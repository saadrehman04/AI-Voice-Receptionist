import Link from "next/link";
import { Suspense } from "react";

import { Badge, Empty, PageHeader, Skeleton } from "@/components/ui";
import { businessTimeZone, dashboard } from "@/lib/api";
import { formatDateTime, formatPhone } from "@/lib/format";

export const metadata = { title: "Bookings · Voice AI Receptionist" };

async function BookingList() {
  const bookings = await dashboard.bookings(true);
  const tz = await businessTimeZone();
  if (!bookings.length)
    return (
      <Empty title="No upcoming bookings">
        Appointments the receptionist books show up here and in your calendar.
      </Empty>
    );
  return (
    <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <table className="w-full text-left text-sm">
        <thead className="hidden bg-zinc-50 text-xs uppercase text-zinc-500 dark:bg-zinc-950 sm:table-header-group">
          <tr>
            <th className="px-4 py-2 font-medium">When</th>
            <th className="px-4 py-2 font-medium">Service</th>
            <th className="px-4 py-2 font-medium">Customer</th>
            <th className="px-4 py-2 font-medium">Code</th>
            <th className="px-4 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {bookings.map((b) => (
            <tr key={b.code} className="flex flex-wrap gap-x-3 px-4 py-3 sm:table-row sm:p-0">
              <td className="w-full font-medium tabular-nums sm:w-auto sm:px-4 sm:py-3">{formatDateTime(b.start, tz)}</td>
              <td className="sm:px-4 sm:py-3">{b.service}</td>
              <td className="sm:px-4 sm:py-3">
                {b.customer_name}{" "}
                <span className="text-zinc-500 dark:text-zinc-400">{formatPhone(b.customer_phone)}</span>
              </td>
              <td className="font-mono text-xs sm:px-4 sm:py-3">{b.code}</td>
              <td className="sm:px-4 sm:py-3">
                <Badge tone={b.status === "cancelled" ? "red" : "green"}>{b.status}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function BookingsPage() {
  return (
    <>
      <PageHeader
        title="Bookings"
        subtitle={
          <>
            Upcoming appointments. To change one, call your own number, or edit it in your{" "}
            <Link href="/settings#calendar" className="underline">calendar</Link>.
          </>
        }
      />
      <Suspense fallback={<Skeleton rows={5} />}>
        <BookingList />
      </Suspense>
    </>
  );
}

import Link from "next/link";
import { Suspense } from "react";

import { Card, CardTitle, PageHeader, Skeleton, buttonClass } from "@/components/ui";
import { dashboard } from "@/lib/api";

import { CalendarCard } from "../settings/calendar-card";

export const metadata = { title: "Welcome · Voice AI Receptionist" };

async function Calendar() {
  return <CalendarCard status={await dashboard.calendar()} />;
}

export default function WelcomePage() {
  return (
    <>
      <PageHeader title="Your receptionist is ready" subtitle="Two last steps, then it can take real calls." />
      <div className="grid gap-4 lg:grid-cols-2">
        <Suspense fallback={<Skeleton rows={2} />}>
          <Calendar />
        </Suspense>
        <Card className="h-fit">
          <CardTitle hint="Hear exactly what your callers will hear. The call then shows up on your Calls page.">
            Make a test call
          </CardTitle>
          <Link href="/test-call" className={buttonClass.primary}>
            Start a test call
          </Link>
        </Card>
      </div>
      <p className="mt-6 text-sm text-zinc-600 dark:text-zinc-400">
        You can come back to all of this from{" "}
        <Link href="/settings" className="underline">Settings</Link>.
      </p>
    </>
  );
}

import { Suspense } from "react";

import { PageHeader, Skeleton } from "@/components/ui";
import { dashboard } from "@/lib/api";

import { CalendarCard } from "./calendar-card";
import { SettingsForm } from "./settings-form";

export const metadata = { title: "Settings · Voice AI Receptionist" };

async function Settings() {
  const [config, calendar] = await Promise.all([dashboard.settings(), dashboard.calendar()]);
  return (
    <div className="space-y-4">
      <SettingsForm initial={config} />
      <CalendarCard status={calendar} />
    </div>
  );
}

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Everything the receptionist knows about your business. It never makes up anything that isn't here."
      />
      <Suspense fallback={<Skeleton rows={6} />}>
        <Settings />
      </Suspense>
    </>
  );
}

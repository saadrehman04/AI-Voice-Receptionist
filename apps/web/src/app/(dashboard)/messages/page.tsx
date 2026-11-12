import { Suspense } from "react";

import { Badge, Empty, PageHeader, Skeleton } from "@/components/ui";
import { dashboard } from "@/lib/api";
import { formatDateTime, formatPhone } from "@/lib/format";

import { MessageToggle } from "./message-toggle";

export const metadata = { title: "Messages · Voice AI Receptionist" };

async function MessageList() {
  const messages = await dashboard.messages();
  if (!messages.length)
    return (
      <Empty title="No messages">
        When a caller wants something the receptionist can&apos;t do, or asks for a person, it
        takes a message and it shows up here.
      </Empty>
    );
  const open = messages.filter((m) => m.status === "new").length;
  return (
    <>
      <p className="mb-3 text-sm text-zinc-600 dark:text-zinc-400">
        {open ? `${open} to follow up` : "All followed up."}
      </p>
      <ul className="space-y-3">
        {messages.map((m) => (
          <li
            key={m.id}
            className="flex flex-col gap-2 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900 sm:flex-row sm:items-start sm:justify-between"
          >
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge tone={m.kind === "handoff" ? "amber" : "blue"}>
                  {m.kind === "handoff" ? "Wants a person" : "Message"}
                </Badge>
                <span className="font-medium">{m.caller_name ?? "Caller"}</span>
                <span className="text-zinc-500 dark:text-zinc-400">{formatPhone(m.caller_phone)}</span>
                <span className="text-xs text-zinc-500 dark:text-zinc-400">{formatDateTime(m.created_at)}</span>
              </div>
              <p dir="auto" className={`mt-2 text-sm ${m.status === "done" ? "text-zinc-500 line-through" : ""}`}>
                {m.body}
              </p>
            </div>
            <MessageToggle id={m.id} status={m.status} />
          </li>
        ))}
      </ul>
    </>
  );
}

export default function MessagesPage() {
  return (
    <>
      <PageHeader title="Messages" subtitle="Callers waiting for you to get back to them." />
      <Suspense fallback={<Skeleton rows={4} />}>
        <MessageList />
      </Suspense>
    </>
  );
}

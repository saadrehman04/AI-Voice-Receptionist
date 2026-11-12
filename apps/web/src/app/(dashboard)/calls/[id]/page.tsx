import Link from "next/link";
import { notFound } from "next/navigation";
import { Suspense } from "react";

import { Badge, Card, CardTitle, PageHeader, Skeleton, cx, outcomeTone } from "@/components/ui";
import { ApiError, businessTimeZone, dashboard } from "@/lib/api";
import {
  formatDateTime,
  formatDuration,
  formatMs,
  callerLabel,
  formatTime,
  outcomeLabel,
} from "@/lib/format";
import type { CallDetail } from "@/lib/types";

export const metadata = { title: "Call · Voice AI Receptionist" };

async function load(id: string): Promise<CallDetail> {
  try {
    return await dashboard.call(id);
  } catch (err) {
    // Unknown ids, malformed ids and other businesses' calls all look the same: not found.
    if (err instanceof ApiError && (err.status === 404 || err.status === 422)) notFound();
    throw err;
  }
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="mt-1 overflow-x-auto rounded-lg bg-zinc-50 p-2 text-xs dark:bg-zinc-950">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

async function Detail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const call = await load(id);
  const tz = await businessTimeZone();
  return (
    <>
      <PageHeader
        title={formatDateTime(call.started_at, tz)}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone={outcomeTone(call.outcome)}>{outcomeLabel(call.outcome)}</Badge>
            <span>{callerLabel(call.caller_phone, call.channel)}</span>
            <span>·</span>
            <span>{formatDuration(call.duration_secs)}</span>
            {call.sentiment ? <span>· caller {call.sentiment}</span> : null}
          </span>
        }
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardTitle>Summary</CardTitle>
            <p className="text-sm">{call.summary ?? "The summary appears a few seconds after the call ends."}</p>
          </Card>
          {call.has_recording ? (
            <Card>
              <CardTitle hint="Caller on the left channel, receptionist on the right.">Recording</CardTitle>
              <audio controls preload="none" src={`/api/recordings/${call.id}`} className="w-full" />
            </Card>
          ) : null}
          <Card>
            <CardTitle hint="Agent turns show how long the caller waited for the first word.">
              Transcript
            </CardTitle>
            {call.turns.length ? (
              <ol className="space-y-3">
                {call.turns.map((t) => {
                  const agent = t.speaker === "agent";
                  return (
                    <li key={t.idx} className={cx("flex", agent ? "justify-start" : "justify-end")}>
                      <div
                        className={cx(
                          "max-w-[85%] rounded-2xl px-3 py-2 text-sm",
                          agent
                            ? "rounded-bl-sm bg-zinc-100 dark:bg-zinc-800"
                            : "rounded-br-sm bg-emerald-600 text-white",
                        )}
                      >
                        <p dir="auto" className="whitespace-pre-wrap">{t.text}</p>
                        <p className={cx("mt-1 text-[11px]", agent ? "text-zinc-500" : "text-emerald-100")}>
                          {agent ? "Receptionist" : "Caller"} · {formatTime(t.spoken_at, tz)}
                          {agent && t.timings?.total !== undefined ? ` · answered in ${formatMs(t.timings.total)}` : ""}
                          {t.interrupted ? " · interrupted" : ""}
                        </p>
                      </div>
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="text-sm text-zinc-500">Nobody spoke on this call.</p>
            )}
          </Card>
        </div>
        <Card className="h-fit">
          <CardTitle hint="What the receptionist actually did, in order.">Actions</CardTitle>
          {call.tool_calls.length ? (
            <ol className="space-y-3">
              {call.tool_calls.map((tc, i) => (
                <li key={i} className="text-sm">
                  <details>
                    <summary className="cursor-pointer">
                      <span className="font-mono text-xs">{tc.name}</span>{" "}
                      <Badge tone={tc.ok ? "green" : "red"}>{tc.ok ? "ok" : "failed"}</Badge>{" "}
                      <span className="text-xs text-zinc-500">{tc.duration_ms} ms</span>
                    </summary>
                    <p className="mt-2 text-xs font-medium text-zinc-500">Asked with</p>
                    <Json value={tc.arguments} />
                    <p className="mt-2 text-xs font-medium text-zinc-500">Got back</p>
                    <Json value={tc.result} />
                  </details>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-zinc-500">No actions: questions only.</p>
          )}
        </Card>
      </div>
    </>
  );
}

export default function CallPage(props: PageProps<"/calls/[id]">) {
  return (
    <>
      <Link href="/calls" className="mb-4 inline-block text-sm text-zinc-600 hover:underline dark:text-zinc-400">
        ← All calls
      </Link>
      <Suspense fallback={<Skeleton rows={5} />}>
        <Detail params={props.params} />
      </Suspense>
    </>
  );
}

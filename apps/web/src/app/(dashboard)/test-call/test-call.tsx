"use client";

import { PipecatClient, type TransportState } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { Card, ErrorNote, buttonClass, cx } from "@/components/ui";

// An agent turn is a list of sentences (segments). Pipecat sends each sentence's progress
// word by word as it's spoken (same segment_id, growing accumulated_text), so a sentence is
// updated in place and shows only what the caller has actually heard.
type Line = { who: "caller"; text: string } | { who: "agent"; parts: { id: number; text: string }[] };

const LIVE: TransportState[] = ["connecting", "connected", "ready", "authenticating", "authenticated", "initializing", "initialized"];

export function TestCall() {
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<TransportState>("disconnected");
  const [lines, setLines] = useState<Line[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);

  useEffect(() => () => void clientRef.current?.disconnect(), []);

  function addCaller(text: string) {
    setLines((prev) => [...prev, { who: "caller", text }]);
  }

  function upsertAgent(id: number, text: string) {
    setLines((prev) => {
      const last = prev.at(-1);
      if (last?.who !== "agent") return [...prev, { who: "agent", parts: [{ id, text }] }];
      const parts = last.parts.some((p) => p.id === id)
        ? last.parts.map((p) => (p.id === id ? { id, text } : p))
        : [...last.parts, { id, text }];
      return [...prev.slice(0, -1), { who: "agent", parts }];
    });
  }

  async function startCall() {
    setError(null);
    setLines([]);
    setEnded(false);
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onTransportStateChanged: setState,
        onTrackStarted: (track, participant) => {
          if (participant?.local || track.kind !== "audio" || !audioRef.current) return;
          audioRef.current.srcObject = new MediaStream([track]);
          void audioRef.current.play().catch(() => undefined);
        },
        onUserTranscript: (d) => {
          if (d.final && d.text.trim()) addCaller(d.text.trim());
        },
        onBotOutput: (d) => {
          if (d.aggregated_by === "word" || !d.text.trim()) return;
          const spoken = d.spoken_progress?.accumulated_text ?? d.text;
          upsertAgent(d.segment_id ?? Date.now(), spoken.trim());
        },
        onDisconnected: () => setEnded(true),
        onError: (m) => setError(String((m.data as { message?: string } | undefined)?.message ?? "the call failed")),
      },
    });
    clientRef.current = client;
    try {
      await client.startBotAndConnect({ endpoint: "/api/test-call/start" });
    } catch (err) {
      setState("disconnected");
      setError(
        err instanceof Error && /permission|notallowed/i.test(err.message)
          ? "Allow the microphone for this page, then try again."
          : "Couldn't start the call. Is the voice agent running (make agent)?",
      );
    }
  }

  async function hangUp() {
    await clientRef.current?.disconnect();
    setEnded(true);
  }

  const live = LIVE.includes(state);
  const label = state === "ready" ? "Connected: say hello" : live ? "Connecting…" : ended ? "Call ended" : "Not connected";

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="h-fit">
        <div className="flex items-center gap-2 text-sm">
          <span
            className={cx(
              "h-2.5 w-2.5 rounded-full",
              state === "ready" ? "bg-emerald-500" : live ? "animate-pulse bg-amber-500" : "bg-zinc-400",
            )}
          />
          <span role="status">{label}</span>
        </div>
        <div className="mt-4 flex gap-2">
          {live ? (
            <button type="button" onClick={hangUp} className={`${buttonClass.primary} bg-red-600 hover:bg-red-700`}>
              Hang up
            </button>
          ) : (
            <button type="button" onClick={startCall} className={buttonClass.primary}>
              {ended ? "Call again" : "Start test call"}
            </button>
          )}
        </div>
        <p className="mt-4 text-xs text-zinc-500 dark:text-zinc-400">
          Uses your microphone. Try booking, asking a price, or switching to Urdu mid-call. Speak over the
          receptionist to interrupt it.
        </p>
        {error ? <div className="mt-3"><ErrorNote>{error}</ErrorNote></div> : null}
        {ended && !error ? (
          <p className="mt-3 text-sm">
            It&apos;s on your <Link href="/calls" className="font-medium text-emerald-700 underline dark:text-emerald-400">Calls</Link> page now (the summary takes a few seconds).
          </p>
        ) : null}
        <audio ref={audioRef} autoPlay />
      </Card>
      <Card className="lg:col-span-2">
        <h2 className="mb-3 text-base font-semibold">Live transcript</h2>
        {lines.length ? (
          <ol className="space-y-2">
            {lines.map((l, i) => (
              <li key={i} className={cx("flex", l.who === "agent" ? "justify-start" : "justify-end")}>
                <p
                  dir="auto"
                  className={cx(
                    "max-w-[85%] rounded-2xl px-3 py-2 text-sm",
                    l.who === "agent" ? "rounded-bl-sm bg-zinc-100 dark:bg-zinc-800" : "rounded-br-sm bg-emerald-600 text-white",
                  )}
                >
                  {l.who === "agent" ? l.parts.map((p) => p.text).join(" ") : l.text}
                </p>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-sm text-zinc-500">What you and the receptionist say appears here.</p>
        )}
      </Card>
    </div>
  );
}

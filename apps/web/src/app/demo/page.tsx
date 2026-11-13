import Link from "next/link";

import { TestCall } from "@/components/test-call";
import { buttonClass } from "@/components/ui";

export const metadata = { title: "Live demo · Voice AI Receptionist" };

// Public: no sign-in. Calls go to the fictional Sunrise Dental Studio (tenants/demo-dental.yaml).
export default function DemoPage() {
  return (
    <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6 sm:py-12">
      <Link href="/" className="text-sm text-zinc-500 hover:underline dark:text-zinc-400">
        ← Voice AI Receptionist
      </Link>
      <h1 className="mt-3 text-3xl font-semibold tracking-tight">Call the demo dental clinic</h1>
      <p className="mt-2 max-w-3xl text-zinc-600 dark:text-zinc-400">
        Sunrise Dental Studio is a made-up clinic in Austin, Texas. Talk to its AI receptionist the
        way a patient would: it answers from the clinic&apos;s own prices, hours and policies, books,
        moves and cancels appointments, and takes a message when it can&apos;t help.
      </p>
      <div className="mt-6">
        <TestCall
          endpoint="/api/demo-call/start"
          startLabel="Start demo call"
          tips={
            <>
              Uses your microphone. Try: &ldquo;Can I book a cleaning next Tuesday morning?&rdquo;,
              &ldquo;How much is teeth whitening?&rdquo;, &ldquo;Do you take insurance?&rdquo; or
              &ldquo;I have a toothache.&rdquo; Speak over it to interrupt. Give a phone number with
              its country code if it isn&apos;t a US number. Nothing here reaches a real clinic.
            </>
          }
          afterCall={
            <>
              Want one for your business?{" "}
              <Link href="/signup" className="font-medium text-emerald-700 underline dark:text-emerald-400">
                Set it up in a few minutes
              </Link>
              .
            </>
          }
          unreachable="Couldn't start the demo call right now. Please try again in a minute."
        />
      </div>
      <div className="mt-8">
        <Link href="/signup" className={buttonClass.secondary}>
          Set up your own business
        </Link>
      </div>
    </main>
  );
}

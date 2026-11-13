import Link from "next/link";

import { buttonClass } from "@/components/ui";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-6 px-4 py-16 sm:px-6">
      <div>
        <p className="text-sm font-medium text-emerald-700 dark:text-emerald-400">Voice AI Receptionist</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
          Your phone answered, day and night.
        </h1>
        <p className="mt-3 text-lg text-zinc-600 dark:text-zinc-400">
          It books, moves and cancels appointments in your calendar, answers questions from your
          own price list and FAQ, and takes a message when it isn&apos;t sure.
        </p>
      </div>
      <div className="flex flex-wrap gap-3">
        <Link href="/demo" className={buttonClass.primary}>
          Try a live demo call
        </Link>
        <Link href="/signup" className={buttonClass.secondary}>
          Set up your business
        </Link>
        <Link href="/login" className={buttonClass.secondary}>
          Log in
        </Link>
      </div>
    </main>
  );
}

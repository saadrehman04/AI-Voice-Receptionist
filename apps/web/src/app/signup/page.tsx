import Link from "next/link";

import { Wizard } from "./wizard";

export const metadata = { title: "Set up your business · Voice AI Receptionist" };

export default function SignupPage() {
  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-8 sm:px-6 sm:py-12">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Already set up?{" "}
        <Link href="/login" className="font-medium text-emerald-700 hover:underline dark:text-emerald-400">
          Log in
        </Link>
      </p>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">Set up your receptionist</h1>
      <p className="mt-1 text-zinc-600 dark:text-zinc-400">
        About five minutes. Everything can be changed later in Settings.
      </p>
      <Wizard />
    </main>
  );
}

import Link from "next/link";
import { Suspense } from "react";

import { LoginForm } from "./login-form";

export const metadata = { title: "Log in · Voice AI Receptionist" };

export default function LoginPage() {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center px-4 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">Log in</h1>
      <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
        New here?{" "}
        <Link href="/signup" className="font-medium text-emerald-700 hover:underline dark:text-emerald-400">
          Set up your business
        </Link>
      </p>
      <Suspense>
        <LoginForm />
      </Suspense>
    </main>
  );
}

import { Suspense } from "react";

import { logout } from "@/app/actions";
import { Nav, NavLinks } from "@/components/nav";
import { buttonClass } from "@/components/ui";
import { dashboard } from "@/lib/api";

// The shell (header, nav) is static and shows at once; the business name needs the session,
// so it streams in behind its own boundary (Cache Components: no request data outside one).

async function BusinessName() {
  const me = await dashboard.me();
  return (
    <span className="min-w-0">
      <span className="block truncate font-semibold">{me.business_name}</span>
      <span className="block truncate text-xs text-zinc-500 dark:text-zinc-400">{me.email}</span>
    </span>
  );
}

export default function DashboardLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="flex min-h-full flex-1 flex-col">
      <header className="sticky top-0 z-10 border-b border-zinc-200 bg-white/90 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/90">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-3 px-4 py-3 sm:px-6">
          <div className="flex items-center justify-between gap-3">
            <Suspense fallback={<span className="h-9 w-40 animate-pulse rounded bg-zinc-100 dark:bg-zinc-800" />}>
              <BusinessName />
            </Suspense>
            <form action={logout}>
              <button type="submit" className={buttonClass.ghost}>
                Log out
              </button>
            </form>
          </div>
          <Suspense fallback={<NavLinks pathname={null} />}>
            <Nav />
          </Suspense>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-8">{children}</main>
    </div>
  );
}

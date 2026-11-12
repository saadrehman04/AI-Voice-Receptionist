"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cx } from "./ui";

export const NAV = [
  { href: "/calls", label: "Calls" },
  { href: "/bookings", label: "Bookings" },
  { href: "/messages", label: "Messages" },
  { href: "/analytics", label: "Analytics" },
  { href: "/settings", label: "Settings" },
  { href: "/test-call", label: "Test call" },
];

/** The links; `pathname` null = no highlight (the static shell, before the URL is known). */
export function NavLinks({ pathname }: { pathname: string | null }) {
  return (
    <nav aria-label="Dashboard" className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <ul className="flex gap-1">
        {NAV.map((item) => {
          const active = pathname !== null && (pathname === item.href || pathname.startsWith(`${item.href}/`));
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cx(
                  "block whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium",
                  active
                    ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                    : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

export function Nav() {
  return <NavLinks pathname={usePathname()} />;
}

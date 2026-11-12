"use client";

import { useTransition } from "react";

import { setMessageStatus } from "@/app/actions";
import { buttonClass } from "@/components/ui";

export function MessageToggle({ id, status }: { id: string; status: "new" | "done" }) {
  const [pending, start] = useTransition();
  const next = status === "new" ? "done" : "new";
  return (
    <button
      type="button"
      disabled={pending}
      onClick={() => start(() => setMessageStatus(id, next))}
      className={`${buttonClass.secondary} shrink-0`}
    >
      {pending ? "Saving…" : status === "new" ? "Mark done" : "Reopen"}
    </button>
  );
}

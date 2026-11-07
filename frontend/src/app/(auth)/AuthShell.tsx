import Link from "next/link";
import type { ReactNode } from "react";

import { Logo } from "@/components/icons";

/** Centered card layout shared by sign-in, sign-up and the email-link pages. */
export function AuthShell({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-[380px]">
        <Link href="/welcome" className="mb-6 flex w-fit items-center gap-2">
          <Logo className="text-ink" />
          <span className="font-serif text-lg font-medium">QueueForge</span>
        </Link>
        <div className="rounded-[4px] border border-line bg-surface p-5">
          <h1 className="text-base font-semibold">{title}</h1>
          {subtitle ? <p className="mt-1 mb-4 text-xs text-muted">{subtitle}</p> : <div className="mb-4" />}
          {children}
        </div>
        {footer ? <p className="mt-4 text-center text-xs text-muted">{footer}</p> : null}
      </div>
    </main>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "error" | "success"; children: ReactNode }) {
  const cls =
    tone === "error"
      ? "border-bad bg-bad-tint/60 text-bad"
      : tone === "success"
        ? "border-ok bg-ok-tint/60 text-ok"
        : "border-ochre bg-ochre-tint/50 text-ink-2";
  return (
    <p role={tone === "error" ? "alert" : "status"} className={`mt-3 border-l-2 px-3 py-2 text-xs ${cls}`}>
      {children}
    </p>
  );
}

export function AuthLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link className="text-copper-strong hover:underline" href={href}>
      {children}
    </Link>
  );
}

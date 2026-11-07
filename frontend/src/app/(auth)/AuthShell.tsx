import Link from "next/link";
import type { ReactNode } from "react";

import { Logo } from "@/components/icons";

/** Sign-in, sign-up and email-link pages: a single focused column, no decorative card. */
export function AuthShell({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-[1180px] items-center px-4 sm:px-8">
        <Link href="/welcome" className="flex items-center gap-2">
          <Logo className="text-ink" />
          <span className="text-[15px] font-semibold tracking-[-0.02em]">queueforge</span>
        </Link>
      </header>
      <main id="main" className="flex flex-1 items-start justify-center px-4 pt-[9vh] pb-16">
        <div className="w-full max-w-[380px]">
          <h1 className="text-[26px] leading-tight font-semibold tracking-[-0.025em]">{title}</h1>
          {subtitle ? <p className="mt-2 mb-7 text-[13.5px] leading-relaxed text-muted">{subtitle}</p> : <div className="mb-7" />}
          {children}
          {footer ? <p className="mt-8 border-t border-line pt-4 text-[13px] text-muted">{footer}</p> : null}
        </div>
      </main>
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "error" | "success"; children: ReactNode }) {
  const cls =
    tone === "error"
      ? "border-bad text-bad"
      : tone === "success"
        ? "border-ok text-ink"
        : "border-ochre text-ink-2";
  return (
    <p role={tone === "error" ? "alert" : "status"} className={`mt-4 border-l-2 bg-surface py-2 pr-3 pl-3 text-[12.5px] ${cls}`}>
      {children}
    </p>
  );
}

export function AuthLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link className="font-medium text-ink underline decoration-line-strong underline-offset-[3px] transition-colors hover:decoration-copper" href={href}>
      {children}
    </Link>
  );
}

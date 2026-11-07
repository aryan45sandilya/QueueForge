"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import useSWR from "swr";

import type { FailureGroup } from "@/lib/api";
import { useLive } from "@/lib/live";
import { useSession } from "@/lib/session";

import {
  IconClose,
  IconDocs,
  IconFailed,
  IconJobs,
  IconKey,
  IconLogout,
  IconMenu,
  IconOverview,
  IconQueues,
  IconSchedules,
  IconSettings,
  Logo,
} from "./icons";
import { cx } from "./ui";

type NavItem = { href: string; label: string; icon: (p: { className?: string }) => ReactNode; badge?: "failures" };

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: "operate",
    items: [
      { href: "/", label: "Overview", icon: IconOverview },
      { href: "/jobs", label: "Jobs", icon: IconJobs },
      { href: "/failures", label: "Failed jobs", icon: IconFailed, badge: "failures" },
      { href: "/queues", label: "Queues & workers", icon: IconQueues },
      { href: "/schedules", label: "Schedules", icon: IconSchedules },
    ],
  },
  {
    section: "configure",
    items: [
      { href: "/project", label: "Project & API keys", icon: IconKey },
      { href: "/settings", label: "Settings", icon: IconSettings },
      { href: "/docs", label: "Developer docs", icon: IconDocs },
    ],
  },
];

const TITLES: Record<string, string> = {
  "": "overview",
  jobs: "jobs",
  failures: "failed jobs",
  queues: "queues & workers",
  schedules: "schedules",
  project: "project & keys",
  settings: "settings",
  docs: "docs",
  account: "account",
};

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function LiveIndicator() {
  const { status } = useLive();
  const label = status === "live" ? "live" : status === "polling" ? "polling · 10s" : "connecting";
  const title =
    status === "live"
      ? "Receiving job updates over Server-Sent Events"
      : status === "polling"
        ? "Live stream unavailable; refreshing every 10 seconds"
        : "Opening live update stream";
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-muted" title={title} role="status">
      <span
        aria-hidden="true"
        className={cx(
          "size-[7px] rounded-[1px]",
          status === "live" ? "bg-ok qf-pulse" : status === "polling" ? "bg-ochre" : "bg-line-strong",
        )}
      />
      {label}
    </span>
  );
}

function ProjectSwitcher() {
  const { projects, project, selectProject } = useSession();
  return (
    <label className="mx-3 block rounded-[3px] border border-line bg-surface px-2.5 pt-1.5 pb-1 transition-colors focus-within:border-copper hover:border-line-strong">
      <span className="qf-label block">project</span>
      <select
        value={project?.id ?? ""}
        onChange={(e) => selectProject(e.target.value)}
        className="-ml-1 h-6 w-full cursor-pointer appearance-none bg-transparent pl-1 text-[13px] font-semibold focus:outline-none"
      >
        {projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function FailureCount({ projectId }: { projectId: string }) {
  const { data } = useSWR<FailureGroup[]>(`/projects/${projectId}/failures`, { refreshInterval: 30_000 });
  const total = (data ?? []).reduce((n, g) => n + g.count, 0);
  if (!total) return null;
  return (
    <span className="ml-auto font-mono text-[11px] text-bad num" aria-label={`${total} failed jobs`}>
      {total > 999 ? "999+" : total}
    </span>
  );
}

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { project } = useSession();
  return (
    <nav aria-label="Main" className="mt-5 flex flex-col gap-5 px-2">
      {NAV.map(({ section, items }) => (
        <div key={section}>
          <p className="qf-label mb-1 px-2.5">{section}</p>
          <ul className="flex flex-col gap-px">
            {items.map(({ href, label, icon: Icon, badge }) => {
              const active = isActive(pathname, href);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cx(
                      "relative flex h-8 items-center gap-2.5 rounded-[2px] px-2.5 text-[13px] transition-colors duration-150",
                      active ? "bg-surface font-medium text-ink shadow-[inset_0_0_0_1px_var(--qf-line)]" : "text-ink-2 hover:bg-sunken hover:text-ink",
                    )}
                  >
                    {active ? <span aria-hidden="true" className="absolute top-2 bottom-2 left-0 w-[2px] bg-copper" /> : null}
                    <Icon className={active ? "text-ink" : "text-muted"} />
                    {label}
                    {badge === "failures" && project ? <FailureCount projectId={project.id} /> : null}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function SidebarFooter() {
  const { me, logout } = useSession();
  const initials = me.user.display_name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div className="flex items-center gap-2.5 border-t border-line px-3 py-3">
      <span aria-hidden="true" className="grid size-7 shrink-0 place-items-center rounded-[3px] bg-sunken font-mono text-[11px] font-medium text-ink-2">
        {initials || "?"}
      </span>
      <div className="min-w-0 flex-1">
        <Link href="/account" className="block truncate text-[12.5px] font-medium hover:underline">
          {me.user.display_name}
        </Link>
        <p className="truncate font-mono text-[10.5px] text-muted">{me.user.email}</p>
      </div>
      <button
        onClick={() => void logout()}
        className="rounded-[2px] p-1.5 text-muted transition-colors hover:bg-sunken hover:text-ink"
        aria-label="Sign out"
        title="Sign out"
      >
        <IconLogout width={14} height={14} />
      </button>
    </div>
  );
}

function Breadcrumb() {
  const pathname = usePathname();
  const { project } = useSession();
  const [first, second] = pathname.split("/").filter(Boolean);
  const page = TITLES[first ?? ""] ?? first;
  return (
    <p className="flex min-w-0 items-center gap-1.5 font-mono text-[12px] text-muted">
      <span className="truncate">{project?.slug}</span>
      <span aria-hidden="true" className="text-line-strong">/</span>
      <span className={second ? "" : "text-ink"}>{page}</span>
      {second ? (
        <>
          <span aria-hidden="true" className="text-line-strong">/</span>
          <span className="truncate text-ink">{second.slice(0, 8)}</span>
        </>
      ) : null}
    </p>
  );
}

function Wordmark() {
  return (
    <span className="flex items-center gap-2">
      <Logo className="text-ink" />
      <span className="text-[15px] font-semibold tracking-[-0.02em]">queueforge</span>
    </span>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const [openedAt, setOpenedAt] = useState(pathname);
  if (openedAt !== pathname) {
    setOpenedAt(pathname);
    setOpen(false);
  }

  const sidebar = (onNavigate?: () => void) => (
    <div className="flex h-full flex-col">
      <div className="flex h-13 items-center px-4">
        <Wordmark />
      </div>
      <ProjectSwitcher />
      <Nav onNavigate={onNavigate} />
      <div className="flex-1" />
      <SidebarFooter />
    </div>
  );

  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[240px_1fr]">
      <aside className="sticky top-0 hidden h-dvh border-r border-line bg-paper lg:block">{sidebar()}</aside>

      {/* Mobile header + drawer */}
      <div className="sticky top-0 z-30 flex h-12 items-center justify-between border-b border-line bg-paper/95 px-4 backdrop-blur lg:hidden">
        <button
          onClick={() => setOpen(true)}
          className="-ml-1 rounded-[2px] p-1.5 text-ink-2 hover:bg-sunken"
          aria-label="Open navigation"
          aria-expanded={open}
        >
          <IconMenu />
        </button>
        <Wordmark />
        <LiveIndicator />
      </div>
      {open ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button className="absolute inset-0 bg-[rgba(13,14,16,0.45)]" aria-label="Close navigation" onClick={() => setOpen(false)} />
          <div className="qf-fade absolute inset-y-0 left-0 w-[272px] border-r border-line bg-paper">
            <button
              onClick={() => setOpen(false)}
              className="absolute top-3 right-3 rounded-[2px] p-1 text-muted hover:bg-sunken"
              aria-label="Close navigation"
            >
              <IconClose />
            </button>
            {sidebar(() => setOpen(false))}
          </div>
        </div>
      ) : null}

      <div className="min-w-0">
        <div className="sticky top-0 z-20 hidden h-13 items-center justify-between gap-4 border-b border-line bg-paper/90 px-6 backdrop-blur lg:flex">
          <Breadcrumb />
          <LiveIndicator />
        </div>
        <main id="main" className="mx-auto w-full max-w-[1320px] px-4 pt-6 pb-16 sm:px-6 lg:px-8">
          {children}
        </main>
      </div>
    </div>
  );
}

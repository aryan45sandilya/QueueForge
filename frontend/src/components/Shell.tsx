"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";

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

const NAV = [
  { href: "/", label: "Overview", icon: IconOverview },
  { href: "/jobs", label: "Jobs", icon: IconJobs },
  { href: "/failures", label: "Failed jobs", icon: IconFailed },
  { href: "/queues", label: "Queues & workers", icon: IconQueues },
  { href: "/schedules", label: "Schedules", icon: IconSchedules },
  { href: "/project", label: "Project & API keys", icon: IconKey },
  { href: "/settings", label: "Settings", icon: IconSettings },
  { href: "/docs", label: "Developer docs", icon: IconDocs },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

export function LiveIndicator() {
  const { status } = useLive();
  const label = status === "live" ? "Live" : status === "polling" ? "Polling every 10 s" : "Connecting";
  const title =
    status === "live"
      ? "Receiving job updates over Server-Sent Events"
      : status === "polling"
        ? "Live stream unavailable; refreshing every 10 seconds"
        : "Opening live update stream";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted" title={title} role="status">
      <span
        aria-hidden="true"
        className={cx(
          "size-1.5 rounded-full",
          status === "live" ? "bg-ok" : status === "polling" ? "bg-ochre" : "bg-line-strong",
        )}
      />
      {label}
    </span>
  );
}

function ProjectSwitcher() {
  const { projects, project, selectProject } = useSession();
  return (
    <label className="block px-3">
      <span className="sr-only">Project</span>
      <select
        value={project?.id ?? ""}
        onChange={(e) => selectProject(e.target.value)}
        className="h-8 w-full rounded-[3px] border border-line bg-paper px-2 text-[13px] font-medium focus:border-copper focus:outline-none"
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

function Nav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="mt-4 flex flex-col gap-px px-2">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cx(
              "relative flex h-8 items-center gap-2.5 rounded-[3px] px-2.5 text-[13px] transition-colors",
              active ? "bg-surface font-medium text-ink" : "text-ink-2 hover:bg-sunken hover:text-ink",
            )}
          >
            {active ? <span aria-hidden="true" className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded bg-copper" /> : null}
            <Icon className={active ? "text-copper-strong" : "text-muted"} />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

function SidebarFooter() {
  const { me, logout } = useSession();
  return (
    <div className="border-t border-line px-3 py-3">
      <p className="truncate text-xs font-medium">{me.user.display_name}</p>
      <p className="truncate text-[11px] text-muted">{me.user.email}</p>
      <button onClick={() => void logout()} className="mt-2 inline-flex items-center gap-1.5 text-xs text-muted hover:text-ink">
        <IconLogout width={13} height={13} /> Sign out
      </button>
    </div>
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
      <div className="flex h-12 items-center gap-2 px-4">
        <Logo className="text-ink" />
        <span className="font-serif text-[17px] font-medium tracking-tight">QueueForge</span>
      </div>
      <ProjectSwitcher />
      <Nav onNavigate={onNavigate} />
      <div className="flex-1" />
      <SidebarFooter />
    </div>
  );

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[232px_1fr]">
      <aside className="sticky top-0 hidden h-screen border-r border-line bg-paper lg:block">{sidebar()}</aside>

      {/* Mobile header + drawer */}
      <div className="sticky top-0 z-30 flex h-12 items-center justify-between border-b border-line bg-paper/95 px-4 backdrop-blur lg:hidden">
        <button
          onClick={() => setOpen(true)}
          className="-ml-1 rounded p-1.5 text-ink-2 hover:bg-sunken"
          aria-label="Open navigation"
          aria-expanded={open}
        >
          <IconMenu />
        </button>
        <span className="flex items-center gap-2">
          <Logo className="text-ink" />
          <span className="font-serif text-base font-medium">QueueForge</span>
        </span>
        <LiveIndicator />
      </div>
      {open ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Navigation">
          <button className="absolute inset-0 bg-[rgba(20,24,19,0.35)]" aria-label="Close navigation" onClick={() => setOpen(false)} />
          <div className="qf-fade absolute inset-y-0 left-0 w-[264px] border-r border-line bg-paper">
            <button
              onClick={() => setOpen(false)}
              className="absolute top-3 right-3 rounded p-1 text-muted hover:bg-sunken"
              aria-label="Close navigation"
            >
              <IconClose />
            </button>
            {sidebar(() => setOpen(false))}
          </div>
        </div>
      ) : null}

      <div className="min-w-0">
        <div className="hidden h-12 items-center justify-end border-b border-line px-6 lg:flex">
          <LiveIndicator />
        </div>
        <main className="mx-auto w-full max-w-[1280px] px-4 py-5 sm:px-6">{children}</main>
      </div>
    </div>
  );
}

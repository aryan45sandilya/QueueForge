"use client";

import Link from "next/link";
import useSWR from "swr";

import { Logo } from "@/components/icons";
import { StateBadge } from "@/components/ui";
import type { InstanceInfo, JobType, Me } from "@/lib/api";
import { formatNumber, formatSeconds } from "@/lib/format";

const REPO = "https://github.com/aryan45sandilya/QueueForge";

// A static, labelled illustration of one job's life. Not live data.
const TRACE: { at: string; state: string; note: string }[] = [
  { at: "09:00:00.004", state: "queued", note: "scheduled run of nightly-sync" },
  { at: "09:00:00.019", state: "running", note: "attempt 1 of 5 · worker w-2" },
  { at: "09:00:01.212", state: "retrying", note: "HTTP 503 from your endpoint · retry in 9.4 s" },
  { at: "09:00:10.633", state: "running", note: "attempt 2 of 5 · worker w-1" },
  { at: "09:00:10.851", state: "succeeded", note: "HTTP 200 in 218 ms" },
];

const CAPABILITIES: [string, string][] = [
  ["http.request", "Calls a URL you own. Signed, retried on 5xx and timeouts, never sent to private networks."],
  ["schedules", "Cron or fixed intervals. Overlapping runs are skipped; missed runs collapse into one."],
  ["retries", "Exponential backoff with jitter, capped per job. Exhausted jobs land in a dead letter, grouped by cause."],
  ["at-least-once", "A crashed worker's job is picked up elsewhere. Every request carries an Idempotency-Key."],
  ["visibility", "Each attempt, its timing and its error are kept. Updates stream to the dashboard as they happen."],
];

export default function WelcomePage() {
  const instance = useSWR<InstanceInfo>("/instance", { shouldRetryOnError: false });
  const types = useSWR<JobType[]>("/job-types", { shouldRetryOnError: false });
  const me = useSWR<Me>("/auth/me", { shouldRetryOnError: false, revalidateOnFocus: false });
  const limits = instance.data?.limits;
  const open = instance.data?.registration_open !== false;

  const spec: [string, string][] = limits
    ? [
        ["projects per account", formatNumber(limits.max_projects_per_user)],
        ["jobs per project per day", formatNumber(limits.max_jobs_per_day_per_project)],
        ["schedules per project", formatNumber(limits.max_schedules_per_project)],
        ["fastest schedule", `every ${formatSeconds(limits.min_schedule_interval_seconds)}`],
        ["longest job", formatSeconds(limits.max_job_timeout_seconds)],
        ["concurrent jobs per queue", limits.default_queue_concurrency ? String(limits.default_queue_concurrency) : "no fixed cap"],
        ["api requests per minute", formatNumber(limits.rate_limit_per_minute)],
      ]
    : [];

  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex h-16 max-w-[1180px] items-center justify-between px-4 sm:px-8">
        <span className="flex items-center gap-2">
          <Logo className="text-ink" />
          <span className="text-[15px] font-semibold tracking-[-0.02em]">queueforge</span>
        </span>
        <nav className="flex items-center gap-1 text-[13px]">
          <a href={REPO} className="hidden h-8 items-center px-3 text-ink-2 transition-colors hover:text-ink sm:inline-flex">
            Source
          </a>
          {me.data ? (
            <Link href="/" className="inline-flex h-8 items-center rounded-[2px] border border-btn-edge bg-btn px-3.5 font-medium text-btn-ink transition-colors hover:bg-btn-hover">
              Open dashboard
            </Link>
          ) : (
            <>
              <Link href="/login" className="inline-flex h-8 items-center px-3 text-ink-2 transition-colors hover:text-ink">
                Sign in
              </Link>
              {open ? (
                <Link href="/register" className="inline-flex h-8 items-center rounded-[2px] border border-btn-edge bg-btn px-3.5 font-medium text-btn-ink transition-colors hover:bg-btn-hover">
                  Create account
                </Link>
              ) : null}
            </>
          )}
        </nav>
      </header>

      <main id="main" className="mx-auto max-w-[1180px] px-4 sm:px-8">
        <section className="grid gap-12 pt-14 pb-20 lg:grid-cols-[1.05fr_1fr] lg:gap-16 lg:pt-24">
          <div>
            <p className="qf-label">background jobs · open source</p>
            <h1 className="mt-4 text-[40px] leading-[1.02] font-semibold tracking-[-0.035em] sm:text-[56px]">
              Hand off the work.
              <br />
              <span className="text-muted">Keep the receipts.</span>
            </h1>
            <p className="mt-6 max-w-[52ch] text-[15.5px] leading-relaxed text-ink-2">
              QueueForge runs jobs for your scripts and apps: it calls your endpoints on time, retries what fails, and keeps a
              record of every attempt so you can see exactly what happened and when.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-x-5 gap-y-3">
              {open ? (
                <Link href="/register" className="inline-flex h-10 items-center rounded-[2px] border border-btn-edge bg-btn px-5 text-[14px] font-medium text-btn-ink transition-[background-color,transform] hover:bg-btn-hover active:translate-y-px">
                  Create a free account
                </Link>
              ) : (
                <span className="font-mono text-[12px] text-muted">registration is closed on this instance</span>
              )}
              <a href={REPO} className="group inline-flex items-center gap-1.5 text-[14px] font-medium text-ink">
                Run your own copy
                <span aria-hidden="true" className="transition-transform group-hover:translate-x-0.5">→</span>
              </a>
            </div>
          </div>

          <figure className="self-start rounded-[3px] border border-line bg-surface">
            <figcaption className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <span className="font-mono text-[12px] text-ink">job 7c41e09a · http.request</span>
              <span className="qf-label">example</span>
            </figcaption>
            <ol className="divide-y divide-line">
              {TRACE.map((row) => (
                <li key={row.at} className="grid grid-cols-[92px_88px_1fr] items-baseline gap-3 px-4 py-2.5 text-[12.5px]">
                  <time className="font-mono text-[11px] text-muted">{row.at}</time>
                  <StateBadge state={row.state} />
                  <span className="text-ink-2">{row.note}</span>
                </li>
              ))}
            </ol>
            <div className="border-t border-line bg-paper/60 px-4 py-3">
              <p className="qf-label mb-1.5">headers your endpoint receives</p>
              <pre className="overflow-x-auto font-mono text-[11.5px] leading-relaxed text-ink-2">
                {`Idempotency-Key: 7c41e09a-…   (same on every retry)
X-QueueForge-Attempt: 2
X-QueueForge-Signature: t=1762505410,v1=9f2c…`}
              </pre>
            </div>
          </figure>
        </section>

        <section className="grid gap-12 border-t border-line py-14 lg:grid-cols-[1.05fr_1fr] lg:gap-16">
          <div>
            <h2 className="text-[22px] font-semibold tracking-[-0.02em]">What you get</h2>
            <dl className="mt-6 border-t border-line">
              {CAPABILITIES.map(([k, v]) => (
                <div key={k} className="grid gap-1 border-b border-line py-3.5 sm:grid-cols-[150px_1fr] sm:gap-6">
                  <dt className="font-mono text-[12.5px] text-ink">{k}</dt>
                  <dd className="text-[13.5px] leading-relaxed text-ink-2">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
          <div>
            <h2 className="text-[22px] font-semibold tracking-[-0.02em]">This instance</h2>
            {spec.length ? (
              <dl className="mt-6 border-t border-line">
                {spec.map(([k, v]) => (
                  <div key={k} className="flex items-baseline justify-between gap-6 border-b border-line py-2.5">
                    <dt className="text-[13.5px] text-ink-2">{k}</dt>
                    <dd className="font-mono text-[13px] num">{v}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="mt-6 font-mono text-[12px] text-muted">limits load from this instance&apos;s api</p>
            )}
            {instance.data?.webhooks_require_https ? (
              <p className="mt-3 text-[12.5px] text-muted">Webhook URLs must use https and resolve to a public address.</p>
            ) : null}
            {types.data?.length ? (
              <p className="mt-8 text-[13px] text-ink-2">
                Job types:{" "}
                {types.data
                  .filter((t) => !t.demo_only)
                  .map((t) => t.name)
                  .map((name, i, all) => (
                    <span key={name}>
                      <code className="font-mono text-[12.5px] text-ink">{name}</code>
                      {i < all.length - 1 ? ", " : ""}
                    </span>
                  ))}
              </p>
            ) : null}
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-[1180px] flex-wrap items-center justify-between gap-3 px-4 py-6 text-[12.5px] text-muted sm:px-8">
          <span className="font-mono text-[11.5px]">queueforge · self-hostable · at-least-once</span>
          <span className="flex gap-5">
            <a href={REPO} className="transition-colors hover:text-ink">Source</a>
            <Link href="/login" className="transition-colors hover:text-ink">Sign in</Link>
            {open ? <Link href="/register" className="transition-colors hover:text-ink">Create account</Link> : null}
          </span>
        </div>
      </footer>
    </div>
  );
}

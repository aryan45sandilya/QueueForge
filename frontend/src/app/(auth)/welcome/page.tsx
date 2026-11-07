"use client";

import Link from "next/link";
import { useSyncExternalStore } from "react";
import useSWR from "swr";

import { Logo } from "@/components/icons";
import { CodeBlock, Mono } from "@/components/ui";
import type { InstanceInfo, JobType, Me } from "@/lib/api";
import { formatNumber, formatSeconds } from "@/lib/format";

const STEPS: [string, string][] = [
  ["Create an account and a project", "A project holds your jobs, queues, schedules and API keys. Nobody else can see them."],
  ["Point it at your code", "Most people use the http.request job: QueueForge calls a URL you own (a serverless function, a small API, a webhook endpoint) with the data you give it."],
  ["Run it now, later or on a schedule", "Submit jobs from the dashboard or the API, delay them, or repeat them with cron. Failed calls are retried with backoff and every attempt is recorded."],
];

export default function WelcomePage() {
  const instance = useSWR<InstanceInfo>("/instance", { shouldRetryOnError: false });
  const types = useSWR<JobType[]>("/job-types", { shouldRetryOnError: false });
  const me = useSWR<Me>("/auth/me", { shouldRetryOnError: false, revalidateOnFocus: false });
  const origin = useSyncExternalStore(() => () => {}, () => window.location.origin, () => "https://your-queueforge");
  const limits = instance.data?.limits;
  const open = instance.data?.registration_open !== false;

  const example = `curl -X POST "${origin}/api/v1/projects/$PROJECT_ID/jobs" \\
  -H "Authorization: Bearer $QF_API_KEY" -H "Content-Type: application/json" \\
  -d '{"type": "http.request", "delay_seconds": 3600,
       "payload": {"url": "https://example.com/hooks/send-digest",
                   "body": {"user": 42}}}'`;

  return (
    <div className="min-h-screen">
      <header className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4 sm:px-6">
        <span className="flex items-center gap-2">
          <Logo className="text-ink" />
          <span className="font-serif text-lg font-medium">QueueForge</span>
        </span>
        <nav className="flex items-center gap-2 text-[13px]">
          {me.data ? (
            <Link href="/" className="inline-flex h-8 items-center rounded-[3px] bg-copper-strong px-3 font-medium text-white">
              Open dashboard
            </Link>
          ) : (
            <>
              <Link href="/login" className="inline-flex h-8 items-center rounded-[3px] px-3 text-ink-2 hover:bg-sunken">
                Sign in
              </Link>
              {open ? (
                <Link href="/register" className="inline-flex h-8 items-center rounded-[3px] bg-copper-strong px-3 font-medium text-white">
                  Create account
                </Link>
              ) : null}
            </>
          )}
        </nav>
      </header>

      <main className="mx-auto max-w-5xl px-4 pb-16 sm:px-6">
        <section className="border-b border-line py-10 sm:py-14">
          <h1 className="max-w-2xl font-serif text-3xl leading-tight font-medium tracking-tight sm:text-[40px]">
            Run your background work reliably, without running a queue.
          </h1>
          <p className="mt-4 max-w-2xl text-[15px] leading-relaxed text-ink-2">
            QueueForge holds your jobs, runs them on time, retries what fails and shows you exactly what happened. Use it to call
            your own endpoints on a schedule, send notification batches, transform CSV data or produce reports.
          </p>
          <div className="mt-6 flex flex-wrap gap-2 text-[13px]">
            {open ? (
              <Link href="/register" className="inline-flex h-9 items-center rounded-[3px] bg-copper-strong px-4 font-medium text-white">
                Create a free account
              </Link>
            ) : (
              <span className="inline-flex h-9 items-center rounded-[3px] border border-line-strong px-4 text-muted">
                Registration is closed on this instance
              </span>
            )}
            <a href="https://github.com/aryan45sandilya/QueueForge" className="inline-flex h-9 items-center rounded-[3px] border border-line-strong px-4 hover:bg-sunken">
              Source code
            </a>
          </div>
        </section>

        <section className="grid gap-10 border-b border-line py-10 lg:grid-cols-[1fr_1.1fr]">
          <div>
            <h2 className="text-[13px] font-semibold tracking-wide text-muted uppercase">How it works</h2>
            <ol className="mt-4 flex flex-col gap-5">
              {STEPS.map(([title, text], i) => (
                <li key={title} className="grid grid-cols-[28px_1fr] gap-2">
                  <span className="font-serif text-xl leading-none text-copper-strong num">{i + 1}</span>
                  <span>
                    <span className="block text-[14px] font-medium">{title}</span>
                    <span className="mt-0.5 block text-[13px] leading-relaxed text-ink-2">{text}</span>
                  </span>
                </li>
              ))}
            </ol>
          </div>
          <div className="min-w-0">
            <h2 className="text-[13px] font-semibold tracking-wide text-muted uppercase">Call your endpoint in an hour</h2>
            <CodeBlock className="mt-4">{example}</CodeBlock>
            <p className="mt-2 text-xs text-muted">
              Each call carries an <Mono>Idempotency-Key</Mono> and an HMAC signature so your endpoint can verify it and ignore repeats.
            </p>
          </div>
        </section>

        <section className="grid gap-10 py-10 lg:grid-cols-2">
          <div>
            <h2 className="text-[13px] font-semibold tracking-wide text-muted uppercase">Job types</h2>
            <ul className="mt-4 divide-y divide-line border-y border-line">
              {(types.data ?? []).filter((t) => !t.demo_only).map((t) => (
                <li key={t.name} className="py-2.5">
                  <Mono className="text-[13px] font-medium">{t.name}</Mono>
                  <p className="text-[13px] text-ink-2">{t.description}</p>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h2 className="text-[13px] font-semibold tracking-wide text-muted uppercase">Limits on this instance</h2>
            {limits ? (
              <dl className="mt-4 grid grid-cols-[1fr_auto] gap-x-6 border-y border-line text-[13px]">
                {(
                  [
                    ["Projects per account", formatNumber(limits.max_projects_per_user)],
                    ["Jobs per project per day", formatNumber(limits.max_jobs_per_day_per_project)],
                    ["Schedules per project", formatNumber(limits.max_schedules_per_project)],
                    ["Most frequent schedule", `every ${formatSeconds(limits.min_schedule_interval_seconds)}`],
                    ["Longest job", formatSeconds(limits.max_job_timeout_seconds)],
                    ["Jobs running at once per queue", limits.default_queue_concurrency ? String(limits.default_queue_concurrency) : "no fixed cap"],
                    ["API requests per minute", formatNumber(limits.rate_limit_per_minute)],
                  ] as [string, string][]
                ).map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="border-b border-line py-2 text-ink-2 last-of-type:border-0">{k}</dt>
                    <dd className="border-b border-line py-2 text-right num">{v}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="mt-4 text-xs text-muted">Limits load from this instance&apos;s API.</p>
            )}
            {instance.data?.webhooks_require_https ? (
              <p className="mt-2 text-xs text-muted">Webhook URLs must use https and point at a public address.</p>
            ) : null}
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-2 px-4 py-5 text-xs text-muted sm:px-6">
          <span>QueueForge is open source. You can also run your own copy.</span>
          <span className="flex gap-4">
            <Link href="/login" className="hover:text-ink">Sign in</Link>
            {open ? <Link href="/register" className="hover:text-ink">Create account</Link> : null}
          </span>
        </div>
      </footer>
    </div>
  );
}

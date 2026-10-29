"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import { Stat, TimeSeriesChart } from "@/components/charts";
import { JobRowLink } from "@/components/jobs";
import {
  ErrorState,
  InfoHint,
  Loading,
  PageHeader,
  Panel,
  StateBadge,
  Td,
  Th,
  TextLink,
  Time,
  cx,
} from "@/components/ui";
import type { FailureGroup, JobPage, Metrics } from "@/lib/api";
import { formatCompact, formatDuration, formatNumber, formatPercent, formatSeconds } from "@/lib/format";
import { useProject } from "@/lib/session";

const WINDOWS = [
  { key: "1h", label: "1 hour" },
  { key: "24h", label: "24 hours" },
  { key: "7d", label: "7 days" },
] as const;

export default function OverviewPage() {
  const project = useProject();
  const [windowKey, setWindowKey] = useState<(typeof WINDOWS)[number]["key"]>("24h");
  const metrics = useSWR<Metrics>(`/projects/${project.id}/metrics?window=${windowKey}`);
  const recent = useSWR<JobPage>(`/projects/${project.id}/jobs?sort=updated_at&order=desc&limit=8`);
  const failures = useSWR<FailureGroup[]>(`/projects/${project.id}/failures`);
  const m = metrics.data;
  const def = (k: string) => m?.definitions[k] ?? "";

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Operational summary for ${project.name}. Figures are computed from persisted job and attempt records.`}
        actions={
          <div role="radiogroup" aria-label="Time window" className="inline-flex rounded-[3px] border border-line-strong bg-surface p-0.5">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                role="radio"
                aria-checked={windowKey === w.key}
                onClick={() => setWindowKey(w.key)}
                className={cx(
                  "h-7 rounded-[2px] px-3 text-xs",
                  windowKey === w.key ? "bg-ink text-paper" : "text-ink-2 hover:bg-sunken",
                )}
              >
                {w.label}
              </button>
            ))}
          </div>
        }
      />

      {metrics.error ? <ErrorState error={metrics.error} onRetry={() => metrics.mutate()} /> : null}
      {!m && !metrics.error ? <Loading label="Computing metrics" /> : null}

      {m ? (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 divide-line rounded-[4px] border border-line bg-surface sm:grid-cols-3 sm:divide-x xl:grid-cols-6">
            <Stat label="Submitted" value={formatCompact(m.submitted)} sub={`${formatNumber(m.submitted_per_minute, 2)} / min arrival`} hint={def("submitted")} />
            <Stat
              label="Completed"
              value={formatCompact(m.succeeded + m.failed)}
              sub={`${formatNumber(m.completed_per_minute, 2)} / min throughput`}
              hint={def("completed_per_minute")}
            />
            <Stat
              label="Failure rate"
              value={formatPercent(m.failure_rate)}
              sub={`${formatNumber(m.failed)} dead-lettered · ${formatNumber(m.retries)} retries`}
              hint={def("failure_rate")}
              tone={m.failure_rate !== null && m.failure_rate !== undefined && m.failure_rate > 0.05 ? "bad" : undefined}
            />
            <Stat
              label="Backlog now"
              value={formatCompact(m.active.queued ?? 0)}
              sub={`${m.active.running ?? 0} running · ${(m.active.retrying ?? 0) + (m.active.scheduled ?? 0)} waiting`}
              hint={def("queue_depth")}
            />
            <Stat label="Wait p95" value={formatDuration(m.wait_time.p95_ms)} sub={`p50 ${formatDuration(m.wait_time.p50_ms)} · ${m.wait_time.samples} attempts`} hint={def("wait_time")} />
            <Stat
              label="Run time p95"
              value={formatDuration(m.execution_time.p95_ms)}
              sub={`p50 ${formatDuration(m.execution_time.p50_ms)} · ${m.execution_time.samples} attempts`}
              hint={def("execution_time")}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-[3fr_2fr]">
            <Panel
              title={
                <>
                  Throughput <InfoHint text="Columns: jobs that finished in each bucket, by outcome. Line: jobs submitted in each bucket." />
                </>
              }
              description={`Jobs per ${Math.round(m.bucket_seconds / 60)}-minute bucket`}
            >
              <TimeSeriesChart
                ariaLabel="Completed jobs by outcome and submitted jobs per bucket"
                points={m.series.map((p) => ({ t: p.t, values: { succeeded: p.succeeded, failed: p.failed, submitted: p.submitted } }))}
                series={[
                  { key: "succeeded", label: "Succeeded", color: "var(--qf-series-a)", kind: "bar" },
                  { key: "failed", label: "Failed (dead letter)", color: "var(--qf-series-b)", kind: "bar" },
                  { key: "submitted", label: "Submitted", color: "var(--qf-series-ink)", kind: "line" },
                ]}
                bucketSeconds={m.bucket_seconds}
                unit="jobs"
                formatValue={(v) => formatCompact(v)}
                emptyMessage="No jobs were submitted or finished in this window."
              />
            </Panel>
            <Panel
              title={
                <>
                  Latency p95 <InfoHint text="Wait: time from becoming runnable to starting. Run time: start to finish of an attempt." />
                </>
              }
              description="Per bucket, milliseconds"
            >
              <TimeSeriesChart
                ariaLabel="95th percentile wait time and run time per bucket"
                points={m.series.map((p) => ({ t: p.t, values: { wait: p.wait_p95_ms ?? null, exec: p.exec_p95_ms ?? null } }))}
                series={[
                  { key: "exec", label: "Run time p95", color: "var(--qf-series-a)", kind: "line" },
                  { key: "wait", label: "Queue wait p95", color: "var(--qf-series-b)", kind: "line" },
                ]}
                bucketSeconds={m.bucket_seconds}
                unit="ms"
                formatValue={(v) => formatDuration(v)}
                emptyMessage="No attempts ran in this window."
              />
            </Panel>
          </div>

          <div className="grid gap-4 xl:grid-cols-[3fr_2fr]">
            <Panel title="Queue health" bodyClassName="overflow-x-auto" actions={<TextLink href="/queues" className="text-xs">Manage queues</TextLink>}>
              <table className="w-full min-w-[520px] text-[13px]">
                <thead>
                  <tr>
                    <Th>Queue</Th>
                    <Th className="text-right">Queued</Th>
                    <Th className="text-right">Running</Th>
                    <Th className="text-right">Retrying</Th>
                    <Th className="text-right">Scheduled</Th>
                    <Th className="text-right">Oldest wait</Th>
                  </tr>
                </thead>
                <tbody>
                  {m.queues.map((q) => (
                    <tr key={q.queue} className="border-b border-line last:border-0">
                      <Td>
                        <span className="font-mono text-xs">{q.queue}</span>
                        {q.paused ? <StateBadge state="paused" className="ml-2" /> : null}
                      </Td>
                      <Td className="text-right num">{q.queued}</Td>
                      <Td className="text-right num">
                        {q.running}
                        {q.concurrency_limit ? <span className="text-muted"> / {q.concurrency_limit}</span> : null}
                      </Td>
                      <Td className="text-right num">{q.retrying}</Td>
                      <Td className="text-right num">{q.scheduled}</Td>
                      <Td className={cx("text-right num", (q.oldest_queued_age_seconds ?? 0) > 300 ? "text-warn" : "")}>
                        {formatSeconds(q.oldest_queued_age_seconds)}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="border-t border-line px-3 py-2 text-xs text-muted">
                Workers: <span className="num">{m.workers.healthy}</span> healthy
                {m.workers.stale ? <>, <span className="text-warn num">{m.workers.stale}</span> stale</> : null}
                {m.workers.lost ? <>, <span className="text-bad num">{m.workers.lost}</span> lost</> : null} ·{" "}
                <span className="num">{m.workers.slots}</span> execution slots
                {m.workers.healthy === 0 ? <span className="text-warn"> · no worker is running, so queued jobs will not start</span> : null}
              </p>
            </Panel>

            <Panel title="Failure groups" actions={<TextLink href="/failures" className="text-xs">Open dead letter</TextLink>} bodyClassName="">
              {failures.data && failures.data.length === 0 ? (
                <p className="px-4 py-6 text-xs text-muted">No failed jobs. Jobs land here after their last attempt fails.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {(failures.data ?? []).slice(0, 4).map((g) => (
                    <li key={g.fingerprint} className="px-4 py-2.5">
                      <Link href={`/jobs?state=failed&fingerprint=${g.fingerprint}`} className="group block">
                        <p className="flex items-baseline justify-between gap-3 text-[13px]">
                          <span className="truncate font-mono text-xs group-hover:underline">{g.error_type}</span>
                          <span className="shrink-0 num text-xs text-muted">
                            {g.count} job{g.count === 1 ? "" : "s"}
                          </span>
                        </p>
                        <p className="truncate text-xs text-muted">{g.sample_message}</p>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>

          <Panel title="Recent activity" description="Most recently updated jobs; refreshes on live events" bodyClassName="overflow-x-auto" actions={<TextLink href="/jobs" className="text-xs">All jobs</TextLink>}>
            {recent.error ? <ErrorState error={recent.error} /> : null}
            {recent.data && recent.data.items.length === 0 ? (
              <p className="px-4 py-6 text-xs text-muted">
                No jobs yet. Submit one from the <TextLink href="/jobs">Jobs</TextLink> page or with the API (see <TextLink href="/docs">docs</TextLink>).
              </p>
            ) : (
              <table className="w-full min-w-[560px] text-[13px]">
                <tbody>
                  {(recent.data?.items ?? []).map((j) => (
                    <tr key={j.id} className="border-b border-line last:border-0">
                      <Td className="w-24">
                        <JobRowLink id={j.id} />
                      </Td>
                      <Td>{j.type}</Td>
                      <Td>
                        <StateBadge state={j.state} />
                      </Td>
                      <Td className="text-muted">{j.queue}</Td>
                      <Td className="text-right text-muted">
                        <Time iso={j.updated_at} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      ) : null}
    </>
  );
}

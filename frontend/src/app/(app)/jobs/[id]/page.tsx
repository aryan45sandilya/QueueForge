"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import useSWR from "swr";

import { IconArrowLeft, IconRetry, IconStop } from "@/components/icons";
import {
  Button,
  CodeBlock,
  ConfirmDialog,
  ErrorState,
  KeyValue,
  Loading,
  Mono,
  Panel,
  StateBadge,
  Td,
  Th,
  Time,
  TextLink,
  cx,
  errorMessage,
  useToast,
} from "@/components/ui";
import { ApiError, api, type JobDetail } from "@/lib/api";
import { absoluteTime, durationBetween, formatDuration, pretty } from "@/lib/format";
import { useLive } from "@/lib/live";
import { useProject } from "@/lib/session";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

function guidance(job: JobDetail): string | null {
  const err = job.last_error as { type?: string; retryable?: boolean } | null;
  if (!err || job.state !== "failed") return null;
  if (err.type === "WorkerLost") return "The worker stopped heartbeating while running this job (crash, OOM kill or network partition). Check worker logs and memory, then retry.";
  if (err.type === "Timeout") return "The handler exceeded its timeout. Raise timeout_seconds for this kind of work or split it into smaller jobs before retrying.";
  if (err.type === "PayloadValidationError" || err.type === "UnknownJobType") return "The payload or job type doesn't match what workers accept. Submit a corrected job; retrying this one will fail the same way.";
  if (err.retryable === false) return "The handler reported a permanent error, so retries were skipped. Fix the input or the downstream resource before retrying.";
  return "All attempts failed with a retryable error. If the dependency has recovered, retry the job; the attempt history is kept.";
}

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>();
  const project = useProject();
  const toast = useToast();
  const key = `/projects/${project.id}/jobs/${id}`;
  const { data: job, error, mutate } = useSWR<JobDetail>(key);
  const { subscribe } = useLive();
  const [action, setAction] = useState<"cancel" | "retry" | null>(null);
  const [busy, setBusy] = useState(false);
  const [showTrace, setShowTrace] = useState(false);

  useEffect(() => subscribe((e) => e.job.id === id && void mutate()), [subscribe, id, mutate]);

  if (error) {
    if (error instanceof ApiError && error.status === 404) {
      return (
        <div className="py-10 text-center">
          <p className="font-medium">Job not found</p>
          <p className="mt-1 text-xs text-muted">It may belong to another project or have been removed by retention.</p>
          <TextLink href="/jobs" className="mt-3 inline-block text-sm">
            Back to jobs
          </TextLink>
        </div>
      );
    }
    return <ErrorState error={error} onRetry={() => mutate()} />;
  }
  if (!job) return <Loading label="Loading job" />;

  const err = job.last_error as { type?: string; message?: string; fingerprint?: string; traceback?: string; retryable?: boolean } | null;
  const result = job.result as Record<string, unknown> | null;
  const reportDoc = typeof result?.document === "string" ? (result.document as string) : null;
  const canCancel = !TERMINAL.has(job.state) && !(job.state === "running" && job.cancel_requested);
  const canRetry = job.state === "failed" || job.state === "cancelled";
  const advice = guidance(job);

  const perform = async () => {
    if (!action) return;
    setBusy(true);
    try {
      const updated = await api.post<JobDetail>(`${key}/${action}`);
      await mutate(updated, { revalidate: false });
      toast(
        "success",
        action === "retry"
          ? "Job re-queued with a fresh attempt budget"
          : updated.state === "running"
            ? "Cancellation requested; the worker will stop the handler"
            : "Job cancelled",
      );
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(false);
      setAction(null);
    }
  };

  return (
    <>
      <div className="mb-3">
        <Link href="/jobs" className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink">
          <IconArrowLeft width={13} height={13} /> Jobs
        </Link>
      </div>
      <header className="mb-5 flex flex-col gap-3 border-b border-line pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-[21px] leading-tight font-medium tracking-[-0.02em]">{job.type}</h1>
            <StateBadge state={job.state} />
            {job.cancel_requested && job.state === "running" ? <span className="text-xs text-warn">cancellation requested</span> : null}
          </div>
          <p className="mt-1 flex flex-wrap items-center gap-x-3 font-mono text-xs text-muted">
            <span className="select-all">{job.id}</span>
          </p>
        </div>
        <div className="flex gap-2">
          <Button icon={<IconStop />} disabled={!canCancel} onClick={() => setAction("cancel")} variant="danger">
            Cancel
          </Button>
          <Button icon={<IconRetry />} disabled={!canRetry} onClick={() => setAction("retry")} variant="primary">
            Retry
          </Button>
        </div>
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(320px,2fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          {err ? (
            <Panel
              title={job.state === "failed" ? "Failure" : job.state === "retrying" ? "Last error (will retry)" : "Last error"}
              className={job.state === "failed" ? "border-bad/40" : undefined}
            >
              <p className="font-mono text-[13px] font-medium text-bad">{err.type}</p>
              <p className="mt-1 text-[13px] break-words whitespace-pre-wrap">{err.message}</p>
              <p className="mt-2 text-xs text-muted">
                {err.retryable === false ? "Permanent error" : "Retryable error"}
                {err.fingerprint ? (
                  <>
                    {" "}
                    · group{" "}
                    <TextLink href={`/jobs?state=failed&fingerprint=${err.fingerprint}`} className="font-mono">
                      {err.fingerprint}
                    </TextLink>
                  </>
                ) : null}
              </p>
              {advice ? <p className="mt-3 border-l-2 border-ochre bg-ochre-tint/50 px-3 py-2 text-xs text-ink-2">{advice}</p> : null}
              {err.traceback ? (
                <div className="mt-3">
                  <button className="text-xs text-muted underline-offset-2 hover:underline" onClick={() => setShowTrace((v) => !v)} aria-expanded={showTrace}>
                    {showTrace ? "Hide traceback" : "Show traceback"}
                  </button>
                  {showTrace ? <CodeBlock className="mt-2">{err.traceback}</CodeBlock> : null}
                </div>
              ) : null}
            </Panel>
          ) : null}

          <Panel title="Attempts" description={`${job.attempts} of ${job.max_attempts} allowed`} bodyClassName="overflow-x-auto">
            {job.attempt_history.length === 0 ? (
              <p className="px-4 py-5 text-xs text-muted">
                {job.state === "scheduled" ? `Not started; scheduled for ${absoluteTime(job.run_at)}.` : "No attempt has started yet."}
              </p>
            ) : (
              <table className="w-full min-w-[620px] text-[13px]">
                <thead>
                  <tr>
                    <Th>#</Th>
                    <Th>Outcome</Th>
                    <Th>Worker</Th>
                    <Th className="text-right">Waited</Th>
                    <Th className="text-right">Ran</Th>
                    <Th className="text-right">Started</Th>
                    <Th>Error</Th>
                  </tr>
                </thead>
                <tbody>
                  {job.attempt_history.map((a) => {
                    const e = a.error as { type?: string; message?: string } | null;
                    return (
                      <tr key={a.attempt_number} className="border-b border-line last:border-0">
                        <Td className="num">{a.attempt_number}</Td>
                        <Td>
                          <StateBadge state={a.outcome} />
                        </Td>
                        <Td>
                          <Mono className="text-ink-2">{a.worker_id ?? "—"}</Mono>
                        </Td>
                        <Td className="text-right num">{formatDuration(a.wait_ms)}</Td>
                        <Td className="text-right num">{a.outcome === "running" ? "running" : formatDuration(a.duration_ms)}</Td>
                        <Td className="text-right text-ink-2">
                          <Time iso={a.started_at} />
                        </Td>
                        <Td className="max-w-[260px] truncate font-mono text-xs text-muted" title={e?.message}>
                          {e?.type ?? ""}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Panel>

          {result ? (
            <Panel title="Result" actions={result.truncated ? <span className="text-xs text-warn">truncated by result size limit</span> : null}>
              {reportDoc ? <CodeBlock className="mb-3">{reportDoc}</CodeBlock> : null}
              <CodeBlock>{pretty(reportDoc ? { ...result, document: "(shown above)" } : result)}</CodeBlock>
            </Panel>
          ) : null}

          <Panel title="Payload">
            <CodeBlock>{pretty(job.payload)}</CodeBlock>
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <Panel title="Details">
            <KeyValue
              items={[
                ["Queue", <Mono key="q">{job.queue}</Mono>],
                ["Priority", <span key="p" className="num">{job.priority}</span>],
                ["Attempts", <span key="a" className="num">{job.attempts} / {job.max_attempts}{job.manual_retries ? ` · ${job.manual_retries} manual retr${job.manual_retries === 1 ? "y" : "ies"}` : ""}</span>],
                ["Timeout", `${job.timeout_seconds} s per attempt`],
                ["Backoff", `${job.backoff_base_seconds} s base, ${job.backoff_max_seconds} s cap, jittered`],
                ["Idempotency key", job.idempotency_key ? <Mono key="i">{job.idempotency_key}</Mono> : "—"],
                ["Submitted by", job.created_by ?? "—"],
                ["Schedule", job.schedule_id ? <TextLink key="s" href="/schedules">{job.schedule_id.slice(0, 8)}</TextLink> : "—"],
                ["Worker", job.worker_id ? <Mono key="w">{job.worker_id}</Mono> : "—"],
                ["Lease expires", job.lease_expires_at ? <Time key="l" iso={job.lease_expires_at} /> : "—"],
              ]}
            />
          </Panel>
          <Panel title="Timestamps">
            <KeyValue
              items={[
                ["Created", absoluteTime(job.created_at)],
                ["Runnable at", absoluteTime(job.run_at)],
                ["First started", absoluteTime(job.started_at)],
                ["Finished", absoluteTime(job.finished_at)],
                ["Total time", formatDuration(durationBetween(job.created_at, job.finished_at))],
              ]}
            />
          </Panel>
          <Panel title="State timeline" bodyClassName="px-4 py-3">
            <ol className="relative">
              {job.events.map((e, i) => (
                <li key={e.id} className="relative pb-3 pl-5 last:pb-0">
                  {i < job.events.length - 1 ? <span aria-hidden="true" className="absolute top-3 bottom-0 left-[4px] w-px bg-line-strong" /> : null}
                  <span
                    aria-hidden="true"
                    className={cx(
                      "absolute top-[5px] left-0 size-[9px] rounded-full border-2 border-surface",
                      e.to_state === "failed" ? "bg-bad" : e.to_state === "succeeded" ? "bg-ok" : e.to_state === "retrying" ? "bg-ochre" : "bg-sage",
                    )}
                  />
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-mono text-xs">
                      {e.from_state ? `${e.from_state} → ` : ""}
                      {e.to_state}
                    </span>
                    <span className="text-[11px] text-muted">
                      <Time iso={e.at} mode="absolute" />
                    </span>
                  </div>
                  {e.message ? <p className="text-xs text-ink-2">{e.message}</p> : null}
                  <p className="font-mono text-[11px] text-muted">{e.actor}</p>
                </li>
              ))}
            </ol>
          </Panel>
        </div>
      </div>

      <ConfirmDialog
        open={action !== null}
        title={action === "cancel" ? "Cancel this job?" : "Retry this job?"}
        confirmLabel={action === "cancel" ? "Cancel job" : "Retry job"}
        danger={action === "cancel"}
        busy={busy}
        onClose={() => setAction(null)}
        onConfirm={perform}
        body={
          action === "cancel" ? (
            job.state === "running" ? (
              <p>The job is running. Its worker will stop the handler at the next heartbeat (within a few seconds). Side effects already performed are not undone.</p>
            ) : (
              <p>The job will not run. This cannot be undone, but you can retry a cancelled job later.</p>
            )
          ) : (
            <p>
              The job goes back to <Mono>{job.queue}</Mono> with {job.attempt_budget} more attempt(s). Handlers run at least once, so make sure
              repeating this work is safe.
            </p>
          )
        }
      />
    </>
  );
}

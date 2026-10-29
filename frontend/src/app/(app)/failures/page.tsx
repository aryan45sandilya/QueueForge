"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import { IconRetry } from "@/components/icons";
import { JobRowLink } from "@/components/jobs";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Td,
  Th,
  Time,
  errorMessage,
  useToast,
} from "@/components/ui";
import { api, type FailureGroup, type JobPage } from "@/lib/api";
import { useProject } from "@/lib/session";

function advice(group: FailureGroup): string {
  if (group.error_type === "WorkerLost")
    return "Workers stopped heartbeating mid-job: look for crashes, OOM kills or restarts in worker logs. Retrying is safe if the handler is idempotent.";
  if (group.error_type === "Timeout")
    return "Attempts ran past their timeout. Raise timeout_seconds for this workload or split it into smaller jobs, then retry.";
  if (group.error_type === "PayloadValidationError" || group.error_type === "UnknownJobType")
    return "Payloads don't match the handler deployed on workers. Fix the submitting client or deploy the matching worker version; retrying unchanged jobs will fail again.";
  if (!group.retryable)
    return "The handler classified this as permanent. Fix the input data or the missing resource first; a retry of the same payload will fail the same way.";
  return "Retryable failures that exhausted their attempts. Once the downstream dependency is healthy, retry the group.";
}

export default function FailuresPage() {
  const project = useProject();
  const toast = useToast();
  const groups = useSWR<FailureGroup[]>(`/projects/${project.id}/failures`);
  const recent = useSWR<JobPage>(`/projects/${project.id}/jobs?state=failed&sort=finished_at&order=desc&limit=10`);
  const [confirm, setConfirm] = useState<FailureGroup | null>(null);
  const [busy, setBusy] = useState(false);

  const retryGroup = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      const res = await api.post<{ retried: number }>(`/projects/${project.id}/failures/${confirm.fingerprint}/retry`);
      toast("success", `${res.retried} job(s) re-queued`);
      await Promise.all([groups.mutate(), recent.mutate()]);
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const total = (groups.data ?? []).reduce((n, g) => n + g.count, 0);

  return (
    <>
      <PageHeader
        title="Failed jobs"
        description="The dead letter: jobs whose final attempt failed, grouped by error fingerprint (error type plus message with IDs and numbers normalized)."
      />
      {groups.error ? <ErrorState error={groups.error} onRetry={() => groups.mutate()} /> : null}
      {!groups.data && !groups.error ? <Loading /> : null}
      {groups.data && groups.data.length === 0 ? (
        <Panel>
          <EmptyState title="Nothing in the dead letter">
            Jobs arrive here after exhausting their retries or hitting a permanent error. Retryable errors are retried automatically
            with exponential backoff first.
          </EmptyState>
        </Panel>
      ) : null}

      {groups.data && groups.data.length > 0 ? (
        <div className="flex flex-col gap-3">
          <p className="text-xs text-muted">
            <span className="num">{total}</span> failed job(s) in <span className="num">{groups.data.length}</span> group(s).
          </p>
          {groups.data.map((g) => (
            <section key={g.fingerprint} className="rounded-[4px] border border-line bg-surface">
              <div className="flex flex-col gap-3 px-4 py-3 md:flex-row md:items-start md:justify-between">
                <div className="min-w-0">
                  <p className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-mono text-[13px] font-medium text-bad">{g.error_type}</span>
                    <span className="num text-xs text-muted">
                      {g.count} job{g.count === 1 ? "" : "s"} · {g.retryable ? "retryable" : "permanent"}
                    </span>
                  </p>
                  <p className="mt-1 text-[13px] break-words">{g.sample_message}</p>
                  <p className="mt-1.5 text-xs text-muted">
                    {g.job_types.join(", ")} on {g.queues.join(", ")} · first <Time iso={g.first_failed_at} /> · last{" "}
                    <Time iso={g.last_failed_at} /> · fingerprint <span className="font-mono">{g.fingerprint}</span>
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Link
                    href={`/jobs?state=failed&fingerprint=${g.fingerprint}`}
                    className="inline-flex h-8 items-center rounded-[3px] border border-line-strong px-3 text-[13px] hover:bg-sunken"
                  >
                    View jobs
                  </Link>
                  <Button icon={<IconRetry />} onClick={() => setConfirm(g)}>
                    Retry all
                  </Button>
                </div>
              </div>
              <p className="border-t border-line bg-paper/60 px-4 py-2 text-xs text-ink-2">
                <span className="font-medium">Recovery: </span>
                {advice(g)}
              </p>
            </section>
          ))}
        </div>
      ) : null}

      {recent.data && recent.data.items.length > 0 ? (
        <Panel title="Most recent failures" className="mt-5" bodyClassName="overflow-x-auto">
          <table className="w-full min-w-[600px] text-[13px]">
            <thead>
              <tr>
                <Th>Job</Th>
                <Th>Type</Th>
                <Th>Error</Th>
                <Th className="text-right">Attempts</Th>
                <Th className="text-right">Failed</Th>
              </tr>
            </thead>
            <tbody>
              {recent.data.items.map((j) => (
                <tr key={j.id} className="border-b border-line last:border-0">
                  <Td>
                    <JobRowLink id={j.id} />
                  </Td>
                  <Td>{j.type}</Td>
                  <Td className="max-w-[340px] truncate" title={j.error_message ?? ""}>
                    <span className="font-mono text-xs">{j.error_type}</span>{" "}
                    <span className="text-xs text-muted">{j.error_message}</span>
                  </Td>
                  <Td className="text-right num">{j.attempts}</Td>
                  <Td className="text-right text-ink-2">
                    <Time iso={j.finished_at} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      ) : null}

      <ConfirmDialog
        open={confirm !== null}
        title="Retry failure group"
        confirmLabel={`Retry ${confirm?.count ?? 0} jobs`}
        busy={busy}
        onClose={() => setConfirm(null)}
        onConfirm={retryGroup}
        body={
          <p>
            All {confirm?.count} failed jobs with error <span className="font-mono">{confirm?.error_type}</span> go back to their queues
            with a fresh attempt budget (up to 1,000 per action).{" "}
            {confirm && !confirm.retryable ? "These errors were marked permanent, so make sure the cause has been fixed." : ""}
          </p>
        }
      />
    </>
  );
}

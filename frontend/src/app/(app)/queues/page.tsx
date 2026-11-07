"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import useSWR from "swr";

import { IconPause, IconPlay, IconPlus } from "@/components/icons";
import {
  Button,
  ConfirmDialog,
  Dialog,
  ErrorState,
  Field,
  Input,
  Mono,
  PageHeader,
  Panel,
  SkeletonRows,
  StateBadge,
  Td,
  Th,
  Time,
  cx,
  errorMessage,
  useToast,
} from "@/components/ui";
import { ApiError, api, type QueueOut, type WorkerOut } from "@/lib/api";
import { formatBytes, formatNumber, formatSeconds } from "@/lib/format";
import { useProject, useSession } from "@/lib/session";

function QueueDialog({
  projectId,
  queue,
  open,
  onClose,
  onSaved,
}: {
  projectId: string;
  queue: QueueOut | null;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [initialised, setInitialised] = useState<string | null>(null);
  const toast = useToast();

  const id = queue?.id ?? "new";
  if (open && initialised !== id) {
    setInitialised(id);
    setName(queue?.name ?? "");
    setDescription(queue?.description ?? "");
    setLimit(queue?.concurrency_limit ? String(queue.concurrency_limit) : "");
    setErrors({});
    setError(null);
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setError(null);
    try {
      if (queue) {
        await api.patch(`/projects/${projectId}/queues/${queue.id}`, {
          description,
          ...(limit ? { concurrency_limit: Number(limit) } : { clear_concurrency_limit: true }),
        });
        toast("success", `Queue ${queue.name} updated`);
      } else {
        await api.post(`/projects/${projectId}/queues`, {
          name,
          description,
          concurrency_limit: limit ? Number(limit) : null,
        });
        toast("success", `Queue ${name} created`);
      }
      onSaved();
      onClose();
      setInitialised(null);
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError) setErrors(err.fieldErrors());
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => {
        onClose();
        setInitialised(null);
      }}
      title={queue ? `Edit queue ${queue.name}` : "Create queue"}
      footer={
        <Button type="submit" form="queue-form" variant="primary" busy={busy}>
          {queue ? "Save" : "Create queue"}
        </Button>
      }
    >
      <form id="queue-form" onSubmit={submit} className="flex flex-col gap-3" noValidate>
        {!queue ? (
          <Field label="Name" htmlFor="qn" error={errors.name} hint="Lowercase letters, digits, dot, dash, underscore.">
            <Input id="qn" value={name} onChange={(e) => setName(e.target.value)} required pattern="[a-z0-9][a-z0-9_.\-]{0,63}" />
          </Field>
        ) : null}
        <Field label="Description" htmlFor="qd" error={errors.description}>
          <Input id="qd" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={255} />
        </Field>
        <Field
          label="Concurrency limit"
          htmlFor="ql"
          error={errors.concurrency_limit}
          hint="Maximum jobs from this queue running at once across all workers. Empty means unlimited (bounded by worker slots)."
        >
          <Input id="ql" type="number" min={1} max={1000} value={limit} onChange={(e) => setLimit(e.target.value)} />
        </Field>
        {error ? (
          <p role="alert" className="text-xs text-bad">
            {error}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

export default function QueuesPage() {
  const project = useProject();
  const { isOwner } = useSession();
  const toast = useToast();
  const queues = useSWR<QueueOut[]>(`/projects/${project.id}/queues`);
  const workers = useSWR<WorkerOut[]>(`/projects/${project.id}/workers`, { refreshInterval: 10_000 });
  const [editing, setEditing] = useState<QueueOut | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<QueueOut | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const togglePause = async (q: QueueOut) => {
    setBusy(q.id);
    try {
      await api.patch(`/projects/${project.id}/queues/${q.id}`, { paused: !q.paused });
      toast("success", q.paused ? `Queue ${q.name} resumed` : `Queue ${q.name} paused; running jobs finish, no new ones start`);
      await queues.mutate();
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(deleting.id);
    try {
      await api.delete(`/projects/${project.id}/queues/${deleting.id}`);
      toast("success", `Queue ${deleting.name} deleted`);
      await queues.mutate();
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(null);
      setDeleting(null);
    }
  };

  const liveWorkers = (workers.data ?? []).filter((w) => w.state === "running" || w.state === "draining");

  return (
    <>
      <PageHeader
        title="Queues & workers"
        description="Queues group jobs and carry concurrency limits. Workers are shared processes that claim jobs from every queue they serve."
        actions={
          isOwner ? (
            <Button variant="primary" icon={<IconPlus />} onClick={() => setCreating(true)}>
              New queue
            </Button>
          ) : null
        }
      />

      {queues.error ? <ErrorState error={queues.error} onRetry={() => queues.mutate()} /> : null}
      <Panel title="Queues" bodyClassName="overflow-x-auto" description={isOwner ? undefined : "Only project owners can change queues."}>
        <table className="w-full min-w-[820px] text-[13px]">
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th className="text-right">Queued</Th>
              <Th className="text-right">Running / limit</Th>
              <Th className="text-right">Retrying</Th>
              <Th className="text-right">Scheduled</Th>
              <Th className="text-right">Oldest queued</Th>
              <Th className="text-right" title="Entries in the Redis dispatch set; should track Queued. A gap is repaired by the reconciler.">
                Dispatch set
              </Th>
              {isOwner ? <Th className="text-right">Actions</Th> : null}
            </tr>
          </thead>
          {!queues.data && !queues.error ? (
            <SkeletonRows rows={3} cols={isOwner ? 9 : 8} />
          ) : (
            <tbody>
              {(queues.data ?? []).map((q) => (
                <tr key={q.id} className="border-b border-line last:border-0">
                  <Td>
                    <Mono>{q.name}</Mono>
                    {q.description ? <p className="text-xs text-muted">{q.description}</p> : null}
                  </Td>
                  <Td>
                    <StateBadge state={q.paused ? "paused" : "active"} />
                  </Td>
                  <Td className="text-right num">{q.queued}</Td>
                  <Td className="text-right num">
                    {q.running}
                    <span className="text-muted"> / {q.concurrency_limit ?? "∞"}</span>
                  </Td>
                  <Td className="text-right num">{q.retrying}</Td>
                  <Td className="text-right num">{q.scheduled}</Td>
                  <Td className="text-right">{q.oldest_queued_at ? <Time iso={q.oldest_queued_at} /> : <span className="text-muted">—</span>}</Td>
                  <Td className={cx("text-right num", q.dispatch_depth !== null && q.dispatch_depth !== undefined && q.dispatch_depth < q.queued ? "text-warn" : "text-ink-2")}>
                    {q.dispatch_depth ?? "unavailable"}
                  </Td>
                  {isOwner ? (
                    <Td className="text-right whitespace-nowrap">
                      <Button size="sm" variant="ghost" busy={busy === q.id} icon={q.paused ? <IconPlay /> : <IconPause />} onClick={() => togglePause(q)}>
                        {q.paused ? "Resume" : "Pause"}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditing(q)}>
                        Edit
                      </Button>
                      {q.name !== "default" ? (
                        <Button size="sm" variant="ghost" onClick={() => setDeleting(q)}>
                          Delete
                        </Button>
                      ) : null}
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          )}
        </table>
      </Panel>

      <Panel
        className="mt-4"
        title="Workers"
        description={`${liveWorkers.length} active · ${liveWorkers.reduce((n, w) => n + w.concurrency, 0)} execution slots · refreshed every 10 s`}
        bodyClassName="overflow-x-auto"
      >
        {workers.error ? <ErrorState error={workers.error} /> : null}
        {workers.data && workers.data.length === 0 ? (
          <div className="px-4 py-6 text-xs text-muted">
            No worker has reported in the last 24 hours. Start one with <code className="font-mono">queueforge worker</code> (or the{" "}
            <code className="font-mono">worker</code> service in Docker Compose). Jobs stay safely queued until then.
          </div>
        ) : (
          <table className="w-full min-w-[900px] text-[13px]">
            <thead>
              <tr>
                <Th>Worker</Th>
                <Th>Health</Th>
                <Th className="text-right">Heartbeat</Th>
                <Th className="text-right">Slots</Th>
                <Th className="text-right">Processed</Th>
                <Th className="text-right">Failed</Th>
                <Th>Dispatch</Th>
                <Th className="text-right">Memory</Th>
                <Th>Running here</Th>
              </tr>
            </thead>
            {!workers.data ? (
              <SkeletonRows rows={2} cols={9} />
            ) : (
              <tbody>
                {workers.data.map((w) => (
                  <tr key={w.id} className="border-b border-line last:border-0">
                    <Td>
                      <Mono>{w.id}</Mono>
                      <p className="text-xs text-muted">
                        {w.hostname} · pid {w.pid} · v{w.version} · queues {w.queues.length ? w.queues.join(", ") : "all"}
                      </p>
                    </Td>
                    <Td>
                      <StateBadge state={w.health} />
                    </Td>
                    <Td className={cx("text-right num", w.health === "stale" ? "text-warn" : "text-ink-2")}>
                      {w.state === "stopped" ? <Time iso={w.stopped_at} /> : `${formatSeconds(w.heartbeat_age_seconds)} ago`}
                    </Td>
                    <Td className="text-right num">
                      {w.running_jobs}/{w.concurrency}
                    </Td>
                    <Td className="text-right num">{formatNumber(w.processed_count)}</Td>
                    <Td className="text-right num">{formatNumber(w.failed_count)}</Td>
                    <Td>
                      <span className={cx("font-mono text-xs", w.dispatch_mode === "postgres" ? "text-warn" : "text-ink-2")} title={w.dispatch_mode === "postgres" ? "Redis unreachable: claiming directly from PostgreSQL" : "Popping from Redis dispatch sets"}>
                        {w.dispatch_mode}
                      </span>
                    </Td>
                    <Td className="text-right num text-ink-2">{formatBytes(w.rss_bytes)}</Td>
                    <Td className="text-xs">
                      {w.project_jobs.length === 0 ? (
                        <span className="text-muted">—</span>
                      ) : (
                        w.project_jobs.map((j) => (
                          <Link key={j.id} href={`/jobs/${j.id}`} className="mr-2 font-mono text-ink underline decoration-line-strong underline-offset-2 transition-colors hover:decoration-copper">
                            {j.id.slice(0, 8)}
                          </Link>
                        ))
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            )}
          </table>
        )}
        <p className="border-t border-line px-4 py-2 text-xs text-muted">
          A worker is <em>stale</em> after missing two heartbeats and <em>lost</em> after three; jobs it held are recovered when their lease
          expires and retried with backoff.
        </p>
      </Panel>

      <QueueDialog projectId={project.id} queue={editing} open={creating || editing !== null} onClose={() => { setCreating(false); setEditing(null); }} onSaved={() => void queues.mutate()} />
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete queue ${deleting?.name ?? ""}?`}
        confirmLabel="Delete queue"
        danger
        busy={busy === deleting?.id}
        onClose={() => setDeleting(null)}
        onConfirm={remove}
        body={<p>Only queues without job records or schedules can be deleted. Pause the queue instead if it still has history.</p>}
      />
    </>
  );
}

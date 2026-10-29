"use client";

import { useState, type FormEvent } from "react";
import useSWR from "swr";

import {
  Button,
  ErrorState,
  Field,
  Input,
  PageHeader,
  Pagination,
  Panel,
  SkeletonRows,
  Td,
  Th,
  Time,
  errorMessage,
  useToast,
} from "@/components/ui";
import { ApiError, api, type AuditPage, type Project } from "@/lib/api";
import { useProject, useSession } from "@/lib/session";

type FormState = Record<
  | "name"
  | "default_max_attempts"
  | "default_timeout_seconds"
  | "default_backoff_base_seconds"
  | "default_backoff_max_seconds"
  | "retention_days"
  | "max_queued_jobs",
  string
>;

const FIELDS: { key: keyof FormState; label: string; hint: string; min: number; max: number }[] = [
  { key: "default_max_attempts", label: "Max attempts", hint: "Per job, 1–25. Includes the first attempt.", min: 1, max: 25 },
  { key: "default_timeout_seconds", label: "Timeout (seconds)", hint: "Per attempt. Handlers can lower it; jobs can override.", min: 1, max: 86400 },
  { key: "default_backoff_base_seconds", label: "Backoff base (seconds)", hint: "Delay before retry n is base × 2^(n−1), jittered.", min: 1, max: 86400 },
  { key: "default_backoff_max_seconds", label: "Backoff cap (seconds)", hint: "Upper bound on any single retry delay.", min: 1, max: 86400 },
  { key: "retention_days", label: "Retention (days)", hint: "Finished jobs older than this are deleted with their history.", min: 1, max: 365 },
  { key: "max_queued_jobs", label: "Backlog cap", hint: "Pending jobs allowed before submissions get 429.", min: 1, max: 10_000_000 },
];

function toForm(p: Project): FormState {
  return {
    name: p.name,
    default_max_attempts: String(p.default_max_attempts),
    default_timeout_seconds: String(p.default_timeout_seconds),
    default_backoff_base_seconds: String(p.default_backoff_base_seconds),
    default_backoff_max_seconds: String(p.default_backoff_max_seconds),
    retention_days: String(p.retention_days),
    max_queued_jobs: String(p.max_queued_jobs),
  };
}

export default function SettingsPage() {
  const project = useProject();
  const { isOwner, refreshProjects } = useSession();
  const toast = useToast();
  const [form, setForm] = useState<FormState>(() => toForm(project));
  const [busy, setBusy] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);
  const audit = useSWR<AuditPage>(isOwner ? `/projects/${project.id}/audit?limit=20&offset=${offset}` : null);

  const [formFor, setFormFor] = useState(project);
  if (formFor !== project) {
    setFormFor(project);
    setForm(toForm(project));
  }

  const original = toForm(project);
  const changed = (Object.keys(form) as (keyof FormState)[]).filter((k) => form[k] !== original[k]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFields({});
    const body: Record<string, string | number> = {};
    for (const k of changed) body[k] = k === "name" ? form[k] : Number(form[k]);
    try {
      await api.patch(`/projects/${project.id}`, body);
      await refreshProjects();
      await audit.mutate();
      toast("success", "Settings saved. New defaults apply to jobs submitted from now on.");
    } catch (err) {
      toast("error", errorMessage(err));
      if (err instanceof ApiError) setFields(err.fieldErrors());
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Settings"
        description="Project defaults for retries, timeouts, retention and backlog limits. Existing jobs keep the policy they were submitted with."
      />
      <div className="grid gap-4 xl:grid-cols-[3fr_2fr]">
        <Panel title="Job defaults" description={isOwner ? undefined : "Read-only: only project owners can change settings."}>
          <form onSubmit={save} className="flex flex-col gap-4" noValidate>
            <Field label="Project name" htmlFor="s-name" error={fields.name}>
              <Input id="s-name" value={form.name} disabled={!isOwner} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={120} />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              {FIELDS.map((f) => (
                <Field key={f.key} label={f.label} hint={f.hint} htmlFor={`s-${f.key}`} error={fields[f.key]}>
                  <Input
                    id={`s-${f.key}`}
                    type="number"
                    min={f.min}
                    max={f.max}
                    value={form[f.key]}
                    disabled={!isOwner}
                    onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                    aria-invalid={!!fields[f.key]}
                  />
                </Field>
              ))}
            </div>
            {isOwner ? (
              <div className="flex items-center justify-end gap-2 border-t border-line pt-3">
                {changed.length ? <span className="mr-auto text-xs text-muted">{changed.length} unsaved change(s)</span> : null}
                <Button type="button" variant="ghost" disabled={!changed.length} onClick={() => setForm(original)}>
                  Reset
                </Button>
                <Button type="submit" variant="primary" busy={busy} disabled={!changed.length}>
                  Save settings
                </Button>
              </div>
            ) : null}
          </form>
        </Panel>
        <Panel title="Platform behaviour" description="Set by the operator through QF_* environment variables">
          <ul className="flex list-disc flex-col gap-2 pl-4 text-xs text-ink-2">
            <li>Execution is at-least-once: a job can run again if its worker loses its lease. Make handlers idempotent.</li>
            <li>Running jobs hold a lease that is renewed by heartbeat; an expired lease is recovered by the scheduler and retried.</li>
            <li>Queue concurrency limits apply across all workers and are edited on the Queues page.</li>
            <li>Request bodies and payloads are size-limited; oversize submissions are rejected with 413.</li>
            <li>API requests are rate limited per key and per user; exceeding the limit returns 429 with Retry-After.</li>
          </ul>
        </Panel>
      </div>

      {isOwner ? (
        <Panel title="Audit log" className="mt-4" description="Changes to keys, queues, schedules, settings and bulk actions" bodyClassName="overflow-x-auto">
          {audit.error ? <ErrorState error={audit.error} /> : null}
          <table className="w-full min-w-[680px] text-[13px]">
            <thead>
              <tr>
                <Th>When</Th>
                <Th>Action</Th>
                <Th>Actor</Th>
                <Th>Details</Th>
              </tr>
            </thead>
            {!audit.data ? (
              <SkeletonRows rows={4} cols={4} />
            ) : (
              <tbody>
                {audit.data.items.map((e) => (
                  <tr key={e.id} className="border-b border-line align-top last:border-0">
                    <Td className="text-ink-2">
                      <Time iso={e.at} />
                    </Td>
                    <Td className="font-mono text-xs">{e.action}</Td>
                    <Td className="text-xs">{e.actor}</Td>
                    <Td className="max-w-[420px] truncate font-mono text-[11px] text-muted" title={JSON.stringify(e.details)}>
                      {JSON.stringify(e.details)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            )}
          </table>
          {audit.data ? (
            <div className="border-t border-line">
              <Pagination total={audit.data.total} limit={20} offset={offset} onChange={setOffset} />
            </div>
          ) : null}
        </Panel>
      ) : null}
    </>
  );
}

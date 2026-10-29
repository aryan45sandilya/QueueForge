"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import useSWR from "swr";

import { IconPlay, IconPlus } from "@/components/icons";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Mono,
  PageHeader,
  Panel,
  Select,
  SkeletonRows,
  StateBadge,
  Td,
  Textarea,
  Th,
  Time,
  errorMessage,
  useToast,
} from "@/components/ui";
import { ApiError, api, type JobType, type QueueOut, type ScheduleOut, type SchedulePreview } from "@/lib/api";
import { absoluteTime, pretty } from "@/lib/format";
import { useProject } from "@/lib/session";

const CRON_PRESETS = [
  { label: "Every 5 minutes", value: "*/5 * * * *" },
  { label: "Hourly", value: "0 * * * *" },
  { label: "Daily at 02:00 UTC", value: "0 2 * * *" },
  { label: "Weekdays at 09:00 UTC", value: "0 9 * * 1-5" },
];
const UNITS = { minutes: 60, hours: 3600, days: 86400 } as const;

function triggerText(s: ScheduleOut): string {
  if (s.interval_seconds) {
    const n = s.interval_seconds;
    if (n % 86400 === 0) return `every ${n / 86400} day(s)`;
    if (n % 3600 === 0) return `every ${n / 3600} hour(s)`;
    if (n % 60 === 0) return `every ${n / 60} minute(s)`;
    return `every ${n} s`;
  }
  return `cron ${s.cron_expression} UTC`;
}

function ScheduleForm({ projectId, onClose, onSaved }: { projectId: string; onClose: () => void; onSaved: () => void }) {
  const types = useSWR<JobType[]>("/job-types");
  const queues = useSWR<QueueOut[]>(`/projects/${projectId}/queues`);
  const [name, setName] = useState("");
  const [chosenType, setJobType] = useState("");
  const [queue, setQueue] = useState("default");
  const [mode, setMode] = useState<"cron" | "interval">("cron");
  const [cron, setCron] = useState("0 * * * *");
  const [every, setEvery] = useState("15");
  const [unit, setUnit] = useState<keyof typeof UNITS>("minutes");
  const [editedPayload, setPayload] = useState<string | null>(null);
  const [priority, setPriority] = useState("5");
  const [skip, setSkip] = useState(true);
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});

  const jobType = chosenType || (types.data?.find((x) => x.name === "report.generate") ?? types.data?.[0])?.name || "";
  const typeSpec = types.data?.find((x) => x.name === jobType);
  const payload = editedPayload ?? (typeSpec ? pretty(typeSpec.example) : "{}");

  const trigger = mode === "cron" ? { cron_expression: cron.trim() } : { interval_seconds: Number(every) * UNITS[unit] };

  useEffect(() => {
    const handle = setTimeout(async () => {
      try {
        setPreview(await api.post<SchedulePreview>("/schedules/preview", { ...trigger, count: 3 }));
        setPreviewError(null);
      } catch (err) {
        setPreview(null);
        setPreviewError(err instanceof ApiError ? (Object.values(err.fieldErrors())[0] ?? err.message) : "Preview unavailable");
      }
    }, 300);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, cron, every, unit]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setFields({});
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload);
    } catch {
      setFields({ payload: "Payload is not valid JSON" });
      return;
    }
    setBusy(true);
    try {
      await api.post(`/projects/${projectId}/schedules`, {
        name,
        job_type: jobType,
        queue,
        payload: parsed,
        priority: Number(priority),
        skip_if_running: skip,
        ...trigger,
      });
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError) {
        const fe = err.fieldErrors();
        const mapped: Record<string, string> = {};
        for (const [k, v] of Object.entries(fe)) mapped[k.startsWith("payload") ? "payload" : k] = v;
        setFields(mapped);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form id="schedule-form" onSubmit={submit} className="flex flex-col gap-3" noValidate>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Name" htmlFor="sn" error={fields.name}>
          <Input id="sn" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} />
        </Field>
        <Field label="Job type" htmlFor="st" error={fields.job_type}>
          <Select
            id="st"
            value={jobType}
            onChange={(e) => {
              setJobType(e.target.value);
              setPayload(null);
            }}
          >
            {(types.data ?? []).map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Queue" htmlFor="sq" error={fields.queue}>
          <Select id="sq" value={queue} onChange={(e) => setQueue(e.target.value)}>
            {(queues.data ?? []).map((q) => (
              <option key={q.id} value={q.name}>
                {q.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <fieldset className="rounded-[3px] border border-line p-3">
        <legend className="px-1 text-xs font-medium text-ink-2">Trigger (UTC)</legend>
        <div className="mb-3 flex gap-4 text-[13px]">
          <label className="flex items-center gap-1.5">
            <input type="radio" name="mode" checked={mode === "cron"} onChange={() => setMode("cron")} className="accent-[var(--qf-copper-strong)]" />
            Cron expression
          </label>
          <label className="flex items-center gap-1.5">
            <input type="radio" name="mode" checked={mode === "interval"} onChange={() => setMode("interval")} className="accent-[var(--qf-copper-strong)]" />
            Fixed interval
          </label>
        </div>
        {mode === "cron" ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Expression" htmlFor="sc" hint="minute hour day-of-month month day-of-week" error={fields.cron_expression}>
              <Input id="sc" className="font-mono" value={cron} onChange={(e) => setCron(e.target.value)} />
            </Field>
            <Field label="Presets" htmlFor="sp">
              <Select id="sp" value="" onChange={(e) => e.target.value && setCron(e.target.value)}>
                <option value="">Choose a preset…</option>
                {CRON_PRESETS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Every" htmlFor="se" error={fields.interval_seconds} hint="Minimum 10 seconds">
              <Input id="se" type="number" min={1} value={every} onChange={(e) => setEvery(e.target.value)} />
            </Field>
            <Field label="Unit" htmlFor="su">
              <Select id="su" value={unit} onChange={(e) => setUnit(e.target.value as keyof typeof UNITS)}>
                {Object.keys(UNITS).map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        )}
        <div className="mt-2 text-xs" aria-live="polite">
          {previewError ? (
            <p className="text-bad">{previewError}</p>
          ) : preview ? (
            <p className="text-muted">
              {preview.description}. Next: {preview.next_runs.map((r) => absoluteTime(r)).join(" · ")}
            </p>
          ) : null}
        </div>
      </fieldset>

      <Field label="Payload (JSON)" htmlFor="spl" error={fields.payload}>
        <Textarea id="spl" rows={7} value={payload} onChange={(e) => setPayload(e.target.value)} spellCheck={false} />
      </Field>
      <div className="flex flex-wrap items-end gap-4">
        <Field label="Priority" htmlFor="spr" className="w-28">
          <Input id="spr" type="number" min={0} max={9} value={priority} onChange={(e) => setPriority(e.target.value)} />
        </Field>
        <Checkbox
          label="Skip a run while the previous one is still active"
          checked={skip}
          onChange={(e) => setSkip(e.currentTarget.checked)}
        />
      </div>
      {error ? (
        <p role="alert" className="border-l-2 border-bad bg-bad-tint/60 px-3 py-2 text-xs text-bad">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end gap-2 border-t border-line pt-3">
        <Button variant="ghost" type="button" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="primary" type="submit" busy={busy} disabled={!name || !!previewError}>
          Create schedule
        </Button>
      </div>
    </form>
  );
}

export default function SchedulesPage() {
  const project = useProject();
  const toast = useToast();
  const schedules = useSWR<ScheduleOut[]>(`/projects/${project.id}/schedules`);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<ScheduleOut | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const act = async (s: ScheduleOut, fn: () => Promise<unknown>, message: string) => {
    setBusy(s.id);
    try {
      await fn();
      toast("success", message);
      await schedules.mutate();
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <PageHeader
        title="Schedules"
        description="Recurring jobs. The scheduler creates one job per occurrence; missed occurrences while it was down are coalesced into a single run. Delayed one-off jobs are submitted from the Jobs page with a delay."
        actions={
          <Button variant="primary" icon={<IconPlus />} onClick={() => setCreating(true)}>
            New schedule
          </Button>
        }
      />
      {schedules.error ? <ErrorState error={schedules.error} onRetry={() => schedules.mutate()} /> : null}
      <Panel bodyClassName="overflow-x-auto">
        <table className="w-full min-w-[880px] text-[13px]">
          <thead>
            <tr>
              <Th>Schedule</Th>
              <Th>Trigger</Th>
              <Th>Next runs</Th>
              <Th>Last run</Th>
              <Th>Status</Th>
              <Th className="text-right">Actions</Th>
            </tr>
          </thead>
          {!schedules.data && !schedules.error ? (
            <SkeletonRows rows={3} cols={6} />
          ) : (
            <tbody>
              {(schedules.data ?? []).map((s) => (
                <tr key={s.id} className="border-b border-line align-top last:border-0">
                  <Td>
                    <p className="font-medium">{s.name}</p>
                    <p className="text-xs text-muted">
                      {s.job_type} → <Mono>{s.queue}</Mono> · priority {s.priority}
                      {s.skip_if_running ? " · skips overlaps" : ""}
                    </p>
                  </Td>
                  <Td className="font-mono text-xs">{triggerText(s)}</Td>
                  <Td className="text-xs">
                    {s.upcoming.length ? (
                      s.upcoming.map((u) => (
                        <p key={u}>
                          <Time iso={u} mode="absolute" />
                        </p>
                      ))
                    ) : (
                      <span className="text-muted">paused</span>
                    )}
                  </Td>
                  <Td className="text-xs">
                    {s.last_job_id ? (
                      <span className="flex flex-wrap items-center gap-1.5">
                        <Link href={`/jobs/${s.last_job_id}`} className="font-mono text-copper-strong hover:underline">
                          {s.last_job_id.slice(0, 8)}
                        </Link>
                        {s.last_job_state ? <StateBadge state={s.last_job_state} /> : <span className="text-muted">(removed)</span>}
                        <Time iso={s.last_run_at} />
                      </span>
                    ) : (
                      <span className="text-muted">never</span>
                    )}
                    {s.last_skipped_at ? (
                      <p className="mt-0.5 text-warn">
                        skipped <Time iso={s.last_skipped_at} /> (previous run active)
                      </p>
                    ) : null}
                  </Td>
                  <Td>
                    <StateBadge state={s.enabled ? "enabled" : "disabled"} />
                  </Td>
                  <Td className="text-right whitespace-nowrap">
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<IconPlay />}
                      busy={busy === s.id}
                      onClick={() =>
                        act(s, () => api.post(`/projects/${project.id}/schedules/${s.id}/trigger`), `Started a run of ${s.name}`)
                      }
                    >
                      Run now
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        act(
                          s,
                          () => api.patch(`/projects/${project.id}/schedules/${s.id}`, { enabled: !s.enabled }),
                          s.enabled ? `${s.name} disabled` : `${s.name} enabled`,
                        )
                      }
                    >
                      {s.enabled ? "Disable" : "Enable"}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setDeleting(s)}>
                      Delete
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          )}
        </table>
        {schedules.data && schedules.data.length === 0 ? (
          <EmptyState title="No schedules">Create one to run a job type on a cron expression or a fixed interval.</EmptyState>
        ) : null}
      </Panel>

      <Dialog open={creating} onClose={() => setCreating(false)} title="New schedule" wide>
        {creating ? <ScheduleForm projectId={project.id} onClose={() => setCreating(false)} onSaved={() => { toast("success", "Schedule created"); void schedules.mutate(); }} /> : null}
      </Dialog>
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete schedule ${deleting?.name ?? ""}?`}
        confirmLabel="Delete schedule"
        danger
        busy={busy === deleting?.id}
        onClose={() => setDeleting(null)}
        onConfirm={() => deleting && act(deleting, () => api.delete(`/projects/${project.id}/schedules/${deleting.id}`), "Schedule deleted").then(() => setDeleting(null))}
        body={<p>Future occurrences stop. Jobs it already created are kept.</p>}
      />
    </>
  );
}

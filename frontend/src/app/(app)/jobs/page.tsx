"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import useSWR from "swr";

import { IconPlus, IconRetry, IconStop } from "@/components/icons";
import { JobRowLink, SubmitJobDialog } from "@/components/jobs";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Input,
  PageHeader,
  Pagination,
  Select,
  SkeletonRows,
  StateBadge,
  Td,
  Th,
  Time,
  cx,
  errorMessage,
  useToast,
} from "@/components/ui";
import { api, type BulkResult, type JobPage, type JobType, type QueueOut } from "@/lib/api";
import { durationBetween, formatDuration } from "@/lib/format";
import {
  DEFAULT_FILTERS,
  JOB_STATES,
  PAGE_SIZES,
  SORT_FIELDS,
  filtersToApiQuery,
  filtersToSearch,
  parseFilters,
  type JobFilters,
} from "@/lib/jobFilters";
import { useProject } from "@/lib/session";

const SORT_LABELS: Record<(typeof SORT_FIELDS)[number], string> = {
  created_at: "Created",
  updated_at: "Updated",
  finished_at: "Finished",
  priority: "Priority",
  attempts: "Attempts",
  run_at: "Run at",
};

function JobsView() {
  const project = useProject();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filters = useMemo(() => parseFilters(new URLSearchParams(params.toString())), [params]);
  const toast = useToast();

  // Keep the search box in sync when the URL changes (back/forward, "clear filters").
  const [search, setSearch] = useState(filters.q);
  const [syncedQ, setSyncedQ] = useState(filters.q);
  if (syncedQ !== filters.q) {
    setSyncedQ(filters.q);
    setSearch(filters.q);
  }

  const key = `/projects/${project.id}/jobs?${filtersToApiQuery(filters)}`;
  const jobs = useSWR<JobPage>(key);
  const types = useSWR<JobType[]>("/job-types");
  const queues = useSWR<QueueOut[]>(`/projects/${project.id}/queues`);

  // Selection belongs to one result set; changing filters or page clears it.
  const [selection, setSelection] = useState<{ key: string; ids: Set<string> }>({ key, ids: new Set() });
  const selected = selection.key === key ? selection.ids : new Set<string>();
  const setSelected = (ids: Set<string>) => setSelection({ key, ids });
  const [bulk, setBulk] = useState<"cancel" | "retry" | null>(null);
  const [busy, setBusy] = useState(false);
  const [submitOpen, setSubmitOpen] = useState(false);

  const update = (patch: Partial<JobFilters>, resetPage = true) => {
    const next = { ...filters, ...patch, offset: resetPage && patch.offset === undefined ? 0 : (patch.offset ?? filters.offset) };
    const qs = filtersToSearch(next);
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  const toggleState = (s: string) =>
    update({ states: filters.states.includes(s) ? filters.states.filter((x) => x !== s) : [...filters.states, s] });

  const items = jobs.data?.items ?? [];
  const allSelected = items.length > 0 && items.every((j) => selected.has(j.id));
  const selectedJobs = items.filter((j) => selected.has(j.id));
  const cancellable = selectedJobs.filter((j) => ["scheduled", "queued", "retrying", "running"].includes(j.state));
  const retryable = selectedJobs.filter((j) => ["failed", "cancelled"].includes(j.state));

  const runBulk = async () => {
    if (!bulk) return;
    const ids = (bulk === "cancel" ? cancellable : retryable).map((j) => j.id);
    setBusy(true);
    try {
      const res = await api.post<BulkResult>(`/projects/${project.id}/jobs/bulk`, { action: bulk, job_ids: ids });
      toast(res.failed ? "error" : "success", `${bulk === "cancel" ? "Cancel" : "Retry"}: ${res.succeeded} succeeded, ${res.failed} failed`);
      setSelected(new Set());
      await jobs.mutate();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setBulk(null);
    }
  };

  const hasFilters = filtersToSearch({ ...filters, offset: 0, limit: DEFAULT_FILTERS.limit, sort: "created_at", order: "desc" }) !== "";

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every job in this project. Filters are kept in the URL so a view can be shared."
        actions={
          <Button variant="primary" icon={<IconPlus />} onClick={() => setSubmitOpen(true)}>
            Submit job
          </Button>
        }
      />

      <div className="mb-3 flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by state">
          {JOB_STATES.map((s) => {
            const on = filters.states.includes(s);
            return (
              <button
                key={s}
                onClick={() => toggleState(s)}
                aria-pressed={on}
                className={cx(
                  "h-7 rounded-[3px] border px-2.5 font-mono text-[11px] transition-colors",
                  on ? "border-ink bg-ink text-paper" : "border-line-strong bg-surface text-ink-2 hover:bg-sunken",
                )}
              >
                {s}
              </button>
            );
          })}
          {hasFilters ? (
            <Button size="sm" variant="ghost" onClick={() => router.replace(pathname)}>
              Clear filters
            </Button>
          ) : null}
        </div>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-[2fr_1fr_1fr_1fr_1fr_1fr_auto]">
          <form
            className="col-span-2 md:col-span-4 xl:col-span-1"
            onSubmit={(e) => {
              e.preventDefault();
              update({ q: search });
            }}
          >
            <label className="sr-only" htmlFor="job-search">
              Search jobs
            </label>
            <Input
              id="job-search"
              placeholder="Search by job ID, idempotency key or type prefix…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onBlur={() => search !== filters.q && update({ q: search })}
            />
          </form>
          <Select aria-label="Job type" value={filters.type} onChange={(e) => update({ type: e.target.value })}>
            <option value="">All types</option>
            {(types.data ?? []).map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </Select>
          <Select aria-label="Queue" value={filters.queue} onChange={(e) => update({ queue: e.target.value })}>
            <option value="">All queues</option>
            {(queues.data ?? []).map((q) => (
              <option key={q.id} value={q.name}>
                {q.name}
              </option>
            ))}
          </Select>
          <Input aria-label="Created from" type="datetime-local" value={filters.from} onChange={(e) => update({ from: e.target.value })} />
          <Input aria-label="Created before" type="datetime-local" value={filters.to} onChange={(e) => update({ to: e.target.value })} />
          <Select
            aria-label="Sort"
            value={`${filters.sort}:${filters.order}`}
            onChange={(e) => {
              const [sort, order] = e.target.value.split(":") as [JobFilters["sort"], JobFilters["order"]];
              update({ sort, order });
            }}
          >
            {SORT_FIELDS.flatMap((f) => [
              <option key={`${f}:desc`} value={`${f}:desc`}>
                {SORT_LABELS[f]} ↓
              </option>,
              <option key={`${f}:asc`} value={`${f}:asc`}>
                {SORT_LABELS[f]} ↑
              </option>,
            ])}
          </Select>
          <Select aria-label="Page size" value={filters.limit} onChange={(e) => update({ limit: Number(e.target.value) })}>
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n} / page
              </option>
            ))}
          </Select>
        </div>
        {filters.fingerprint ? (
          <p className="text-xs text-muted">
            Showing one failure group (<span className="font-mono">{filters.fingerprint}</span>).{" "}
            <button className="underline" onClick={() => update({ fingerprint: "" })}>
              Remove
            </button>
          </p>
        ) : null}
      </div>

      {selected.size > 0 ? (
        <div className="qf-fade mb-2 flex flex-wrap items-center gap-2 rounded-[3px] border border-copper/40 bg-copper-tint/50 px-3 py-2 text-[13px]">
          <span className="num font-medium">{selected.size} selected</span>
          <Button size="sm" icon={<IconStop />} disabled={!cancellable.length} onClick={() => setBulk("cancel")}>
            Cancel {cancellable.length || ""}
          </Button>
          <Button size="sm" icon={<IconRetry />} disabled={!retryable.length} onClick={() => setBulk("retry")}>
            Retry {retryable.length || ""}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      ) : null}

      {jobs.error ? <ErrorState error={jobs.error} onRetry={() => jobs.mutate()} /> : null}

      <div className="overflow-x-auto rounded-[4px] border border-line bg-surface">
        <table className="w-full min-w-[860px] text-[13px]">
          <thead>
            <tr>
              <Th className="w-8">
                <input
                  type="checkbox"
                  aria-label="Select all on this page"
                  checked={allSelected}
                  onChange={() => setSelected(allSelected ? new Set() : new Set(items.map((j) => j.id)))}
                  className="size-3.5 accent-[var(--qf-copper-strong)]"
                />
              </Th>
              <Th>Job</Th>
              <Th>Type</Th>
              <Th>State</Th>
              <Th>Queue</Th>
              <Th className="text-right">Priority</Th>
              <Th className="text-right">Attempts</Th>
              <Th className="text-right">Created</Th>
              <Th className="text-right">Duration</Th>
            </tr>
          </thead>
          {!jobs.data && !jobs.error ? (
            <SkeletonRows rows={8} cols={9} />
          ) : (
            <tbody>
              {items.map((j) => (
                <tr key={j.id} className={cx("border-b border-line last:border-0 hover:bg-paper/60", selected.has(j.id) && "bg-copper-tint/30")}>
                  <Td>
                    <input
                      type="checkbox"
                      aria-label={`Select job ${j.id}`}
                      checked={selected.has(j.id)}
                      onChange={() => {
                        const next = new Set(selected);
                        if (next.has(j.id)) next.delete(j.id);
                        else next.add(j.id);
                        setSelected(next);
                      }}
                      className="size-3.5 accent-[var(--qf-copper-strong)]"
                    />
                  </Td>
                  <Td>
                    <JobRowLink id={j.id} />
                    {j.idempotency_key ? <span className="ml-2 font-mono text-[11px] text-muted">{j.idempotency_key}</span> : null}
                  </Td>
                  <Td>{j.type}</Td>
                  <Td>
                    <StateBadge state={j.state} />
                    {j.cancel_requested && j.state === "running" ? <span className="ml-1.5 text-[11px] text-warn">cancelling</span> : null}
                    {j.error_type && ["failed", "retrying"].includes(j.state) ? (
                      <span className="ml-1.5 font-mono text-[11px] text-muted" title={j.error_message ?? ""}>
                        {j.error_type}
                      </span>
                    ) : null}
                  </Td>
                  <Td className="font-mono text-xs text-ink-2">{j.queue}</Td>
                  <Td className="text-right num">{j.priority}</Td>
                  <Td className="text-right num">
                    {j.attempts}
                    <span className="text-muted">/{j.max_attempts}</span>
                  </Td>
                  <Td className="text-right text-ink-2">
                    <Time iso={j.created_at} />
                  </Td>
                  <Td className="text-right num text-ink-2">{formatDuration(durationBetween(j.started_at, j.finished_at))}</Td>
                </tr>
              ))}
            </tbody>
          )}
        </table>
        {jobs.data && items.length === 0 ? (
          <EmptyState title={hasFilters ? "No jobs match these filters" : "No jobs yet"}>
            {hasFilters ? "Try removing a filter or widening the date range." : "Submit a job with the button above or through the API."}
          </EmptyState>
        ) : null}
        {jobs.data && jobs.data.total > 0 ? (
          <div className="border-t border-line">
            <Pagination total={jobs.data.total} limit={filters.limit} offset={filters.offset} onChange={(offset) => update({ offset }, false)} />
          </div>
        ) : null}
      </div>

      <ConfirmDialog
        open={bulk !== null}
        title={bulk === "cancel" ? "Cancel selected jobs" : "Retry selected jobs"}
        confirmLabel={bulk === "cancel" ? `Cancel ${cancellable.length} jobs` : `Retry ${retryable.length} jobs`}
        danger={bulk === "cancel"}
        busy={busy}
        onClose={() => setBulk(null)}
        onConfirm={runBulk}
        body={
          bulk === "cancel" ? (
            <p>
              {cancellable.length} of {selected.size} selected jobs can be cancelled. Pending jobs stop immediately; running jobs are
              stopped by their worker at its next heartbeat. Finished jobs are skipped.
            </p>
          ) : (
            <p>
              {retryable.length} of {selected.size} selected jobs are failed or cancelled and will be re-queued with a fresh attempt
              budget. Their attempt history is kept.
            </p>
          )
        }
      />

      <SubmitJobDialog
        projectId={project.id}
        open={submitOpen}
        onClose={() => setSubmitOpen(false)}
        onSubmitted={(res) => {
          toast("success", res.created ? `Job ${res.job.id.slice(0, 8)} submitted (${res.job.state})` : "Existing job returned for that idempotency key");
          setSubmitOpen(false);
          void jobs.mutate();
          router.push(`/jobs/${res.job.id}`);
        }}
      />
    </>
  );
}

export default function JobsPage() {
  return (
    <Suspense>
      <JobsView />
    </Suspense>
  );
}

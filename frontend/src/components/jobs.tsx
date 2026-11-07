"use client";

import Link from "next/link";
import { useMemo, useState, type FormEvent } from "react";
import useSWR from "swr";

import { ApiError, api, type JobType, type QueueOut, type Schemas } from "@/lib/api";
import { pretty, shortId } from "@/lib/format";

import { Button, Dialog, Field, Input, Select, Textarea } from "./ui";

export function JobRowLink({ id }: { id: string }) {
  return (
    <Link href={`/jobs/${id}`} className="font-mono text-xs text-ink underline decoration-line-strong underline-offset-2 transition-colors hover:decoration-copper" title={id}>
      {shortId(id)}
    </Link>
  );
}

type SubmitResponse = Schemas["JobSubmitResponse"];

/** Submit-job dialog: fields map 1:1 to POST /projects/{id}/jobs; the server validates the payload. */
export function SubmitJobDialog({
  projectId,
  open,
  onClose,
  onSubmitted,
}: {
  projectId: string;
  open: boolean;
  onClose: () => void;
  onSubmitted: (res: SubmitResponse) => void;
}) {
  const types = useSWR<JobType[]>(open ? "/job-types" : null);
  const queues = useSWR<QueueOut[]>(open ? `/projects/${projectId}/queues` : null);
  const [chosenType, setType] = useState("");
  const [queue, setQueue] = useState("default");
  const [editedPayload, setPayload] = useState<string | null>(null);
  const [priority, setPriority] = useState("5");
  const [delay, setDelay] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // Until the user picks a type or edits the payload, show the first real handler and its example.
  const type = chosenType || (types.data?.find((t) => !t.demo_only) ?? types.data?.[0])?.name || "";
  const selected = useMemo(() => types.data?.find((t) => t.name === type), [types.data, type]);
  const payload = editedPayload ?? (selected ? pretty(selected.example) : "");

  const chooseType = (name: string) => {
    setType(name);
    setPayload(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload || "{}");
    } catch (err) {
      setFieldErrors({ payload: `Payload is not valid JSON: ${(err as Error).message}` });
      return;
    }
    const body: Record<string, unknown> = { type, queue, payload: parsed, priority: Number(priority) };
    if (delay) body.delay_seconds = Number(delay);
    if (maxAttempts) body.max_attempts = Number(maxAttempts);
    if (idempotencyKey.trim()) body.idempotency_key = idempotencyKey.trim();
    setBusy(true);
    try {
      const res = await api.post<SubmitResponse>(`/projects/${projectId}/jobs`, body);
      onSubmitted(res);
      setIdempotencyKey("");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
        const fe = err.fieldErrors();
        // Collapse payload.* paths onto the payload field so they show beside the editor.
        const mapped: Record<string, string> = {};
        for (const [k, v] of Object.entries(fe)) {
          if (k.startsWith("payload")) mapped.payload = mapped.payload ? `${mapped.payload}; ${k}: ${v}` : `${k}: ${v}`;
          else mapped[k] = v;
        }
        setFieldErrors(mapped);
      } else setError("Unexpected error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Submit a job"
      wide
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button variant="primary" type="submit" form="submit-job" busy={busy} disabled={!type}>
            Submit job
          </Button>
        </>
      }
    >
      <form id="submit-job" onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Job type" htmlFor="jt" error={fieldErrors.type} hint={selected?.description}>
            <Select id="jt" value={type} onChange={(e) => chooseType(e.target.value)}>
              {(types.data ?? []).map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                  {t.demo_only ? " (demo)" : ""}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Queue" htmlFor="jq" error={fieldErrors.queue}>
            <Select id="jq" value={queue} onChange={(e) => setQueue(e.target.value)}>
              {(queues.data ?? [{ name: "default" } as QueueOut]).map((q) => (
                <option key={q.name} value={q.name}>
                  {q.name}
                  {q.paused ? " (paused)" : ""}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Payload (JSON)" htmlFor="jp" error={fieldErrors.payload} hint="Validated by the handler's schema on the server.">
          <Textarea id="jp" rows={10} value={payload} onChange={(e) => setPayload(e.target.value)} spellCheck={false} aria-invalid={!!fieldErrors.payload} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Priority" htmlFor="jpr" error={fieldErrors.priority} hint="0–9, higher first">
            <Input id="jpr" type="number" min={0} max={9} value={priority} onChange={(e) => setPriority(e.target.value)} />
          </Field>
          <Field label="Delay (s)" htmlFor="jd" error={fieldErrors.delay_seconds} hint="Empty = now">
            <Input id="jd" type="number" min={0} value={delay} onChange={(e) => setDelay(e.target.value)} />
          </Field>
          <Field label="Max attempts" htmlFor="jm" error={fieldErrors.max_attempts} hint="Project default">
            <Input id="jm" type="number" min={1} max={25} value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value)} />
          </Field>
          <Field label="Idempotency key" htmlFor="ji" error={fieldErrors.idempotency_key} hint="Optional">
            <Input id="ji" value={idempotencyKey} onChange={(e) => setIdempotencyKey(e.target.value)} maxLength={128} />
          </Field>
        </div>
        {error ? (
          <p role="alert" className="border-l-2 border-bad bg-bad-tint/60 px-3 py-2 text-xs text-bad">
            {error}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}

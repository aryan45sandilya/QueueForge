"use client";

import { useState, useSyncExternalStore } from "react";
import useSWR from "swr";

import { CodeBlock, Mono, PageHeader, Panel, Td, Th, TextLink, cx } from "@/components/ui";
import type { JobType } from "@/lib/api";
import { pretty } from "@/lib/format";
import { useProject } from "@/lib/session";

const SECTIONS = [
  ["auth", "Authentication"],
  ["submit", "Submit a job"],
  ["webhooks", "Webhooks"],
  ["inspect", "Inspect and control"],
  ["events", "Live events"],
  ["errors", "Errors"],
  ["delivery", "Delivery semantics"],
  ["types", "Job types"],
] as const;

const ERRORS: [number, string, string][] = [
  [401, "unauthorized", "Missing, malformed, expired or revoked credentials."],
  [403, "forbidden / insufficient_scope / csrf_failed", "Authenticated, but the key lacks a scope, the role is not owner, or a dashboard request lacked the CSRF header."],
  [404, "not_found", "Unknown resource, or a resource in a project your credentials cannot access."],
  [409, "conflict", "Idempotency key reused with a different payload; invalid state for cancel/retry; duplicate names."],
  [413, "payload_too_large", "Request body or job payload above the configured limit."],
  [422, "validation_error", "Body, query or job payload failed validation. details lists each field (loc, msg, type)."],
  [429, "rate_limited / backlog_full", "Too many requests, or the project backlog cap is reached. Honour Retry-After."],
  [503, "service_unavailable", "PostgreSQL is unreachable; nothing was persisted. Safe to retry with the same idempotency key."],
];

export default function DocsPage() {
  const project = useProject();
  const types = useSWR<JobType[]>("/job-types");
  const origin = useSyncExternalStore(
    () => () => {},
    () => window.location.origin,
    () => "http://localhost:3000",
  );
  const base = `${origin}/api/v1/projects/${project.id}`;

  const submit = `curl -sS -X POST "${base}/jobs" \\
  -H "Authorization: Bearer $QF_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "type": "notification.send",
    "queue": "default",
    "priority": 7,
    "idempotency_key": "invoice-2041-email",
    "payload": {
      "channel": "email",
      "template": "invoice-ready",
      "subject": "Invoice {{invoice}}",
      "body": "Hi {{name}}, invoice {{invoice}} is ready.",
      "recipients": [{"address": "ada@example.com", "name": "Ada", "variables": {"invoice": "INV-2041"}}]
    }
  }'`;

  const delayed = `curl -sS -X POST "${base}/jobs" \\
  -H "Authorization: Bearer $QF_API_KEY" -H "Content-Type: application/json" \\
  -d '{"type": "report.generate", "delay_seconds": 600, "max_attempts": 3, "timeout_seconds": 60,
       "payload": {"period_hours": 24, "group_by": "queue"}}'`;

  const inspect = `# One job: payload, result, structured error, attempts and state timeline
curl -sS "${base}/jobs/$JOB_ID" -H "Authorization: Bearer $QF_API_KEY"

# Failed jobs of one type, newest first, 25 per page
curl -sS "${base}/jobs?state=failed&type=data.transform&sort=finished_at&order=desc&limit=25" \\
  -H "Authorization: Bearer $QF_API_KEY"

# Cancel (pending: immediate; running: stopped at the worker's next heartbeat)
curl -sS -X POST "${base}/jobs/$JOB_ID/cancel" -H "Authorization: Bearer $QF_API_KEY"

# Retry a failed or cancelled job with a fresh attempt budget
curl -sS -X POST "${base}/jobs/$JOB_ID/retry" -H "Authorization: Bearer $QF_API_KEY"

# Metrics for the last hour (see "definitions" in the response)
curl -sS "${base}/metrics?window=1h" -H "Authorization: Bearer $QF_API_KEY"`;

  const events = `curl -N "${base}/events" -H "Authorization: Bearer $QF_API_KEY"

event: ready
data: {"project_id": "${project.id}"}

event: job
data: {"type": "job.updated", "job": {"id": "…", "state": "running", "type": "notification.send", "attempts": 1, …}}`;

  const webhookExample = `curl -sS -X POST "${base}/jobs" \\
  -H "Authorization: Bearer $QF_API_KEY" -H "Content-Type: application/json" \\
  -d '{"type": "http.request", "idempotency_key": "digest-2026-11-07",
       "payload": {"url": "https://example.com/hooks/daily-digest", "method": "POST",
                   "headers": {"Authorization": "Bearer <your token>"},
                   "body": {"date": "2026-11-07"}, "timeout_seconds": 15}}'`;

  const verifyNode = `// Node.js: verify before doing any work (use the raw request body, not re-serialized JSON)
import crypto from "node:crypto";

export function verifyQueueForge(rawBody, header, secret, toleranceSeconds = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > toleranceSeconds) return false;
  const expected = crypto.createHmac("sha256", secret).update(\`\${parts.t}.\`).update(rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1 ?? ""));
}`;

  const verifyPython = `# Python: same check
import hashlib, hmac, time

def verify_queueforge(raw_body: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))`;

  const errorExample = `{
  "error": {
    "code": "validation_error",
    "message": "Payload is not valid for job type 'notification.send'",
    "details": [{"loc": ["payload", "recipients"], "msg": "List should have at least 1 item after validation, not 0", "type": "too_short"}],
    "request_id": "c42997448a9f43e8b36b01e538cae4de"
  }
}`;

  return (
    <>
      <PageHeader
        title="Developer documentation"
        description={
          <>
            Examples use this project ({project.slug}). The full OpenAPI reference is at <TextLink href="/api/docs">/api/docs</TextLink>; the
            machine-readable document is <TextLink href="/api/openapi.json">/api/openapi.json</TextLink>.
          </>
        }
      />
      <div className="grid gap-6 lg:grid-cols-[180px_minmax(0,1fr)]">
        <nav aria-label="On this page" className="hidden lg:block">
          <ul className="sticky top-16 flex flex-col gap-1 text-[13px]">
            {SECTIONS.map(([id, label]) => (
              <li key={id}>
                <a href={`#${id}`} className="text-ink-2 hover:text-copper-strong">
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="flex min-w-0 flex-col gap-4 text-[13px] leading-relaxed">
          <Panel title="Authentication">
            <div id="auth" className="flex scroll-mt-16 flex-col gap-2">
              <p>
                Create a key on <TextLink href="/project">Project &amp; API keys</TextLink> and send it as a bearer token. Keys are scoped to
                one project; a key used on another project&apos;s path receives <Mono>404</Mono>. Store it in an environment variable:
              </p>
              <CodeBlock>{`export QF_API_KEY="qf_<prefix>_<secret>"`}</CodeBlock>
              <p className="text-ink-2">
                Scopes: <Mono>jobs:write</Mono>, <Mono>jobs:read</Mono>, <Mono>schedules:write</Mono>, <Mono>schedules:read</Mono>,{" "}
                <Mono>metrics:read</Mono>. Key management and project settings require a dashboard session with the owner role. Responses carry{" "}
                <Mono>X-RateLimit-Limit</Mono>, <Mono>X-RateLimit-Remaining</Mono> and <Mono>X-RateLimit-Reset</Mono>.
              </p>
            </div>
          </Panel>
          <Panel title="Submit a job">
            <div id="submit" className="flex scroll-mt-16 flex-col gap-2">
              <p>
                The job is committed to PostgreSQL before the response is sent. <Mono>201</Mono> means created; repeating the request with the
                same <Mono>idempotency_key</Mono> and payload returns the original job with <Mono>200</Mono> and <Mono>&quot;created&quot;: false</Mono>.
              </p>
              <CodeBlock>{submit}</CodeBlock>
              <p>Delay a job with <Mono>delay_seconds</Mono> or <Mono>run_at</Mono> (ISO 8601 with offset), and override the retry policy per job:</p>
              <CodeBlock>{delayed}</CodeBlock>
            </div>
          </Panel>
          <Panel title="Webhooks: run your own code">
            <div id="webhooks" className="flex scroll-mt-16 flex-col gap-2">
              <p>
                <Mono>http.request</Mono> calls a URL you own. Network errors, timeouts, <Mono>429</Mono> and <Mono>5xx</Mono> responses are
                retried with backoff; other <Mono>4xx</Mono> responses fail the job permanently. Redirects aren&apos;t followed, and URLs must
                resolve to a public address.
              </p>
              <CodeBlock>{webhookExample}</CodeBlock>
              <p>
                Every call includes <Mono>Idempotency-Key</Mono> (the job ID, the same on every retry), <Mono>X-QueueForge-Attempt</Mono> and{" "}
                <Mono>X-QueueForge-Signature: t=&lt;unix seconds&gt;,v1=&lt;hex&gt;</Mono>. Verify it with the secret from{" "}
                <TextLink href="/project">Project &amp; API keys</TextLink> before trusting the request:
              </p>
              <CodeBlock>{verifyNode}</CodeBlock>
              <CodeBlock>{verifyPython}</CodeBlock>
            </div>
          </Panel>
          <Panel title="Inspect and control">
            <div id="inspect" className="scroll-mt-16">
              <CodeBlock>{inspect}</CodeBlock>
            </div>
          </Panel>
          <Panel title="Live events (Server-Sent Events)">
            <div id="events" className="flex scroll-mt-16 flex-col gap-2">
              <p>
                Every state change is published to the project&apos;s stream. A <Mono>: ping</Mono> comment arrives every 15 s. If Redis is
                unavailable you receive <Mono>event: unavailable</Mono> and the stream closes; poll the jobs endpoint instead and reconnect later.
              </p>
              <CodeBlock>{events}</CodeBlock>
            </div>
          </Panel>
          <Panel title="Errors" bodyClassName="overflow-x-auto">
            <div id="errors" className="scroll-mt-16">
              <div className="px-4 pt-4">
                <p className="mb-2">Every error uses the same shape. Quote the request_id when reporting a problem.</p>
                <CodeBlock>{errorExample}</CodeBlock>
              </div>
              <table className="mt-3 w-full min-w-[600px]">
                <thead>
                  <tr>
                    <Th>Status</Th>
                    <Th>Code</Th>
                    <Th>Meaning</Th>
                  </tr>
                </thead>
                <tbody>
                  {ERRORS.map(([status, code, meaning]) => (
                    <tr key={status} className="border-t border-line align-top">
                      <Td className="num">{status}</Td>
                      <Td className="font-mono text-xs">{code}</Td>
                      <Td className="text-ink-2">{meaning}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
          <Panel title="Delivery semantics">
            <ul id="delivery" className="flex scroll-mt-16 list-disc flex-col gap-1.5 pl-4 text-ink-2">
              <li>
                <span className="text-ink">At-least-once execution.</span> A handler can run more than once if its worker loses its lease
                (crash, partition, stall). Use the job ID as a deduplication key for external side effects.
              </li>
              <li>
                <span className="text-ink">Retries.</span> Retryable errors are retried with exponential backoff and jitter until{" "}
                <Mono>max_attempts</Mono>; permanent errors and exhausted jobs move to <Mono>failed</Mono> (the dead letter).
              </li>
              <li>
                <span className="text-ink">Ordering.</span> Higher priority first, then first-in first-out within a queue. Retries rejoin at the
                back when their backoff ends. There is no strict ordering across workers.
              </li>
              <li>
                <span className="text-ink">Cancellation.</span> Pending jobs cancel immediately. Running jobs are interrupted cooperatively at
                the next heartbeat; side effects already performed are not undone.
              </li>
            </ul>
          </Panel>
          <Panel title="Job types" description="Payload schemas published by GET /api/v1/job-types">
            <div id="types" className="flex scroll-mt-16 flex-col gap-4">
              {(types.data ?? []).map((t) => (
                <JobTypeDoc key={t.name} type={t} />
              ))}
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}

function JobTypeDoc({ type }: { type: JobType }) {
  const [view, setView] = useState<"example" | "schema">("example");
  return (
    <section className="border-b border-line pb-4 last:border-0 last:pb-0">
      <h3 className="flex flex-wrap items-baseline gap-2">
        <Mono className="text-[13px] font-medium">{type.name}</Mono>
        <span className="text-xs text-muted">
          {type.title} · default timeout {type.default_timeout_seconds} s{type.demo_only ? " · demo/benchmark" : ""}
        </span>
      </h3>
      <p className="mt-1 text-ink-2">{type.description}</p>
      {type.side_effects ? <p className="mt-1 text-xs text-muted">Side effects: {type.side_effects}</p> : null}
      <div className="mt-2 mb-1.5 flex gap-1 text-xs">
        {(["example", "schema"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            aria-pressed={view === v}
            className={cx("rounded-[3px] px-2 py-0.5", view === v ? "bg-ink text-paper" : "text-ink-2 hover:bg-sunken")}
          >
            {v === "example" ? "Example payload" : "JSON Schema"}
          </button>
        ))}
      </div>
      <CodeBlock>{pretty(view === "example" ? type.example : type.payload_schema)}</CodeBlock>
    </section>
  );
}

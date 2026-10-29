"use client";

import { useState, type FormEvent } from "react";
import useSWR from "swr";

import { CreateProjectForm } from "@/components/CreateProjectForm";
import { IconPlus } from "@/components/icons";
import { SecretReveal } from "@/components/SecretReveal";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  CopyButton,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  KeyValue,
  Mono,
  PageHeader,
  Panel,
  Select,
  SkeletonRows,
  StateBadge,
  Td,
  Th,
  Time,
  errorMessage,
  useToast,
} from "@/components/ui";
import { ApiError, api, type ApiKeyCreated, type ApiKeyOut } from "@/lib/api";
import { useProject, useSession } from "@/lib/session";

const SCOPES = [
  { key: "jobs:write", label: "Submit, cancel and retry jobs" },
  { key: "jobs:read", label: "Read jobs, queues and live events" },
  { key: "schedules:write", label: "Manage schedules" },
  { key: "schedules:read", label: "Read schedules" },
  { key: "metrics:read", label: "Read metrics and workers" },
];

function CreateKeyDialog({ projectId, open, onClose, onCreated }: { projectId: string; open: boolean; onClose: () => void; onCreated: (k: ApiKeyCreated) => void }) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(SCOPES.map((s) => s.key));
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFields({});
    try {
      const created = await api.post<ApiKeyCreated>(`/projects/${projectId}/api-keys`, {
        name,
        scopes,
        expires_in_days: expires ? Number(expires) : null,
      });
      onCreated(created);
      setName("");
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError) setFields(err.fieldErrors());
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Create API key"
      footer={
        <Button variant="primary" type="submit" form="key-form" busy={busy} disabled={!name || scopes.length === 0}>
          Create key
        </Button>
      }
    >
      <form id="key-form" onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <Field label="Name" htmlFor="kn" error={fields.name} hint="Where the key is used, e.g. billing-service-prod.">
          <Input id="kn" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />
        </Field>
        <fieldset>
          <legend className="mb-1.5 text-xs font-medium text-ink-2">Scopes</legend>
          <div className="flex flex-col gap-1.5">
            {SCOPES.map((s) => (
              <Checkbox
                key={s.key}
                checked={scopes.includes(s.key)}
                onChange={(e) => setScopes(e.currentTarget.checked ? [...scopes, s.key] : scopes.filter((x) => x !== s.key))}
                label={
                  <>
                    <Mono>{s.key}</Mono> <span className="text-muted">{s.label}</span>
                  </>
                }
              />
            ))}
          </div>
          {fields.scopes ? <p className="mt-1 text-xs text-bad">{fields.scopes}</p> : null}
        </fieldset>
        <Field label="Expires after (days)" htmlFor="ke" error={fields.expires_in_days} hint="Empty for no expiry.">
          <Input id="ke" type="number" min={1} max={3650} value={expires} onChange={(e) => setExpires(e.target.value)} />
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

export default function ProjectPage() {
  const project = useProject();
  const { isOwner, projects, selectProject, refreshProjects } = useSession();
  const toast = useToast();
  const keys = useSWR<ApiKeyOut[]>(isOwner ? `/projects/${project.id}/api-keys` : null);
  const [creating, setCreating] = useState(false);
  const [newProject, setNewProject] = useState(false);
  const [secret, setSecret] = useState<{ value: string; name: string } | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);
  const [rotating, setRotating] = useState<ApiKeyOut | null>(null);
  const [grace, setGrace] = useState("24");
  const [busy, setBusy] = useState(false);

  const revoke = async () => {
    if (!revoking) return;
    setBusy(true);
    try {
      await api.post(`/projects/${project.id}/api-keys/${revoking.id}/revoke`);
      toast("success", `Key ${revoking.prefix} revoked; requests using it now get 401`);
      await keys.mutate();
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  };

  const rotate = async () => {
    if (!rotating) return;
    setBusy(true);
    try {
      const created = await api.post<ApiKeyCreated>(`/projects/${project.id}/api-keys/${rotating.id}/rotate`, {
        grace_period_hours: Number(grace),
      });
      setSecret({ value: created.secret, name: created.key.name });
      await keys.mutate();
    } catch (e) {
      toast("error", errorMessage(e));
    } finally {
      setBusy(false);
      setRotating(null);
    }
  };

  return (
    <>
      <PageHeader title="Project & API keys" description="Projects isolate jobs, queues, schedules and credentials. API keys belong to exactly one project." />

      <div className="grid gap-4 xl:grid-cols-[2fr_3fr]">
        <Panel
          title="This project"
          actions={
            <Button size="sm" variant="ghost" icon={<IconPlus />} onClick={() => setNewProject((v) => !v)}>
              New project
            </Button>
          }
        >
          <KeyValue
            items={[
              ["Name", project.name],
              ["Slug", <Mono key="s">{project.slug}</Mono>],
              [
                "Project ID",
                <span key="id" className="flex flex-wrap items-center gap-1">
                  <Mono className="select-all">{project.id}</Mono>
                  <CopyButton value={project.id} />
                </span>,
              ],
              ["Your role", project.role ?? "—"],
              ["Created", <Time key="c" iso={project.created_at} mode="absolute" />],
            ]}
          />
          {projects.length > 1 ? (
            <div className="mt-4 border-t border-line pt-3">
              <p className="mb-1.5 text-xs font-medium text-ink-2">Your projects</p>
              <ul className="flex flex-col gap-1 text-[13px]">
                {projects.map((p) => (
                  <li key={p.id} className="flex items-center justify-between">
                    <span>
                      {p.name} <span className="text-xs text-muted">({p.role})</span>
                    </span>
                    {p.id === project.id ? (
                      <span className="text-xs text-muted">current</span>
                    ) : (
                      <Button size="sm" variant="ghost" onClick={() => selectProject(p.id)}>
                        Switch
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {newProject ? (
            <div className="mt-4 border-t border-line pt-3">
              <CreateProjectForm
                onCreated={async (p) => {
                  await refreshProjects();
                  selectProject(p.id);
                  setNewProject(false);
                  toast("success", `Project ${p.name} created; you are its owner`);
                }}
              />
            </div>
          ) : null}
        </Panel>

        <div className="flex flex-col gap-3">
          {secret ? <SecretReveal secret={secret.value} name={secret.name} onDone={() => setSecret(null)} /> : null}
          <Panel
            title="API keys"
            description="Send as Authorization: Bearer <key>. Only a keyed hash is stored."
            bodyClassName="overflow-x-auto"
            actions={
              isOwner ? (
                <Button size="sm" variant="primary" icon={<IconPlus />} onClick={() => setCreating(true)}>
                  Create key
                </Button>
              ) : null
            }
          >
            {!isOwner ? (
              <p className="px-4 py-6 text-xs text-muted">Only project owners can view and manage API keys.</p>
            ) : keys.error ? (
              <ErrorState error={keys.error} onRetry={() => keys.mutate()} />
            ) : (
              <>
                <table className="w-full min-w-[640px] text-[13px]">
                  <thead>
                    <tr>
                      <Th>Key</Th>
                      <Th>Scopes</Th>
                      <Th>Status</Th>
                      <Th className="text-right">Last used</Th>
                      <Th className="text-right">Actions</Th>
                    </tr>
                  </thead>
                  {!keys.data ? (
                    <SkeletonRows rows={2} cols={5} />
                  ) : (
                    <tbody>
                      {keys.data.map((k) => (
                        <tr key={k.id} className="border-b border-line align-top last:border-0">
                          <Td>
                            <p className="font-medium">{k.name}</p>
                            <Mono className="text-muted">qf_{k.prefix}_…</Mono>
                          </Td>
                          <Td className="font-mono text-[11px] text-ink-2">{k.scopes.join(" ")}</Td>
                          <Td>
                            <StateBadge state={k.status} />
                            {k.expires_at && k.status !== "revoked" ? (
                              <p className="mt-0.5 text-[11px] text-muted">
                                {k.status === "expired" ? "expired" : "expires"} <Time iso={k.expires_at} />
                              </p>
                            ) : null}
                          </Td>
                          <Td className="text-right text-ink-2">
                            <Time iso={k.last_used_at} />
                          </Td>
                          <Td className="text-right whitespace-nowrap">
                            {k.status === "active" || k.status === "expiring" ? (
                              <>
                                <Button size="sm" variant="ghost" onClick={() => setRotating(k)}>
                                  Rotate
                                </Button>
                                <Button size="sm" variant="ghost" onClick={() => setRevoking(k)}>
                                  Revoke
                                </Button>
                              </>
                            ) : null}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  )}
                </table>
                {keys.data && keys.data.length === 0 ? (
                  <EmptyState title="No API keys">Create a key to submit jobs from your services.</EmptyState>
                ) : null}
              </>
            )}
          </Panel>
        </div>
      </div>

      <CreateKeyDialog
        projectId={project.id}
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(k) => {
          setSecret({ value: k.secret, name: k.key.name });
          void keys.mutate();
        }}
      />
      <ConfirmDialog
        open={revoking !== null}
        title={`Revoke ${revoking?.name ?? ""}?`}
        confirmLabel="Revoke key"
        danger
        busy={busy}
        onClose={() => setRevoking(null)}
        onConfirm={revoke}
        body={<p>Requests using this key are rejected immediately. This cannot be undone.</p>}
      />
      <ConfirmDialog
        open={rotating !== null}
        title={`Rotate ${rotating?.name ?? ""}`}
        confirmLabel="Rotate key"
        busy={busy}
        onClose={() => setRotating(null)}
        onConfirm={rotate}
        body={
          <div className="flex flex-col gap-3">
            <p>A replacement key with the same name and scopes is created and shown once. The current key keeps working during the grace period.</p>
            <Field label="Grace period" htmlFor="grace">
              <Select id="grace" value={grace} onChange={(e) => setGrace(e.target.value)}>
                <option value="0">None: revoke the old key now</option>
                <option value="1">1 hour</option>
                <option value="24">24 hours</option>
                <option value="168">7 days</option>
              </Select>
            </Field>
          </div>
        }
      />
    </>
  );
}

"use client";

import { useState, type FormEvent } from "react";

import { Button, Field, Input } from "@/components/ui";
import { ApiError, api, type Project } from "@/lib/api";

export function CreateProjectForm({ onCreated, compact }: { onCreated: (p: Project) => void; compact?: boolean }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const derived = slug || name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.post<Project>("/projects", { name, slug: derived }));
      setName("");
      setSlug("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create project");
      if (err instanceof ApiError) setFields(err.fieldErrors());
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className={compact ? "flex flex-col gap-3 sm:flex-row sm:items-end" : "flex flex-col gap-3"}>
      <Field label="Project name" htmlFor="pname" error={fields.name} className="flex-1">
        <Input id="pname" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
      </Field>
      <Field label="Slug" htmlFor="pslug" error={fields.slug} hint={compact ? undefined : "Lowercase letters, digits and dashes."} className="flex-1">
        <Input id="pslug" value={derived} onChange={(e) => setSlug(e.target.value)} required pattern="[a-z0-9][a-z0-9-]{1,62}" />
      </Field>
      {error ? (
        <p role="alert" className="text-xs text-bad">
          {error}
        </p>
      ) : null}
      <Button type="submit" variant="primary" busy={busy} disabled={!name || !derived}>
        Create project
      </Button>
    </form>
  );
}

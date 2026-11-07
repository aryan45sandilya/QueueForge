"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button, Dialog, Field, Input, KeyValue, PageHeader, Panel, Time, errorMessage, useToast } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useSession } from "@/lib/session";

function ChangePassword() {
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const mismatch = confirm.length > 0 && confirm !== next;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFields({});
    try {
      await api.post("/auth/change-password", { current_password: current, new_password: next });
      toast("success", "Password changed. Other devices have been signed out.");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      if (err instanceof ApiError && err.code === "wrong_password") setFields({ current_password: err.message });
      else if (err instanceof ApiError && err.status === 422) setFields(err.fieldErrors());
      else toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="flex max-w-sm flex-col gap-3" noValidate>
      <Field label="Current password" htmlFor="cur" error={fields.current_password}>
        <Input id="cur" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} aria-invalid={!!fields.current_password} />
      </Field>
      <Field label="New password" htmlFor="new" hint="At least 10 characters." error={fields.new_password}>
        <Input id="new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} aria-invalid={!!fields.new_password} />
      </Field>
      <Field label="Confirm new password" htmlFor="confirm" error={mismatch ? "Passwords don't match" : undefined}>
        <Input id="confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-invalid={mismatch} />
      </Field>
      <div>
        <Button type="submit" variant="primary" busy={busy} disabled={!current || next.length < 10 || next !== confirm}>
          Change password
        </Button>
      </div>
    </form>
  );
}

function DeleteAccount() {
  const { projects } = useSession();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const owned = projects.filter((p) => p.role === "owner");

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/delete-account", { password });
      router.replace("/welcome");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <>
      <p className="max-w-xl text-[13px] text-ink-2">
        Deleting your account removes it immediately. Projects you own alone are deleted with all their jobs, schedules, queues and
        API keys. Projects that have another owner are kept. This can&apos;t be undone.
      </p>
      <Button variant="danger" className="mt-3" onClick={() => setOpen(true)}>
        Delete account
      </Button>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="Delete your account?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="danger" onClick={remove} busy={busy} disabled={!password || typed !== "delete"}>
              Delete permanently
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <p>
            This deletes your account
            {owned.length ? ` and up to ${owned.length} project${owned.length === 1 ? "" : "s"} you own (${owned.map((p) => p.name).join(", ")})` : ""}.
          </p>
          <Field label="Password" htmlFor="del-pw">
            <Input id="del-pw" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Field label='Type "delete" to confirm' htmlFor="del-confirm">
            <Input id="del-confirm" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </Field>
          {error ? (
            <p role="alert" className="text-xs text-bad">
              {error}
            </p>
          ) : null}
        </div>
      </Dialog>
    </>
  );
}

export default function AccountPage() {
  const { me } = useSession();
  return (
    <>
      <PageHeader title="Account" description="Your sign-in details and account settings." />
      <div className="flex flex-col gap-4">
        <Panel title="Profile">
          <KeyValue
            items={[
              ["Name", me.user.display_name],
              ["Email", me.user.email],
              ["Email confirmed", me.user.email_verified_at ? <Time key="v" iso={me.user.email_verified_at} mode="absolute" /> : "Not confirmed"],
              ["Member since", <Time key="c" iso={me.user.created_at} mode="absolute" />],
            ]}
          />
        </Panel>
        <Panel title="Change password" description="Other devices are signed out; this one stays signed in.">
          <ChangePassword />
        </Panel>
        <Panel title="Delete account" className="border-bad/40">
          <DeleteAccount />
        </Panel>
      </div>
    </>
  );
}

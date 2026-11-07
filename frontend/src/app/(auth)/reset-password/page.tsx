"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { Button, Field, Input } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

import { AuthLink, AuthShell, Notice } from "../AuthShell";

function Reset() {
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mismatch = confirm.length > 0 && confirm !== password;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== confirm) return;
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/reset-password", { token, new_password: password });
      setDone(true);
    } catch (err) {
      const fields = err instanceof ApiError ? err.fieldErrors() : {};
      setError(fields.new_password ?? (err instanceof Error ? err.message : "Reset failed"));
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <AuthShell title="Link not valid" footer={<AuthLink href="/forgot-password">Request a new link</AuthLink>}>
        <Notice tone="error">This reset link is missing its token.</Notice>
      </AuthShell>
    );
  }
  if (done) {
    return (
      <AuthShell title="Password updated" subtitle="You've been signed out everywhere else.">
        <Notice tone="success">Sign in with your new password.</Notice>
        <p className="mt-4 text-[13px]">
          <AuthLink href="/login">Sign in</AuthLink>
        </p>
      </AuthShell>
    );
  }
  return (
    <AuthShell title="Choose a new password" subtitle="At least 10 characters."
      footer={<AuthLink href="/forgot-password">Request a new link</AuthLink>}>
      <form onSubmit={submit} noValidate>
        <div className="flex flex-col gap-3">
          <Field label="New password" htmlFor="new-password">
            <Input id="new-password" type="password" autoComplete="new-password" value={password}
              onChange={(e) => setPassword(e.target.value)} minLength={10} required />
          </Field>
          <Field label="Confirm new password" htmlFor="confirm-password" error={mismatch ? "Passwords don't match" : undefined}>
            <Input id="confirm-password" type="password" autoComplete="new-password" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} aria-invalid={mismatch} required />
          </Field>
        </div>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <Button type="submit" variant="primary" className="mt-4 w-full" busy={busy}
          disabled={password.length < 10 || password !== confirm}>
          Set new password
        </Button>
      </form>
    </AuthShell>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <Reset />
    </Suspense>
  );
}

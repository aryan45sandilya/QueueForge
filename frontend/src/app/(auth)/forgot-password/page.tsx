"use client";

import { useState, type FormEvent } from "react";

import { Button, Field, Input } from "@/components/ui";
import { api } from "@/lib/api";

import { AuthLink, AuthShell, Notice } from "../AuthShell";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ message: string }>("/auth/forgot-password", { email });
      setDone(r.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell title="Reset your password" subtitle="We'll email you a link that's valid for one hour."
      footer={<>Remembered it? <AuthLink href="/login">Sign in</AuthLink></>}>
      {done ? (
        <Notice tone="success">{done}</Notice>
      ) : (
        <form onSubmit={submit} noValidate>
          <Field label="Email" htmlFor="email">
            <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          {error ? <Notice tone="error">{error}</Notice> : null}
          <Button type="submit" variant="primary" className="mt-4 w-full" busy={busy} disabled={!email}>
            Send reset link
          </Button>
        </form>
      )}
    </AuthShell>
  );
}

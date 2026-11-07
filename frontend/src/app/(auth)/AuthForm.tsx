"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import useSWR from "swr";

import { Button, Field, Input } from "@/components/ui";
import { ApiError, api, type InstanceInfo, type RegisterOut } from "@/lib/api";

import { AuthLink, AuthShell, Notice } from "./AuthShell";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const instance = useSWR<InstanceInfo>("/instance", { shouldRetryOnError: false });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [unverified, setUnverified] = useState(false);
  const [checkInbox, setCheckInbox] = useState(false);
  const [resent, setResent] = useState<string | null>(null);

  const isLogin = mode === "login";
  const closed = !isLogin && instance.data?.registration_open === false;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFields({});
    setUnverified(false);
    setResent(null);
    try {
      if (!isLogin) {
        const created = await api.post<RegisterOut>("/auth/register", { email, password, display_name: name });
        if (created?.verification_required) {
          setCheckInbox(true);
          return;
        }
      }
      await api.post("/auth/login", { email, password });
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.code === "email_not_verified") setUnverified(true);
        setError(err.status === 422 ? "Check the highlighted fields." : err.message);
        setFields(err.fieldErrors());
      } else {
        setError("Unexpected error");
      }
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    try {
      const r = await api.post<{ message: string }>("/auth/resend-verification", { email });
      setResent(r.message);
    } catch (err) {
      setResent(err instanceof Error ? err.message : "Could not send the email");
    }
  };

  if (checkInbox) {
    return (
      <AuthShell title="Check your inbox" subtitle={`We sent a confirmation link to ${email}.`}
        footer={<>Confirmed already? <AuthLink href="/login">Sign in</AuthLink></>}>
        <p className="text-[13px] text-ink-2">
          Open the link in that email to activate your account. It expires in 48 hours. Nothing arrived after a few minutes?
          Check your spam folder or send it again.
        </p>
        <Button className="mt-4 w-full" onClick={resend}>
          Send the link again
        </Button>
        {resent ? <Notice>{resent}</Notice> : null}
      </AuthShell>
    );
  }

  if (closed) {
    return (
      <AuthShell title="Registration is closed" subtitle="This QueueForge instance isn't accepting new accounts."
        footer={<>Have an account? <AuthLink href="/login">Sign in</AuthLink></>}>
        <p className="text-[13px] text-ink-2">Ask the person who runs it for an account, or host your own copy.</p>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={isLogin ? "Sign in" : "Create an account"}
      subtitle={isLogin ? "Use your QueueForge account to open the dashboard." : "Free. Passwords need at least 10 characters."}
      footer={
        isLogin ? (
          <>
            No account? <AuthLink href="/register">Create one</AuthLink>
          </>
        ) : (
          <>
            Already registered? <AuthLink href="/login">Sign in</AuthLink>
          </>
        )
      }
    >
      <form onSubmit={submit} noValidate>
        <div className="flex flex-col gap-3">
          {!isLogin ? (
            <Field label="Name" htmlFor="name" error={fields.display_name}>
              <Input id="name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} required aria-invalid={!!fields.display_name} />
            </Field>
          ) : null}
          <Field label="Email" htmlFor="email" error={fields.email}>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              aria-invalid={!!fields.email}
            />
          </Field>
          <Field label="Password" htmlFor="password" error={fields.password}>
            <Input
              id="password"
              type="password"
              autoComplete={isLogin ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={isLogin ? 1 : 10}
              aria-invalid={!!fields.password}
            />
          </Field>
        </div>
        {isLogin ? (
          <p className="mt-2 text-right text-xs">
            <AuthLink href="/forgot-password">Forgot password?</AuthLink>
          </p>
        ) : null}
        {error ? <Notice tone="error">{error}</Notice> : null}
        {unverified ? (
          <div className="mt-2 text-xs">
            <button type="button" className="text-ink underline decoration-line-strong underline-offset-2 transition-colors hover:decoration-copper" onClick={resend}>
              Email me a new confirmation link
            </button>
            {resent ? <Notice>{resent}</Notice> : null}
          </div>
        ) : null}
        <Button type="submit" variant="primary" className="mt-4 w-full" busy={busy}>
          {isLogin ? "Sign in" : "Create account"}
        </Button>
      </form>
    </AuthShell>
  );
}

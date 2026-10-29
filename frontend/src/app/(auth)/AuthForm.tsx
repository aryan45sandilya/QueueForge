"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Logo } from "@/components/icons";
import { Button, Field, Input } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFields({});
    try {
      if (mode === "register") {
        await api.post("/auth/register", { email, password, display_name: name });
      }
      await api.post("/auth/login", { email, password });
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.status === 422 ? "Check the highlighted fields." : err.message);
        setFields(err.fieldErrors());
      } else {
        setError("Unexpected error");
      }
    } finally {
      setBusy(false);
    }
  };

  const isLogin = mode === "login";
  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-[360px]">
        <div className="mb-6 flex items-center gap-2">
          <Logo className="text-ink" />
          <span className="font-serif text-lg font-medium">QueueForge</span>
        </div>
        <form onSubmit={submit} className="rounded-[4px] border border-line bg-surface p-5" noValidate>
          <h1 className="text-base font-semibold">{isLogin ? "Sign in" : "Create an account"}</h1>
          <p className="mt-1 mb-4 text-xs text-muted">
            {isLogin ? "Use your QueueForge account to open the dashboard." : "Passwords need at least 10 characters."}
          </p>
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
          {error ? (
            <p role="alert" className="mt-3 border-l-2 border-bad bg-bad-tint/60 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          ) : null}
          <Button type="submit" variant="primary" className="mt-4 w-full" busy={busy}>
            {isLogin ? "Sign in" : "Create account"}
          </Button>
        </form>
        <p className="mt-4 text-center text-xs text-muted">
          {isLogin ? (
            <>
              No account? <Link className="text-copper-strong hover:underline" href="/register">Create one</Link>
              <br />
              <span className="mt-2 inline-block">
                Local demo: run <code className="font-mono">queueforge seed</code> and sign in as demo@queueforge.dev.
              </span>
            </>
          ) : (
            <>
              Already registered? <Link className="text-copper-strong hover:underline" href="/login">Sign in</Link>
            </>
          )}
        </p>
      </div>
    </main>
  );
}

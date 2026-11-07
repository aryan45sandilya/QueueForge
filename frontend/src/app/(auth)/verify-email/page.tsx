"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Loading } from "@/components/ui";
import { api } from "@/lib/api";

import { AuthLink, AuthShell, Notice } from "../AuthShell";

function Verify() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<"working" | "done" | "failed">(token ? "working" : "failed");
  const [message, setMessage] = useState(token ? "" : "This link is missing its token.");
  const sent = useRef(false);

  useEffect(() => {
    if (!token || sent.current) return;
    sent.current = true; // the token is single-use; never submit it twice (React strict mode)
    api
      .post("/auth/verify-email", { token })
      .then(() => setState("done"))
      .catch((err: unknown) => {
        setState("failed");
        setMessage(err instanceof Error ? err.message : "Verification failed");
      });
  }, [token]);

  if (state === "working") return <Loading label="Confirming your email" />;
  return state === "done" ? (
    <AuthShell title="Email confirmed" subtitle="Your account is active.">
      <Notice tone="success">You can sign in now.</Notice>
      <p className="mt-4 text-[13px]">
        <AuthLink href="/login">Sign in to QueueForge</AuthLink>
      </p>
    </AuthShell>
  ) : (
    <AuthShell title="Link not valid" footer={<AuthLink href="/login">Back to sign in</AuthLink>}>
      <Notice tone="error">{message}</Notice>
      <p className="mt-3 text-[13px] text-ink-2">
        Links expire after 48 hours and work once. Sign in and choose &ldquo;Email me a new confirmation link&rdquo; to get a fresh one.
      </p>
    </AuthShell>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense>
      <Verify />
    </Suspense>
  );
}

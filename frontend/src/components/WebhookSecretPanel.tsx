"use client";

import { useState } from "react";

import { api, type WebhookSecret } from "@/lib/api";

import { Button, ConfirmDialog, CopyButton, Panel, errorMessage, useToast } from "./ui";

/** Shows (on request) and rotates the secret that signs this project's http.request calls. */
export function WebhookSecretPanel({ projectId }: { projectId: string }) {
  const toast = useToast();
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  const load = async (rotate: boolean) => {
    setBusy(true);
    try {
      const path = `/projects/${projectId}/webhook-secret${rotate ? "/rotate" : ""}`;
      const r = rotate ? await api.post<WebhookSecret>(path) : await api.get<WebhookSecret>(path);
      setSecret(r.secret);
      if (rotate) toast("success", "Webhook secret rotated. Update your endpoint to the new value.");
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  return (
    <Panel
      title="Webhook signing secret"
      description="http.request calls carry X-QueueForge-Signature, an HMAC of the body made with this secret."
    >
      {secret ? (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <code data-testid="webhook-secret" className="min-w-0 flex-1 overflow-x-auto rounded-[3px] border border-line bg-sunken px-2.5 py-1.5 font-mono text-[12px] whitespace-nowrap select-all">
            {secret}
          </code>
          <div className="flex gap-1">
            <CopyButton value={secret} />
            <Button size="sm" variant="ghost" onClick={() => setSecret(null)}>
              Hide
            </Button>
          </div>
        </div>
      ) : (
        <Button size="sm" onClick={() => void load(false)} busy={busy}>
          Reveal secret
        </Button>
      )}
      <p className="mt-3 text-xs text-muted">
        Leaked or shared by mistake?{" "}
        <button className="text-copper-strong underline-offset-2 hover:underline" onClick={() => setConfirm(true)}>
          Rotate it
        </button>
        . The Developer docs show how to verify signatures.
      </p>
      <ConfirmDialog
        open={confirm}
        title="Rotate the webhook secret?"
        confirmLabel="Rotate secret"
        busy={busy}
        onClose={() => setConfirm(false)}
        onConfirm={() => void load(true)}
        body={<p>Calls made from now on are signed with a new secret. Endpoints still checking the old one will reject them until you update them.</p>}
      />
    </Panel>
  );
}

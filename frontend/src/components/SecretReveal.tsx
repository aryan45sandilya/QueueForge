"use client";

import { useState } from "react";

import { Button, CopyButton } from "./ui";

/**
 * Shows a newly created API key exactly once. The secret lives only in this component's state;
 * after "I've stored it" it is discarded and cannot be retrieved again (the server keeps only a hash).
 */
export function SecretReveal({ secret, name, onDone }: { secret: string; name: string; onDone: () => void }) {
  const [visible, setVisible] = useState(false);
  return (
    <div role="region" aria-label="New API key" className="qf-fade rounded-[4px] border border-ochre bg-ochre-tint/60 p-4">
      <p className="text-[13px] font-semibold">Copy the key for “{name}” now</p>
      <p className="mt-0.5 text-xs text-ink-2">
        This is the only time QueueForge shows it. Store it in your secret manager; if it is lost, rotate the key.
      </p>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
        <code
          data-testid="secret-value"
          className="min-w-0 flex-1 overflow-x-auto rounded-[3px] border border-line-strong bg-surface px-2.5 py-1.5 font-mono text-[12px] whitespace-nowrap select-all"
        >
          {visible ? secret : `${secret.slice(0, 16)}${"•".repeat(24)}`}
        </code>
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" onClick={() => setVisible((v) => !v)} aria-pressed={visible}>
            {visible ? "Hide" : "Reveal"}
          </Button>
          <CopyButton value={secret} label="Copy key" />
          <Button size="sm" variant="primary" onClick={onDone}>
            I&apos;ve stored it
          </Button>
        </div>
      </div>
    </div>
  );
}

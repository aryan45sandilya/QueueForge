"use client";

import Link from "next/link";
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { ApiError } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";

import { IconCheck, IconClose, IconCopy, IconInfo } from "./icons";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// --- buttons ------------------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-btn text-btn-ink border border-btn-edge hover:bg-btn-hover",
  secondary: "bg-surface text-ink border border-line-strong hover:border-ink-2",
  ghost: "text-ink-2 hover:bg-sunken hover:text-ink border border-transparent",
  danger: "bg-surface text-bad border border-line-strong hover:border-bad hover:bg-bad-tint",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  busy?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", busy, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-[2px] font-medium whitespace-nowrap",
        "transition-[background-color,border-color,color,opacity,transform] duration-150 active:translate-y-px",
        "disabled:cursor-not-allowed disabled:opacity-45 disabled:active:translate-y-0",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3.5 text-[13px]",
        VARIANTS[variant],
        className,
      )}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Spinner /> : icon}
      {children}
    </button>
  );
});

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cx("inline-block size-3 animate-spin rounded-full border-[1.5px] border-current border-r-transparent", className)}
    />
  );
}

// --- form fields --------------------------------------------------------------------------------

const CONTROL =
  "w-full rounded-[2px] border border-line-strong bg-surface px-2.5 text-[13px] text-ink placeholder:text-muted " +
  "transition-[border-color,box-shadow] duration-150 hover:border-ink-2/50 " +
  "focus:border-copper focus:shadow-[0_0_0_3px_var(--qf-copper-tint)] focus:outline-none " +
  "disabled:opacity-60 aria-[invalid=true]:border-bad";

export function Field({
  label,
  hint,
  error,
  children,
  className,
  htmlFor,
}: {
  label: string;
  hint?: ReactNode;
  error?: string;
  children: ReactNode;
  className?: string;
  htmlFor?: string;
}) {
  return (
    <div className={cx("flex flex-col gap-1", className)}>
      <label htmlFor={htmlFor} className="text-[12px] font-medium text-ink-2">
        {label}
      </label>
      {children}
      {error ? (
        <p role="alert" className="text-xs text-bad">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted">{hint}</p>
      ) : null}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...props },
  ref,
) {
  return <input ref={ref} className={cx(CONTROL, "h-8", className)} {...props} />;
});

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cx(CONTROL, "h-8 pr-7", className)} {...props}>
      {children}
    </select>
  );
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(CONTROL, "py-2 font-mono text-xs leading-relaxed", className)} {...props} />;
}

export function Checkbox({ label, ...props }: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-[13px]">
      <input type="checkbox" className="size-3.5 accent-[var(--qf-copper)]" {...props} />
      {label}
    </label>
  );
}

// --- status -------------------------------------------------------------------------------------

// Indicator lamps: a coloured pip beside a plain label. The label carries the meaning, so the
// state is never conveyed by colour alone.
const LAMP: Record<string, string> = {
  scheduled: "border border-idle bg-transparent",
  queued: "bg-sage",
  running: "bg-run qf-pulse",
  retrying: "bg-ochre",
  succeeded: "bg-ok",
  failed: "bg-bad",
  cancelled: "border border-idle bg-transparent",
  healthy: "bg-ok",
  active: "bg-ok",
  enabled: "bg-ok",
  draining: "bg-ochre",
  stale: "bg-ochre",
  expiring: "bg-ochre",
  paused: "bg-ochre",
  lost: "bg-bad",
  revoked: "border border-idle bg-transparent",
  expired: "border border-idle bg-transparent",
  stopped: "bg-idle",
  disabled: "border border-idle bg-transparent",
  interrupted: "bg-ochre",
  timed_out: "bg-bad",
};

/** Status shown as an indicator lamp plus its name. */
export function StateBadge({ state, className }: { state: string; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 font-mono text-[11.5px] leading-none text-ink-2", className)}>
      <span aria-hidden="true" className={cx("inline-block size-[7px] shrink-0 rounded-[1px]", LAMP[state] ?? "bg-idle")} />
      {state.replace("_", " ")}
    </span>
  );
}

// --- layout pieces ------------------------------------------------------------------------------

export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-[22px] leading-tight font-semibold tracking-[-0.02em]">{title}</h1>
        {description ? <p className="mt-1.5 max-w-[68ch] text-[13px] leading-relaxed text-muted">{description}</p> : null}
        {children}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Panel({
  title,
  actions,
  children,
  className,
  bodyClassName,
  description,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  description?: ReactNode;
}) {
  return (
    <section className={cx("min-w-0 rounded-[3px] border border-line bg-surface", className)}>
      {title || actions ? (
        <div className="flex min-h-11 items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="min-w-0">
            {title ? <h2 className="text-[13.5px] font-semibold tracking-[-0.005em]">{title}</h2> : null}
            {description ? <p className="text-xs text-muted">{description}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={bodyClassName ?? "p-4"}>{children}</div>
    </section>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="text-[13px] font-medium text-ink-2">{title}</p>
      {children ? <div className="mx-auto mt-1 max-w-md text-xs text-muted">{children}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div role="alert" className="flex items-start gap-3 border-l-2 border-bad bg-bad-tint/60 px-4 py-3 text-[13px]">
      <div className="min-w-0 flex-1">
        <p className="font-medium text-bad">Couldn&apos;t load this data</p>
        <p className="mt-0.5 text-ink-2">{message}</p>
        {requestId ? <p className="mt-1 font-mono text-[11px] text-muted">request {requestId}</p> : null}
      </div>
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function SkeletonRows({ rows = 5, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <tbody aria-hidden="true">
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r} className="border-b border-line">
          {Array.from({ length: cols }, (_, c) => (
            <td key={c} className="px-3 py-2.5">
              <span className="block h-3 animate-pulse rounded-sm bg-sunken" style={{ width: `${40 + ((r * 7 + c * 13) % 50)}%` }} />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 px-4 py-8 text-xs text-muted">
      <Spinner /> {label}…
    </div>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,max-content)_1fr] gap-x-4 gap-y-1.5 text-[13px]">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Time({ iso, mode = "relative" }: { iso: string | null | undefined; mode?: "relative" | "absolute" }) {
  const [, force] = useState(0);
  useEffect(() => {
    if (mode !== "relative" || !iso) return;
    const t = setInterval(() => force((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, [iso, mode]);
  if (!iso) return <span className="text-muted">—</span>;
  return (
    <time dateTime={iso} title={absoluteTime(iso)} className="whitespace-nowrap">
      {mode === "relative" ? relativeTime(iso) : absoluteTime(iso)}
    </time>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("font-mono text-[12px]", className)}>{children}</span>;
}

export function InfoHint({ text }: { text: string }) {
  return (
    <span className="inline-flex cursor-help align-middle text-muted" title={text} aria-label={text} role="img">
      <IconInfo width={13} height={13} />
    </span>
  );
}

export function TextLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return (
    <Link href={href} className={cx("text-ink underline decoration-line-strong underline-offset-2 transition-colors hover:decoration-copper", className)}>
      {children}
    </Link>
  );
}

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <Button size="sm" variant="ghost" onClick={copy} icon={copied ? <IconCheck /> : <IconCopy />} aria-live="polite">
      {copied ? "Copied" : label}
    </Button>
  );
}

export function CodeBlock({ children, className, label }: { children: string; className?: string; label?: string }) {
  return (
    <div className={cx("relative rounded-[3px] border border-line bg-sunken", className)}>
      <div className="absolute top-1 right-1">
        <CopyButton value={children} label={label ?? "Copy"} />
      </div>
      <pre className="max-h-[480px] overflow-auto p-3 pr-20 font-mono text-[12px] leading-relaxed whitespace-pre">
        {children}
      </pre>
    </div>
  );
}

// --- dialog -------------------------------------------------------------------------------------

export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) {
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
    } else if (!open && el.open) {
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
    }
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      className={cx(
        "m-auto w-[calc(100%-32px)] rounded-[4px] border border-line-strong bg-surface p-0 text-ink shadow-[0_12px_40px_-12px_rgba(20,24,19,0.35)]",
        "backdrop:bg-[rgba(20,24,19,0.35)]",
        wide ? "max-w-2xl" : "max-w-md",
      )}
    >
      {open ? (
        <div className="qf-fade">
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <h2 id={titleId} className="text-sm font-semibold">
              {title}
            </h2>
            <button onClick={onClose} className="rounded p-1 text-muted hover:bg-sunken hover:text-ink" aria-label="Close">
              <IconClose />
            </button>
          </div>
          <div className="max-h-[70vh] overflow-y-auto px-4 py-4 text-[13px]">{children}</div>
          {footer ? <div className="flex justify-end gap-2 border-t border-line px-4 py-3">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  );
}

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} busy={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {body}
    </Dialog>
  );
}

// --- toasts -------------------------------------------------------------------------------------

interface Toast {
  id: number;
  kind: "success" | "error" | "info";
  message: string;
}

const ToastContext = createContext<(kind: Toast["kind"], message: string) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const push = useCallback((kind: Toast["kind"], message: string) => {
    const id = nextId.current++;
    setToasts((t) => [...t.slice(-3), { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 8000 : 4000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[min(380px,calc(100%-32px))] flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            className={cx(
              "qf-fade pointer-events-auto flex items-start gap-2 rounded-[4px] border bg-surface px-3 py-2.5 text-[13px] shadow-[0_8px_24px_-12px_rgba(20,24,19,0.4)]",
              t.kind === "error" ? "border-bad/50" : t.kind === "success" ? "border-ok/40" : "border-line-strong",
            )}
          >
            <span
              aria-hidden="true"
              className={cx("mt-1.5 size-1.5 shrink-0 rounded-full", t.kind === "error" ? "bg-bad" : t.kind === "success" ? "bg-ok" : "bg-sage")}
            />
            <span className="flex-1">{t.message}</span>
            <button
              className="pointer-events-auto text-muted hover:text-ink"
              aria-label="Dismiss"
              onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))}
            >
              <IconClose width={12} height={12} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Unexpected error";
}

// --- table helpers ------------------------------------------------------------------------------

export function Th({ children, className, ...rest }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cx("border-b border-line bg-paper/40 px-3 py-2 text-left font-mono text-[10.5px] font-normal tracking-[0.04em] text-muted lowercase", className)}
      {...rest}
    >
      {children}
    </th>
  );
}

export function Td({ children, className, ...rest }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cx("px-3 py-2 align-middle", className)} {...rest}>
      {children}
    </td>
  );
}

export function Pagination({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + limit, total);
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2 text-xs text-muted">
      <span className="num">
        {start}–{end} of {total.toLocaleString()}
      </span>
      <div className="flex gap-1">
        <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        <Button size="sm" variant="ghost" disabled={end >= total} onClick={() => onChange(offset + limit)}>
          Next
        </Button>
      </div>
    </div>
  );
}

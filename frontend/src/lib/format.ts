/** Formatting helpers. Pure functions; all times are rendered in the viewer's locale. */

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function formatNumber(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function formatCompact(n: number): string {
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });
  return formatNumber(n, n < 10 && n % 1 !== 0 ? 1 : 0);
}

export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio === null || ratio === undefined) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}

/** Milliseconds to a compact human duration: 340 ms, 2.4 s, 3m 05s, 1h 12m. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 59.5) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const whole = Math.round(s); // round once, then split, so 2579.6 s is 43m 00s, not 42m 60s
  const m = Math.floor(whole / 60);
  if (m < 60) return `${m}m ${String(whole % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function formatSeconds(s: number | null | undefined): string {
  return s === null || s === undefined ? "—" : formatDuration(s * 1000);
}

const RTF = typeof Intl !== "undefined" ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;

export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const diff = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(diff);
  if (abs < 5) return "just now";
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["second", 60],
    ["minute", 3600],
    ["hour", 86400],
    ["day", 86400 * 30],
  ];
  let divisor = 1;
  for (const [unit, limit] of units) {
    if (abs < limit) return RTF ? RTF.format(Math.round(diff / divisor), unit) : `${Math.round(abs / divisor)} ${unit}s`;
    divisor = limit;
  }
  return new Date(iso).toLocaleDateString();
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function durationBetween(start: string | null | undefined, end: string | null | undefined): number | null {
  if (!start || !end) return null;
  return new Date(end).getTime() - new Date(start).getTime();
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

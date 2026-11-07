"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { cx } from "./ui";

/**
 * Small, dependency-free time-series chart: stacked columns and/or lines on ONE y-axis.
 * Marks follow fixed specs (<=24px columns with a 4px rounded cap, 2px lines, 2px surface gaps,
 * hairline grid). Hover shows a crosshair and a tooltip listing every series for the bucket,
 * and a table view exposes the same numbers without relying on colour.
 */

export interface Series {
  key: string;
  label: string;
  color: string; // CSS colour or var()
  kind: "bar" | "line";
}

export interface Point {
  t: string; // ISO bucket start
  values: Record<string, number | null>;
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T | null>(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setWidth(Math.max(240, Math.floor(w)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  const f = value / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

function timeLabel(iso: string, bucketSeconds: number): string {
  const d = new Date(iso);
  if (bucketSeconds >= 3600 * 3) return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function Legend({ series }: { series: Series[] }) {
  return (
    <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-2">
      {series.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          {s.kind === "bar" ? (
            <span aria-hidden="true" className="inline-block size-2.5 rounded-[2px]" style={{ background: s.color }} />
          ) : (
            <span aria-hidden="true" className="inline-block h-[2px] w-3.5 rounded" style={{ background: s.color }} />
          )}
          {s.label}
        </li>
      ))}
    </ul>
  );
}

export function TimeSeriesChart({
  points,
  series,
  bucketSeconds,
  unit,
  formatValue,
  height = 180,
  emptyMessage,
  ariaLabel,
}: {
  points: Point[];
  series: Series[];
  bucketSeconds: number;
  unit: string;
  formatValue: (v: number) => string;
  height?: number;
  emptyMessage: ReactNode;
  ariaLabel: string;
}) {
  const [wrapRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);

  const bars = series.filter((s) => s.kind === "bar");
  const lines = series.filter((s) => s.kind === "line");
  const pad = { top: 10, right: 12, bottom: 22, left: 44 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;

  const { max, hasData } = useMemo(() => {
    let m = 0;
    let any = false;
    for (const p of points) {
      const stack = bars.reduce((sum, s) => sum + (p.values[s.key] ?? 0), 0);
      m = Math.max(m, stack);
      for (const s of series) {
        const v = p.values[s.key];
        if (v !== null && v !== undefined && v > 0) any = true;
        if (s.kind === "line" && v !== null && v !== undefined) m = Math.max(m, v);
      }
    }
    return { max: niceMax(m), hasData: any };
  }, [points, series, bars]);

  const n = Math.max(points.length, 1);
  const band = innerW / n;
  const barW = Math.max(1, Math.min(24, band - 2));
  const y = (v: number) => pad.top + innerH - (v / max) * innerH;
  const xCenter = (i: number) => pad.left + band * i + band / 2;
  const ticks = [0, max / 2, max];
  const labelEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(innerW / 90))));

  const linePath = (key: string) => {
    let d = "";
    let pen = false;
    points.forEach((p, i) => {
      const v = p.values[key];
      if (v === null || v === undefined) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${xCenter(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const i = Math.floor((e.clientX - rect.left - pad.left) / band);
    setHover(i >= 0 && i < points.length ? i : null);
  };

  const hovered = hover !== null ? points[hover] : undefined;

  return (
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        {series.length > 1 ? <Legend series={series} /> : <span />}
        <button
          type="button"
          onClick={() => setShowTable((v) => !v)}
          className="text-xs text-muted underline-offset-2 hover:text-ink hover:underline"
          aria-pressed={showTable}
        >
          {showTable ? "Chart view" : "Table view"}
        </button>
      </div>
      {showTable ? (
        <div className="max-h-64 overflow-auto rounded-[3px] border border-line">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-surface">
              <tr>
                <th className="px-2 py-1.5 text-left font-medium text-muted">Bucket start</th>
                {series.map((s) => (
                  <th key={s.key} className="px-2 py-1.5 text-right font-medium text-muted">
                    {s.label} ({unit})
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.t} className="border-t border-line">
                  <td className="px-2 py-1">{new Date(p.t).toLocaleString()}</td>
                  {series.map((s) => (
                    <td key={s.key} className="px-2 py-1 text-right">
                      {p.values[s.key] === null || p.values[s.key] === undefined ? "—" : formatValue(p.values[s.key] as number)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={wrapRef} className="relative w-full min-w-0 overflow-hidden">
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={ariaLabel}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
            className="block touch-none select-none"
          >
            {ticks.map((t) => (
              <g key={t}>
                <line x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} stroke="var(--qf-grid)" strokeWidth={1} />
                <text x={pad.left - 6} y={y(t) + 3.5} textAnchor="end" className="fill-[var(--qf-muted)] text-[10px] num">
                  {formatValue(t)}
                </text>
              </g>
            ))}
            {hover !== null ? (
              <rect x={pad.left + band * hover} y={pad.top} width={band} height={innerH} fill="var(--qf-sunken)" opacity={0.7} />
            ) : null}
            {points.map((p, i) => {
              let base = 0;
              const cx0 = xCenter(i) - barW / 2;
              const visible = bars.filter((s) => (p.values[s.key] ?? 0) > 0);
              return visible.map((s, si) => {
                const v = p.values[s.key] ?? 0;
                const top = y(base + v);
                const bottom = y(base);
                base += v;
                const gap = si > 0 ? 2 : 0; // 2px surface gap between stacked segments
                const h = Math.max(1, bottom - top - gap);
                const isTop = si === visible.length - 1;
                const r = isTop ? Math.min(4, h / 2, barW / 2) : 0;
                const path = isTop
                  ? `M${cx0},${top + h}V${top + r}Q${cx0},${top} ${cx0 + r},${top}H${cx0 + barW - r}Q${cx0 + barW},${top} ${cx0 + barW},${top + r}V${top + h}Z`
                  : `M${cx0},${top}H${cx0 + barW}V${top + h}H${cx0}Z`;
                return <path key={s.key} d={path} fill={s.color} />;
              });
            })}
            {lines.map((s) => (
              <path key={s.key} d={linePath(s.key)} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            ))}
            {lines.map((s) =>
              points.map((p, i) => {
                // A value with no neighbours has no segment to draw; show it as a dot.
                const v = p.values[s.key];
                const prev = points[i - 1]?.values[s.key];
                const next = points[i + 1]?.values[s.key];
                if (v === null || v === undefined || (prev ?? null) !== null || (next ?? null) !== null) return null;
                return <circle key={`${s.key}-${p.t}`} cx={xCenter(i)} cy={y(v)} r={3} fill={s.color} />;
              }),
            )}
            {hover !== null
              ? lines.map((s) => {
                  const v = points[hover]?.values[s.key];
                  if (v === null || v === undefined) return null;
                  return <circle key={s.key} cx={xCenter(hover)} cy={y(v)} r={4} fill={s.color} stroke="var(--qf-surface)" strokeWidth={2} />;
                })
              : null}
            <line x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} stroke="var(--qf-line-strong)" strokeWidth={1} />
            {points.map((p, i) =>
              i % labelEvery === 0 ? (
                <text key={p.t} x={xCenter(i)} y={height - 6} textAnchor="middle" className="fill-[var(--qf-muted)] text-[10px] num">
                  {timeLabel(p.t, bucketSeconds)}
                </text>
              ) : null,
            )}
          </svg>
          {!hasData ? (
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <p className="rounded-[3px] bg-surface/90 px-3 py-1.5 text-xs text-muted">{emptyMessage}</p>
            </div>
          ) : null}
          {hovered && hover !== null ? (
            <div
              className="pointer-events-none absolute top-1 z-10 min-w-40 rounded-[3px] border border-line-strong bg-surface px-2.5 py-2 text-xs shadow-[0_6px_18px_-10px_rgba(20,24,19,0.45)]"
              style={{
                left: Math.min(Math.max(0, xCenter(hover) + 10), width - 170),
              }}
            >
              <p className="mb-1 font-medium">
                {new Date(hovered.t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                <span className="font-normal text-muted"> · {Math.round(bucketSeconds / 60)} min bucket</span>
              </p>
              {series.map((s) => {
                const v = hovered.values[s.key];
                return (
                  <p key={s.key} className="flex items-center justify-between gap-4">
                    <span className="flex items-center gap-1.5 text-ink-2">
                      <span aria-hidden="true" className={cx("inline-block", s.kind === "bar" ? "size-2 rounded-[2px]" : "h-[2px] w-3")} style={{ background: s.color }} />
                      {s.label}
                    </span>
                    <span className="num font-medium">{v === null || v === undefined ? "—" : formatValue(v)}</span>
                  </p>
                );
              })}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

/** A compact metric with label, value, unit and an optional sub-line. */
export function Stat({
  label,
  value,
  unit,
  sub,
  hint,
  tone,
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: ReactNode;
  hint?: string;
  tone?: "bad" | "warn";
}) {
  return (
    <div className="min-w-0 px-4 py-3" title={hint}>
      <p className="qf-label truncate">{label.toLowerCase()}</p>
      <p
        className={cx(
          "mt-2 font-mono text-[26px] leading-none font-medium tracking-[-0.03em] num",
          tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "",
        )}
      >
        {value}
        {unit ? <span className="ml-1 font-sans text-xs tracking-normal text-muted">{unit}</span> : null}
      </p>
      {sub ? <p className="mt-1 truncate text-xs text-muted">{sub}</p> : null}
    </div>
  );
}

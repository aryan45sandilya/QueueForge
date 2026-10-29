/** Job list filters <-> URL query string. The URL is the source of truth so views are shareable. */

export const JOB_STATES = ["scheduled", "queued", "running", "retrying", "succeeded", "failed", "cancelled"] as const;
export const SORT_FIELDS = ["created_at", "updated_at", "finished_at", "priority", "attempts", "run_at"] as const;
export const PAGE_SIZES = [25, 50, 100] as const;

export interface JobFilters {
  states: string[];
  type: string;
  queue: string;
  q: string;
  from: string; // datetime-local value, interpreted in the viewer's timezone
  to: string;
  fingerprint: string;
  sort: (typeof SORT_FIELDS)[number];
  order: "asc" | "desc";
  limit: number;
  offset: number;
}

export const DEFAULT_FILTERS: JobFilters = {
  states: [],
  type: "",
  queue: "",
  q: "",
  from: "",
  to: "",
  fingerprint: "",
  sort: "created_at",
  order: "desc",
  limit: 50,
  offset: 0,
};

export function parseFilters(params: URLSearchParams): JobFilters {
  const sort = params.get("sort");
  const limit = Number(params.get("limit"));
  const offset = Number(params.get("offset"));
  return {
    states: params.getAll("state").filter((s) => (JOB_STATES as readonly string[]).includes(s)),
    type: params.get("type") ?? "",
    queue: params.get("queue") ?? "",
    q: params.get("q") ?? "",
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
    fingerprint: params.get("fingerprint") ?? "",
    sort: (SORT_FIELDS as readonly string[]).includes(sort ?? "") ? (sort as JobFilters["sort"]) : "created_at",
    order: params.get("order") === "asc" ? "asc" : "desc",
    limit: (PAGE_SIZES as readonly number[]).includes(limit) ? limit : DEFAULT_FILTERS.limit,
    offset: Number.isFinite(offset) && offset > 0 ? Math.floor(offset) : 0,
  };
}

/** Query string for the page URL (only non-default values). */
export function filtersToSearch(f: JobFilters): string {
  const p = new URLSearchParams();
  f.states.forEach((s) => p.append("state", s));
  if (f.type) p.set("type", f.type);
  if (f.queue) p.set("queue", f.queue);
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.from) p.set("from", f.from);
  if (f.to) p.set("to", f.to);
  if (f.fingerprint) p.set("fingerprint", f.fingerprint);
  if (f.sort !== DEFAULT_FILTERS.sort) p.set("sort", f.sort);
  if (f.order !== DEFAULT_FILTERS.order) p.set("order", f.order);
  if (f.limit !== DEFAULT_FILTERS.limit) p.set("limit", String(f.limit));
  if (f.offset) p.set("offset", String(f.offset));
  return p.toString();
}

function toIso(local: string): string | null {
  if (!local) return null;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** Query string for GET /projects/{id}/jobs. */
export function filtersToApiQuery(f: JobFilters): string {
  const p = new URLSearchParams();
  f.states.forEach((s) => p.append("state", s));
  if (f.type) p.append("type", f.type);
  if (f.queue) p.append("queue", f.queue);
  if (f.q.trim()) p.set("q", f.q.trim());
  const from = toIso(f.from);
  const to = toIso(f.to);
  if (from) p.set("created_from", from);
  if (to) p.set("created_to", to);
  if (f.fingerprint) p.set("error_fingerprint", f.fingerprint);
  p.set("sort", f.sort);
  p.set("order", f.order);
  p.set("limit", String(f.limit));
  p.set("offset", String(f.offset));
  return p.toString();
}

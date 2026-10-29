import type { components } from "./openapi";

/** Schema types generated from the backend's OpenAPI document (`npm run gen:api`). */
export type Schemas = components["schemas"];
export type JobSummary = Schemas["JobSummary"];
export type JobDetail = Schemas["JobDetail"];
export type JobState = JobSummary["state"];
export type JobPage = Schemas["Page_JobSummary_"];
export type Project = Schemas["ProjectOut"];
export type Me = Schemas["MeResponse"];
export type QueueOut = Schemas["QueueOut"];
export type WorkerOut = Schemas["WorkerOut"];
export type Metrics = Schemas["MetricsOverview"];
export type ScheduleOut = Schemas["ScheduleOut"];
export type ApiKeyOut = Schemas["ApiKeyOut"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type FailureGroup = Schemas["FailureGroup"];
export type JobType = Schemas["JobTypeOut"];
export type BulkResult = Schemas["BulkResult"];
export type AuditPage = Schemas["Page_AuditOut_"];
export type SchedulePreview = Schemas["SchedulePreview"];

export interface ErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}

/** Mirrors the backend error contract: {"error": {code, message, details, request_id}}. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: unknown = null,
    public requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Validation messages keyed by the last path segment (field name). */
  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    if (Array.isArray(this.details)) {
      for (const item of this.details as ErrorItem[]) {
        const key = item.loc?.filter((p) => p !== "body").join(".") || "_";
        out[key] = item.msg;
      }
    }
    return out;
  }
}

export const API_BASE = "/api/v1";
const UNSAFE = new Set(["POST", "PATCH", "PUT", "DELETE"]);

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) {
    const csrf = readCookie("qf_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the QueueForge API. Check that it is running.");
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const err = (data as { error?: { code?: string; message?: string; details?: unknown; request_id?: string } })
      ?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "http_error",
      err?.message ?? `Request failed with status ${response.status}`,
      err?.details ?? null,
      err?.request_id ?? null,
    );
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  delete: (path: string) => request<void>("DELETE", path),
};

export const fetcher = <T>(path: string) => api.get<T>(path);

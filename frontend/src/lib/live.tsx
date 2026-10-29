"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useSWRConfig } from "swr";

import { API_BASE } from "./api";

export type LiveStatus = "connecting" | "live" | "polling";

export interface JobEvent {
  type: "job.updated";
  job: { id: string; state: string; type: string; queue: string | null; attempts: number; updated_at: string | null };
}

interface LiveValue {
  status: LiveStatus;
  lastEvent: JobEvent | null;
  subscribe: (fn: (e: JobEvent) => void) => () => void;
}

const LiveContext = createContext<LiveValue>({ status: "connecting", lastEvent: null, subscribe: () => () => {} });

export function useLive(): LiveValue {
  return useContext(LiveContext);
}

const POLL_MS = 10_000;
const REVALIDATE_DEBOUNCE_MS = 400;

/**
 * Subscribes to the project's Server-Sent Events stream. Job events trigger a debounced
 * revalidation of project-scoped SWR keys. If the stream is unavailable (Redis down, proxy
 * without streaming), the status switches to "polling" and data refreshes every 10 s instead.
 * The header shows which mode is active, so the UI never implies live data it doesn't have.
 */
export function LiveProvider({ projectId, children }: { projectId: string; children: ReactNode }) {
  const { mutate } = useSWRConfig();
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [lastEvent, setLastEvent] = useState<JobEvent | null>(null);
  const listeners = useRef(new Set<(e: JobEvent) => void>());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const prefix = `/projects/${projectId}/`;
    const revalidate = () => {
      if (timer.current) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        void mutate((key) => typeof key === "string" && key.startsWith(prefix) && !key.includes("/api-keys"));
      }, REVALIDATE_DEBOUNCE_MS);
    };

    let source: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const startPolling = () => {
      setStatus("polling");
      if (!poll) poll = setInterval(revalidate, POLL_MS);
    };
    const stopPolling = () => {
      if (poll) clearInterval(poll);
      poll = null;
    };

    const connect = () => {
      if (closed || typeof EventSource === "undefined") {
        startPolling();
        return;
      }
      setStatus((s) => (s === "live" ? s : "connecting"));
      source = new EventSource(`${API_BASE}/projects/${projectId}/events`, { withCredentials: true });
      source.addEventListener("ready", () => {
        stopPolling();
        setStatus("live");
      });
      source.addEventListener("job", (msg) => {
        try {
          const event = JSON.parse((msg as MessageEvent<string>).data) as JobEvent;
          setLastEvent(event);
          listeners.current.forEach((fn) => fn(event));
          revalidate();
        } catch {
          /* ignore malformed event */
        }
      });
      const fallBack = () => {
        source?.close();
        source = null;
        startPolling();
        if (!closed) retry = setTimeout(connect, 30_000);
      };
      source.addEventListener("unavailable", fallBack);
      source.onerror = fallBack;
    };

    connect();
    return () => {
      closed = true;
      source?.close();
      stopPolling();
      if (retry) clearTimeout(retry);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [projectId, mutate]);

  const subscribe = (fn: (e: JobEvent) => void) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  };

  return <LiveContext.Provider value={{ status, lastEvent, subscribe }}>{children}</LiveContext.Provider>;
}

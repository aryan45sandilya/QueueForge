"use client";

import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import useSWR from "swr";

import { ApiError, api, fetcher, type Me, type Project } from "./api";

const PROJECT_KEY = "qf.projectId";

interface SessionValue {
  me: Me;
  projects: Project[];
  project: Project | null;
  isOwner: boolean;
  selectProject: (id: string) => void;
  refreshProjects: () => Promise<unknown>;
  logout: () => Promise<void>;
}

const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside <SessionProvider>");
  return value;
}

/** The current project; pages under the app shell only render once one is selected. */
export function useProject(): Project {
  const { project } = useSession();
  if (!project) throw new Error("No project selected");
  return project;
}

function readStoredProject(): string | null {
  try {
    return window.localStorage.getItem(PROJECT_KEY);
  } catch {
    return null;
  }
}

export function SessionProvider({ children, fallback }: { children: ReactNode; fallback: ReactNode }) {
  const router = useRouter();
  const me = useSWR<Me, ApiError>("/auth/me", fetcher, { shouldRetryOnError: false, revalidateOnFocus: false });
  const projects = useSWR<Project[], ApiError>(me.data ? "/projects" : null, fetcher);
  // Children render only after /auth/me resolves on the client, so reading storage here can't
  // cause a hydration mismatch.
  const [selected, setSelected] = useState<string | null>(() =>
    typeof window === "undefined" ? null : readStoredProject(),
  );

  useEffect(() => {
    if (me.error?.status === 401) router.replace("/login");
  }, [me.error, router]);

  const selectProject = useCallback((id: string) => {
    setSelected(id);
    try {
      window.localStorage.setItem(PROJECT_KEY, id);
    } catch {
      /* storage unavailable: selection lasts for this page view */
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      router.replace("/login");
    }
  }, [router]);

  const value = useMemo<SessionValue | null>(() => {
    if (!me.data || !projects.data) return null;
    const list = projects.data;
    const project = list.find((p) => p.id === selected) ?? list[0] ?? null;
    return {
      me: me.data,
      projects: list,
      project,
      isOwner: project?.role === "owner",
      selectProject,
      refreshProjects: () => projects.mutate(),
      logout,
    };
  }, [me.data, projects, selected, selectProject, logout]);

  if (me.error && me.error.status !== 401) {
    return (
      <div className="grid min-h-screen place-items-center p-6 text-center">
        <div className="max-w-sm">
          <p className="font-medium">QueueForge API unavailable</p>
          <p className="mt-1 text-sm text-muted">{me.error.message}</p>
          <button className="mt-4 text-sm underline" onClick={() => me.mutate()}>
            Retry
          </button>
        </div>
      </div>
    );
  }
  if (!value) return <>{fallback}</>;
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

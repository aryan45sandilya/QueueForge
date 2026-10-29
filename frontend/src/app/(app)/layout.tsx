"use client";

import type { ReactNode } from "react";

import { CreateProjectForm } from "@/components/CreateProjectForm";
import { Logo } from "@/components/icons";
import { Shell } from "@/components/Shell";
import { Loading } from "@/components/ui";
import { LiveProvider } from "@/lib/live";
import { SessionProvider, useSession } from "@/lib/session";

function FirstProject() {
  const { refreshProjects, selectProject } = useSession();
  return (
    <main className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-[400px] rounded-[4px] border border-line bg-surface p-5">
        <div className="mb-3 flex items-center gap-2">
          <Logo className="text-ink" />
          <h1 className="text-base font-semibold">Create your first project</h1>
        </div>
        <p className="mb-4 text-xs text-muted">
          Projects isolate jobs, queues, schedules and API keys. You become the project owner, and a{" "}
          <code className="font-mono">default</code> queue is created for you.
        </p>
        <CreateProjectForm
          onCreated={async (p) => {
            selectProject(p.id);
            await refreshProjects();
          }}
        />
      </div>
    </main>
  );
}

function Gate({ children }: { children: ReactNode }) {
  const { project } = useSession();
  if (!project) return <FirstProject />;
  return (
    <LiveProvider projectId={project.id} key={project.id}>
      <Shell>{children}</Shell>
    </LiveProvider>
  );
}

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider fallback={<Loading label="Loading workspace" />}>
      <Gate>{children}</Gate>
    </SessionProvider>
  );
}

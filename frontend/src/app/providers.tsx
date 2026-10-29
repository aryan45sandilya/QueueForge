"use client";

import type { ReactNode } from "react";
import { SWRConfig } from "swr";

import { ToastProvider } from "@/components/ui";
import { ApiError, fetcher } from "@/lib/api";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <SWRConfig
      value={{
        fetcher,
        keepPreviousData: true,
        revalidateOnFocus: true,
        shouldRetryOnError: (err: unknown) => !(err instanceof ApiError && err.status >= 400 && err.status < 500),
        errorRetryCount: 3,
      }}
    >
      <ToastProvider>{children}</ToastProvider>
    </SWRConfig>
  );
}

import type { SVGProps } from "react";

/** A small stroke icon set drawn on a 16px grid; inherits currentColor. */
function Icon({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      width={16}
      height={16}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.4}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

type P = SVGProps<SVGSVGElement>;

export const IconOverview = (p: P) => (
  <Icon {...p}>
    <path d="M2 13.5h12" />
    <path d="M4 11V7.5M7 11V4.5M10 11V8.5M13 11V2.5" />
  </Icon>
);
export const IconJobs = (p: P) => (
  <Icon {...p}>
    <path d="M5.5 4h8M5.5 8h8M5.5 12h8" />
    <path d="M2.5 4h.5M2.5 8h.5M2.5 12h.5" />
  </Icon>
);
export const IconQueues = (p: P) => (
  <Icon {...p}>
    <path d="M8 2 14 5 8 8 2 5Z" />
    <path d="m2 8 6 3 6-3M2 11l6 3 6-3" />
  </Icon>
);
export const IconFailed = (p: P) => (
  <Icon {...p}>
    <path d="M8 2.2 14.3 13H1.7Z" />
    <path d="M8 6.5v3M8 11.3v.2" />
  </Icon>
);
export const IconSchedules = (p: P) => (
  <Icon {...p}>
    <circle cx="8" cy="8" r="5.8" />
    <path d="M8 4.8V8l2.2 1.4" />
  </Icon>
);
export const IconKey = (p: P) => (
  <Icon {...p}>
    <circle cx="5.2" cy="10.8" r="2.7" />
    <path d="m7.2 8.8 6-6M11.2 4.8l1.6 1.6M9.7 6.3l1.2 1.2" />
  </Icon>
);
export const IconSettings = (p: P) => (
  <Icon {...p}>
    <path d="M2 4.5h7M12 4.5h2M2 11.5h2M7 11.5h7" />
    <circle cx="10.5" cy="4.5" r="1.5" />
    <circle cx="5.5" cy="11.5" r="1.5" />
  </Icon>
);
export const IconDocs = (p: P) => (
  <Icon {...p}>
    <path d="M3 2.5h7.5L13 5v8.5H3Z" />
    <path d="M10.5 2.5V5H13M5.5 8h5M5.5 10.5h5" />
  </Icon>
);
export const IconMenu = (p: P) => (
  <Icon {...p}>
    <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
  </Icon>
);
export const IconClose = (p: P) => (
  <Icon {...p}>
    <path d="m4 4 8 8M12 4l-8 8" />
  </Icon>
);
export const IconCopy = (p: P) => (
  <Icon {...p}>
    <rect x="5.5" y="5.5" width="8" height="8" rx="1" />
    <path d="M10.5 5.5v-3h-8v8h3" />
  </Icon>
);
export const IconCheck = (p: P) => (
  <Icon {...p}>
    <path d="m3 8.5 3 3 7-7" />
  </Icon>
);
export const IconPlus = (p: P) => (
  <Icon {...p}>
    <path d="M8 3v10M3 8h10" />
  </Icon>
);
export const IconChevronLeft = (p: P) => (
  <Icon {...p}>
    <path d="m10 3.5-4.5 4.5 4.5 4.5" />
  </Icon>
);
export const IconChevronRight = (p: P) => (
  <Icon {...p}>
    <path d="m6 3.5 4.5 4.5L6 12.5" />
  </Icon>
);
export const IconArrowLeft = (p: P) => (
  <Icon {...p}>
    <path d="M13 8H3M7 4 3 8l4 4" />
  </Icon>
);
export const IconRetry = (p: P) => (
  <Icon {...p}>
    <path d="M13 8a5 5 0 1 1-1.5-3.6" />
    <path d="M13 2.5v3h-3" />
  </Icon>
);
export const IconStop = (p: P) => (
  <Icon {...p}>
    <rect x="4" y="4" width="8" height="8" rx="1" />
  </Icon>
);
export const IconPause = (p: P) => (
  <Icon {...p}>
    <path d="M6 3.5v9M10 3.5v9" />
  </Icon>
);
export const IconPlay = (p: P) => (
  <Icon {...p}>
    <path d="M5 3.5v9l7.5-4.5Z" />
  </Icon>
);
export const IconLogout = (p: P) => (
  <Icon {...p}>
    <path d="M9.5 3H3v10h6.5M7 8h7M11.5 5.5 14 8l-2.5 2.5" />
  </Icon>
);
export const IconInfo = (p: P) => (
  <Icon {...p}>
    <circle cx="8" cy="8" r="5.8" />
    <path d="M8 7.2v3.6M8 5.2v.1" />
  </Icon>
);

/** The QueueForge mark: three stacked bars, the top one offset like a job leaving a queue. */
export function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" width={20} height={20} className={className} aria-hidden="true">
      <rect x="2" y="12.5" width="16" height="3.5" rx="1" fill="currentColor" />
      <rect x="2" y="7.25" width="16" height="3.5" rx="1" fill="currentColor" opacity="0.55" />
      <rect x="6" y="2" width="12" height="3.5" rx="1" fill="var(--qf-copper)" />
    </svg>
  );
}

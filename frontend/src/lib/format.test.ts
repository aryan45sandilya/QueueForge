import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration, formatPercent, relativeTime, shortId } from "./format";

describe("format", () => {
  it("formats durations compactly", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(340)).toBe("340 ms");
    expect(formatDuration(2400)).toBe("2.4 s");
    expect(formatDuration(185_000)).toBe("3m 05s");
    expect(formatDuration(4_320_000)).toBe("1h 12m");
    expect(formatDuration(2_579_600)).toBe("43m 00s");
    expect(formatDuration(59_700)).toBe("1m 00s");
  });

  it("formats percentages and bytes", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(0.25)).toBe("25.0%");
    expect(formatBytes(1536)).toBe("1.5 KB");
  });

  it("renders relative times against a fixed clock", () => {
    const now = Date.parse("2026-01-01T12:00:00Z");
    expect(relativeTime("2026-01-01T11:59:58Z", now)).toBe("just now");
    expect(relativeTime("2026-01-01T11:55:00Z", now)).toMatch(/5 minutes ago/);
    expect(shortId("1234567890abcdef")).toBe("12345678");
  });
});

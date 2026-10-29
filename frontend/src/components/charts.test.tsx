import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TimeSeriesChart, niceMax } from "./charts";

const series = [
  { key: "ok", label: "Succeeded", color: "red", kind: "bar" as const },
  { key: "bad", label: "Failed", color: "blue", kind: "bar" as const },
];

describe("TimeSeriesChart", () => {
  it("shows an explicit empty state instead of an empty plot", () => {
    render(
      <TimeSeriesChart
        ariaLabel="throughput"
        points={[{ t: "2026-01-01T00:00:00Z", values: { ok: 0, bad: 0 } }]}
        series={series}
        bucketSeconds={60}
        unit="jobs"
        formatValue={String}
        emptyMessage="Nothing ran"
      />,
    );
    expect(screen.getByText("Nothing ran")).toBeInTheDocument();
    expect(screen.getByText("Succeeded")).toBeInTheDocument(); // legend for multi-series
  });

  it("offers a table view with the same numbers", async () => {
    const user = userEvent.setup();
    render(
      <TimeSeriesChart
        ariaLabel="throughput"
        points={[
          { t: "2026-01-01T00:00:00Z", values: { ok: 7, bad: 2 } },
          { t: "2026-01-01T00:01:00Z", values: { ok: 3, bad: null } },
        ]}
        series={series}
        bucketSeconds={60}
        unit="jobs"
        formatValue={String}
        emptyMessage="Nothing ran"
      />,
    );
    expect(screen.queryByText("Nothing ran")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Table view" }));
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("7");
    expect(rows[2]).toHaveTextContent("—");
  });

  it("rounds axis maxima to clean numbers", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(7)).toBe(10);
    expect(niceMax(130)).toBe(200);
    expect(niceMax(2300)).toBe(2500);
  });
});

import { describe, expect, it } from "vitest";

import { DEFAULT_FILTERS, filtersToApiQuery, filtersToSearch, parseFilters } from "./jobFilters";

describe("job filters", () => {
  it("round-trips through the page URL", () => {
    const f = { ...DEFAULT_FILTERS, states: ["failed", "retrying"], type: "data.transform", q: "abc", sort: "priority" as const, order: "asc" as const, offset: 50 };
    const parsed = parseFilters(new URLSearchParams(filtersToSearch(f)));
    expect(parsed).toEqual(f);
  });

  it("drops invalid values instead of sending them to the API", () => {
    const parsed = parseFilters(new URLSearchParams("state=exploded&state=queued&sort=drop_table&limit=9999&offset=-5"));
    expect(parsed.states).toEqual(["queued"]);
    expect(parsed.sort).toBe("created_at");
    expect(parsed.limit).toBe(DEFAULT_FILTERS.limit);
    expect(parsed.offset).toBe(0);
  });

  it("omits defaults from the page URL", () => {
    expect(filtersToSearch(DEFAULT_FILTERS)).toBe("");
  });

  it("builds the API query with ISO dates and repeated states", () => {
    const query = new URLSearchParams(
      filtersToApiQuery({ ...DEFAULT_FILTERS, states: ["queued", "running"], from: "2026-01-02T03:04", fingerprint: "abc123" }),
    );
    expect(query.getAll("state")).toEqual(["queued", "running"]);
    expect(query.get("created_from")).toBe(new Date("2026-01-02T03:04").toISOString());
    expect(query.get("error_fingerprint")).toBe("abc123");
    expect(query.get("limit")).toBe("50");
    expect(query.has("created_to")).toBe(false);
  });
});

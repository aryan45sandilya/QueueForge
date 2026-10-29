import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "qf_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("api client", () => {
  it("sends the CSRF cookie value as a header on unsafe methods only", async () => {
    document.cookie = "qf_csrf=token-123";
    const fetchMock = mockFetch(200, { ok: true });
    await api.post("/projects", { name: "x" });
    await api.get("/projects");
    const [, postInit] = fetchMock.mock.calls[0]!;
    const [, getInit] = fetchMock.mock.calls[1]!;
    expect(postInit.headers["X-CSRF-Token"]).toBe("token-123");
    expect(postInit.body).toBe(JSON.stringify({ name: "x" }));
    expect(getInit.headers["X-CSRF-Token"]).toBeUndefined();
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/v1/projects");
  });

  it("turns the error contract into an ApiError with field errors", async () => {
    mockFetch(422, {
      error: {
        code: "validation_error",
        message: "Payload is not valid",
        details: [
          { loc: ["body", "priority"], msg: "too big", type: "less_than_equal" },
          { loc: ["payload", "recipients"], msg: "too short", type: "too_short" },
        ],
        request_id: "req-1",
      },
    });
    const err = await api.post("/projects/p/jobs", {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(422);
    expect(apiErr.code).toBe("validation_error");
    expect(apiErr.requestId).toBe("req-1");
    expect(apiErr.fieldErrors()).toEqual({ priority: "too big", "payload.recipients": "too short" });
  });

  it("reports network failures distinctly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const err = (await api.get("/auth/me").catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(err.code).toBe("network_error");
  });

  it("returns undefined for 204 responses", async () => {
    mockFetch(204, undefined);
    await expect(api.delete("/projects/p/queues/q")).resolves.toBeUndefined();
  });
});

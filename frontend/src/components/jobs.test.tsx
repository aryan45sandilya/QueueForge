import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { fetcher } from "@/lib/api";

import { SubmitJobDialog } from "./jobs";

// Paste instead of typing key by key: faster, and immune to jsdom's missing cursor support in
// type="email" inputs, which can reorder characters typed during a re-render.
async function fill(user: ReturnType<typeof userEvent.setup>, el: HTMLElement, text: string) {
  await user.click(el);
  await user.paste(text);
}

const JOB_TYPES = [
  {
    name: "bench.synthetic",
    title: "Synthetic",
    description: "Sleeps",
    side_effects: "",
    default_timeout_seconds: 60,
    demo_only: true,
    payload_schema: { type: "object" },
    example: { work_ms: 200 },
  },
  {
    name: "notification.send",
    title: "Notify",
    description: "Sends notifications",
    side_effects: "",
    default_timeout_seconds: 120,
    demo_only: false,
    payload_schema: { type: "object" },
    example: { template: "welcome" },
  },
];

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type FetchMock = Mock<(url: string, init?: RequestInit) => Promise<Response>>;

function lookups(url: string): Promise<Response> {
  return Promise.resolve(url.endsWith("/job-types") ? json(200, JOB_TYPES) : json(200, [{ id: "q", name: "default", paused: false }]));
}

function renderDialog(fetchMock: FetchMock, onSubmitted = vi.fn()) {
  vi.stubGlobal("fetch", fetchMock);
  render(
    <SWRConfig value={{ provider: () => new Map(), fetcher, dedupingInterval: 0 }}>
      <SubmitJobDialog projectId="p1" open onClose={() => {}} onSubmitted={onSubmitted} />
    </SWRConfig>,
  );
  return onSubmitted;
}

const postCalls = (fetchMock: FetchMock) => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");

afterEach(() => vi.unstubAllGlobals());

describe("SubmitJobDialog", () => {
  it("defaults to the first non-demo handler and its example payload", async () => {
    renderDialog(vi.fn(lookups) as FetchMock);
    expect(await screen.findByDisplayValue(/notification.send/)).toBeInTheDocument();
    expect((screen.getByLabelText("Payload (JSON)") as HTMLTextAreaElement).value).toContain('"template": "welcome"');
  });

  it("rejects malformed JSON locally without calling the API", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(lookups) as FetchMock;
    renderDialog(fetchMock);
    await screen.findByDisplayValue(/notification.send/);
    const payload = screen.getByLabelText("Payload (JSON)");
    await user.clear(payload);
    await fill(user, payload, "not json");
    await user.click(screen.getByRole("button", { name: "Submit job" }));
    expect(await screen.findByText(/Payload is not valid JSON/)).toBeInTheDocument();
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it("shows server-side payload validation errors next to the payload", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          json(422, {
            error: {
              code: "validation_error",
              message: "Payload is not valid for job type notification.send",
              details: [{ loc: ["payload", "recipients"], msg: "Field required", type: "missing" }],
            },
          }),
        );
      }
      return lookups(url);
    }) as FetchMock;
    const onSubmitted = renderDialog(fetchMock);
    await screen.findByDisplayValue(/notification.send/);
    await user.click(screen.getByRole("button", { name: "Submit job" }));
    expect(await screen.findByText(/payload.recipients: Field required/)).toBeInTheDocument();
    expect(screen.getAllByRole("alert").some((el) => el.textContent?.includes("not valid for job type"))).toBe(true);
    expect(onSubmitted).not.toHaveBeenCalled();
    const body = JSON.parse(String(postCalls(fetchMock)[0]?.[1]?.body));
    expect(body).toMatchObject({ type: "notification.send", queue: "default", priority: 5, payload: { template: "welcome" } });
  });
});

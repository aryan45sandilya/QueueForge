import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthForm } from "./AuthForm";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn() }) }));

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => replace.mockReset());
afterEach(() => vi.unstubAllGlobals());

describe("AuthForm", () => {
  it("shows the server's message on failed sign-in and stays on the page", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond(401, { error: { code: "unauthorized", message: "Invalid email or password" } })),
    );
    render(<AuthForm mode="login" />);
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
    expect(replace).not.toHaveBeenCalled();
  });

  it("maps validation errors onto fields when registering", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        respond(422, {
          error: {
            code: "validation_error",
            message: "Request validation failed",
            details: [{ loc: ["body", "password"], msg: "String should have at least 10 characters", type: "string_too_short" }],
          },
        }),
      ),
    );
    render(<AuthForm mode="register" />);
    await user.type(screen.getByLabelText("Name"), "Ada");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("String should have at least 10 characters")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
  });

  it("registers, signs in and navigates to the dashboard", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(respond(201, { id: "u1", email: "ada@example.com" }))
      .mockResolvedValueOnce(respond(200, { csrf_token: "x", user: {}, projects: [] }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthForm mode="register" />);
    await user.type(screen.getByLabelText("Name"), "Ada");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["/api/v1/auth/register", "/api/v1/auth/login"]);
  });
});

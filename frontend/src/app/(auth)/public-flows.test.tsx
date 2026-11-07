import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthForm } from "./AuthForm";
import ResetPasswordPage from "./reset-password/page";

// Paste instead of typing key by key: faster, and immune to jsdom's missing cursor support in
// type="email" inputs, which can reorder characters typed during a re-render.
async function fill(user: ReturnType<typeof userEvent.setup>, el: HTMLElement, text: string) {
  await user.click(el);
  await user.paste(text);
}

const replace = vi.fn();
let params = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => params,
}));

function respond(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  replace.mockReset();
  params = new URLSearchParams();
});
afterEach(() => vi.unstubAllGlobals());

describe("public sign-up flows", () => {
  it("asks new users to confirm their email instead of signing them in", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(respond(201, { id: "u1", email: "ada@example.com", verification_required: true }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthForm mode="register" />);
    await fill(user, screen.getByLabelText("Name"), "Ada");
    await fill(user, screen.getByLabelText("Email"), "ada@example.com");
    await fill(user, screen.getByLabelText("Password"), "long-enough-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Check your inbox")).toBeInTheDocument();
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["/api/v1/auth/register"]);
    expect(replace).not.toHaveBeenCalled();
  });

  it("offers a new confirmation link when an unverified account signs in", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(respond(403, { error: { code: "email_not_verified", message: "Confirm your email address before signing in." } }))
      .mockResolvedValueOnce(respond(202, { message: "If an account exists for that address, an email is on its way." }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthForm mode="login" />);
    expect(screen.getByRole("link", { name: "Forgot password?" })).toHaveAttribute("href", "/forgot-password");
    await fill(user, screen.getByLabelText("Email"), "ada@example.com");
    await fill(user, screen.getByLabelText("Password"), "whatever-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.click(await screen.findByRole("button", { name: /new confirmation link/i }));
    expect(await screen.findByText(/an email is on its way/)).toBeInTheDocument();
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/auth/resend-verification");
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({ email: "ada@example.com" });
  });

  it("resets a password only when both entries match", async () => {
    const user = userEvent.setup();
    params = new URLSearchParams("token=abcdefghijklmnopqrstuvwxyz");
    const fetchMock = vi.fn().mockResolvedValue(respond(200, { message: "Password updated." }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ResetPasswordPage />);
    const submit = await screen.findByRole("button", { name: "Set new password" });
    await fill(user, screen.getByLabelText("New password"), "a fresh passphrase");
    await fill(user, screen.getByLabelText("Confirm new password"), "a different one!");
    expect(screen.getByText("Passwords don't match")).toBeInTheDocument();
    expect(submit).toBeDisabled();
    await user.clear(screen.getByLabelText("Confirm new password"));
    await fill(user, screen.getByLabelText("Confirm new password"), "a fresh passphrase");
    await user.click(submit);
    expect(await screen.findByText("Password updated")).toBeInTheDocument();
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      token: "abcdefghijklmnopqrstuvwxyz",
      new_password: "a fresh passphrase",
    });
  });

  it("explains a reset link without a token", async () => {
    render(<ResetPasswordPage />);
    expect(await screen.findByText(/missing its token/)).toBeInTheDocument();
  });
});

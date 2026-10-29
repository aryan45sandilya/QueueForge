import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SecretReveal } from "./SecretReveal";

const SECRET = "qf_0123456789ab_SuperSecretValueThatMustNotLeak123456789";

describe("SecretReveal", () => {
  it("masks the key until revealed and copies the full value", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const onDone = vi.fn();
    render(<SecretReveal secret={SECRET} name="deploy" onDone={onDone} />);

    expect(screen.getByTestId("secret-value")).not.toHaveTextContent(SECRET);
    await user.click(screen.getByRole("button", { name: "Reveal" }));
    expect(screen.getByTestId("secret-value")).toHaveTextContent(SECRET);

    await user.click(screen.getByRole("button", { name: /copy key/i }));
    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /stored it/i }));
    expect(onDone).toHaveBeenCalled();
  });
});

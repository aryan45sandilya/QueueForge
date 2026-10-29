import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";

import { ErrorState, StateBadge } from "./ui";

describe("ui primitives", () => {
  it("never conveys job state by colour alone", () => {
    render(<StateBadge state="failed" />);
    expect(screen.getByText("failed")).toBeInTheDocument();
  });

  it("shows the request id for API errors", () => {
    render(<ErrorState error={new ApiError(500, "internal_error", "Internal server error", null, "req-42")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Internal server error");
    expect(screen.getByRole("alert")).toHaveTextContent("req-42");
  });
});

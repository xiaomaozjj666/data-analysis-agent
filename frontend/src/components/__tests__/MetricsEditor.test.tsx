/// <reference types="vitest/globals" />
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import MetricsEditor from "../MetricsEditor";

const apiMock = vi.fn();
vi.mock("../../utils/api", () => ({
  api: (...args: unknown[]) => apiMock(...args),
}));

describe("MetricsEditor", () => {
  beforeEach(() => {
    apiMock.mockReset();
  });

  it("loads and saves session metrics", async () => {
    apiMock.mockResolvedValue({
      metrics: [{ name: "净销售额", description: "销售额减去退货", expression: "销售额 - 退货金额", aliases: [], source_columns: [] }],
      source: "workspace",
      env_overrides: false,
    });
    render(<MetricsEditor sessionId="s1" />);
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith("/api/sessions/s1/metrics"));
    fireEvent.click(screen.getByRole("button", { name: /业务口径/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存口径" }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledTimes(2));
    expect(apiMock.mock.calls[1][1]).toMatchObject({ method: "PUT" });
    expect(await screen.findByText(/已保存 1 条口径/)).toBeInTheDocument();
  });
});

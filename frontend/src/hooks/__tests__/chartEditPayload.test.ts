/// <reference types="vitest/globals" />
import "@testing-library/jest-dom";

import { buildChartEditPayload } from "../useArtifactPreview";

describe("buildChartEditPayload", () => {
  it("omits color unless the user touched the color picker", () => {
    // 用户只改标题：不得把默认单色发给后端（会覆盖图表分组配色）
    expect(buildChartEditPayload("新标题", "#245C55", false)).toEqual({ title: "新标题" });
  });

  it("sends color only when the user explicitly changed it", () => {
    expect(buildChartEditPayload("新标题", "#E15759", true)).toEqual({
      title: "新标题",
      color: "#E15759",
    });
  });

  it("supports color-only edits and empty bodies", () => {
    expect(buildChartEditPayload(undefined, "#F28E2B", true)).toEqual({ color: "#F28E2B" });
    expect(buildChartEditPayload(undefined, "#245C55", false)).toEqual({});
  });
});

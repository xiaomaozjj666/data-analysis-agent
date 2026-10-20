import { describe, expect, it } from "vitest";
import { splitReportAppendix } from "../reportAppendix";

describe("splitReportAppendix", () => {
  it("keeps reports without an appendix intact", () => {
    const md = "## 结论\n- ok";
    expect(splitReportAppendix(md)).toEqual({ body: md, appendix: "" });
  });

  it("splits the verification appendix from the narrative", () => {
    const md = "## 结论速览\n- 销售额上升\n\n## 核验附录\n| 事实 | 值 |\n";
    const split = splitReportAppendix(md);
    expect(split.body).toContain("结论速览");
    expect(split.body).not.toContain("核验附录");
    expect(split.appendix.startsWith("## 核验附录")).toBe(true);
  });
});

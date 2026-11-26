import { describe, expect, it, vi } from "vitest";
import { restoreKernel } from "../sessionRestore";
import type { Session } from "../../types";

function makeSetters() {
  return {
    setPlan: vi.fn(),
    setCompleted: vi.fn(),
    setPlanSource: vi.fn(),
    setCurrentNodeTitle: vi.fn(),
    setToolTrace: vi.fn(),
  };
}

const baseSession = {
  id: "s1",
  filename: "sales.csv",
  preview: [],
  profile: { row_count: 0, column_count: 0, column_info: [] },
  artifacts: [],
  analysis_status: "running",
} as Session;

describe("restoreKernel", () => {
  it("恢复内核中的计划、步骤、来源、节点与工具时间线", () => {
    const setters = makeSetters();
    const plan = [{ id: "p1", title: "检查", instruction: "x", success_criteria: "y" }];
    const completed = [{ id: "p1", title: "检查", status: "completed" as const }];
    const trace = [{ call_id: "c1", name: "inspect_data", status: "done" as const }];
    const restored = restoreKernel(
      {
        ...baseSession,
        kernel: {
          objective: "检查销售",
          plan,
          plan_source: "fallback",
          completed_steps: completed,
          current_node: "execute_step",
          status: "running",
          tool_trace: trace,
        },
      },
      setters,
      () => "执行步骤",
    );
    expect(restored).toBe(true);
    expect(setters.setPlan).toHaveBeenCalledWith(plan);
    expect(setters.setCompleted).toHaveBeenCalledWith(completed);
    expect(setters.setPlanSource).toHaveBeenCalledWith("fallback");
    expect(setters.setCurrentNodeTitle).toHaveBeenCalledWith("执行步骤");
    expect(setters.setToolTrace).toHaveBeenCalledWith(trace);
  });

  it("没有内核时不做任何恢复", () => {
    const setters = makeSetters();
    expect(restoreKernel(baseSession, setters)).toBe(false);
    expect(setters.setPlan).not.toHaveBeenCalled();
  });

  it("空数组不覆盖已有状态", () => {
    const setters = makeSetters();
    restoreKernel(
      {
        ...baseSession,
        kernel: {
          objective: "",
          plan: [],
          plan_source: "",
          completed_steps: [],
          current_node: "",
          status: "idle",
          tool_trace: [],
        },
      },
      setters,
    );
    expect(setters.setPlan).not.toHaveBeenCalled();
    expect(setters.setCompleted).not.toHaveBeenCalled();
    expect(setters.setToolTrace).not.toHaveBeenCalled();
    expect(setters.setCurrentNodeTitle).not.toHaveBeenCalled();
  });
});

import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PlanPanel from "../PlanPanel";
import type { PlanStep, ToolTraceItem as ToolTraceItemType } from "../../types";

const PLAN: PlanStep[] = [
  { id: "p1", title: "检查数据", instruction: "i1", success_criteria: "输出统计" },
  { id: "p2", title: "生成图表", instruction: "i2", success_criteria: "输出图片" },
];

function renderPanel(overrides: Record<string, unknown> = {}) {
  const props = { plan: PLAN, ...overrides };
  return render(<PlanPanel {...props} />);
}

describe("PlanPanel", () => {
  it("无计划时展示空态", () => {
    renderPanel({ plan: [] });
    expect(screen.getByText(/点「生成计划」后/)).toBeInTheDocument();
  });

  it("运行中展示当前节点、耗时与步骤内进度", () => {
    renderPanel({
      plan: PLAN,
      completed: [{ id: "p1", title: "检查数据", status: "completed" }],
      running: true,
      currentNodeTitle: "执行第 2 步",
      elapsedSeconds: 65,
      stepProgress: { progress: 60, toolCalls: 3, message: "读取数据", stepIndex: 2, totalSteps: 4 },
    });
    expect(screen.getByText("执行第 2 步")).toBeInTheDocument();
    expect(screen.getByText(/步骤 2\/4/)).toBeInTheDocument();
    expect(screen.getByText(/读取数据/)).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("已耗时")).toBeInTheDocument();
  });

  it("旧版 step_progress 无步骤序号时隐藏前缀", () => {
    const { container } = renderPanel({
      plan: PLAN,
      running: true,
      stepProgress: { progress: 30, toolCalls: 1, message: "整理中" },
    });
    expect(container.querySelector(".step-progress-step")).toBeNull();
  });

  it("计划降级与重规划原因给出说明，完成后展示已完成", () => {
    renderPanel({
      plan: PLAN,
      completed: [
        { id: "p1", title: "检查数据", status: "completed" },
        { id: "p2", title: "生成图表", status: "completed" },
      ],
      planSource: "fallback",
      replanReason: "已用完分析预算，提前收尾。",
      elapsedSeconds: 100,
    });
    expect(screen.getByText(/内置默认步骤/)).toBeInTheDocument();
    expect(screen.getByText("已用完分析预算，提前收尾。")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("总耗时")).toBeInTheDocument();
  });

  it("已完成步骤可从这里重跑", () => {
    const onRerunFromStep = vi.fn();
    renderPanel({
      plan: PLAN,
      completed: [{ id: "p1", title: "检查数据", status: "completed" }],
      onRerunFromStep,
    });
    fireEvent.click(screen.getByRole("button", { name: "重跑从此处" }));
    expect(onRerunFromStep).toHaveBeenCalledWith(0);
  });

  it("工具调用超过 8 条时支持展开全部", () => {
    const tools: ToolTraceItemType[] = Array.from({ length: 9 }, (_, index) => ({
      call_id: `c${index}`,
      name: "inspect_data",
      status: "done",
      duration_ms: index,
    }));
    const { container } = renderPanel({ plan: PLAN, toolTrace: tools });
    expect(container.querySelectorAll(".tool-trace-item")).toHaveLength(8);
    fireEvent.click(screen.getByRole("button", { name: /展开全部 \(9\)/ }));
    expect(container.querySelectorAll(".tool-trace-item")).toHaveLength(9);
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(container.querySelectorAll(".tool-trace-item")).toHaveLength(8);
  });

  it("审批模式可编辑标题 / 成功标准并批准", () => {
    const onApprovePlan = vi.fn();
    const { container } = renderPanel({ awaitingApproval: true, pendingObjective: "检查销售", onApprovePlan });
    expect(screen.getByText(/围绕这个问题：检查销售/)).toBeInTheDocument();
    expect(screen.getByText("待审阅")).toBeInTheDocument();

    const inputs = container.querySelectorAll<HTMLInputElement>(".step-edit-field input");
    fireEvent.change(inputs[0], { target: { value: "检查缺失值" } });
    fireEvent.change(inputs[1], { target: { value: "输出缺失统计" } });
    fireEvent.click(screen.getByRole("button", { name: "批准并执行" }));

    expect(onApprovePlan).toHaveBeenCalledWith([
      { ...PLAN[0], title: "检查缺失值", success_criteria: "输出缺失统计" },
      PLAN[1],
    ]);
  });

  it("审批模式支持上移 / 下移 / 删除 / 还原 / 取消", () => {
    const onApprovePlan = vi.fn();
    const onCancelApproval = vi.fn();
    const { container } = renderPanel({ awaitingApproval: true, onApprovePlan, onCancelApproval });

    const controls = () => Array.from(container.querySelectorAll(".step-edit-controls button"));
    // 第一步不能上移，最后一步不能下移
    expect(controls()[0]).toBeDisabled();
    expect(controls().at(-2)).toBeDisabled();

    fireEvent.click(controls()[1]); // 第一步下移
    let inputs = container.querySelectorAll<HTMLInputElement>(".step-edit-field input");
    expect(inputs[0].value).toBe("生成图表");

    // 还原回原始顺序，还原按钮随即禁用
    fireEvent.click(screen.getByRole("button", { name: "还原" }));
    inputs = container.querySelectorAll<HTMLInputElement>(".step-edit-field input");
    expect(inputs[0].value).toBe("检查数据");
    expect(screen.getByRole("button", { name: "还原" })).toBeDisabled();

    // 删除全部步骤后批准按钮禁用
    fireEvent.click(controls()[2]);
    fireEvent.click(controls()[2]);
    expect(container.querySelectorAll(".step-edit-field")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "批准并执行" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancelApproval).toHaveBeenCalled();
  });

  it("审批模式支持 Ctrl+Enter 快捷批准", () => {
    const onApprovePlan = vi.fn();
    renderPanel({ awaitingApproval: true, onApprovePlan });
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    expect(onApprovePlan).toHaveBeenCalledTimes(1);
  });

  it("审批取消或计划为空时 Ctrl+Enter 不触发", () => {
    const onApprovePlan = vi.fn();
    const { rerender } = renderPanel({ onApprovePlan });
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    expect(onApprovePlan).not.toHaveBeenCalled();

    rerender(<PlanPanel plan={[]} awaitingApproval onApprovePlan={onApprovePlan} />);
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    expect(onApprovePlan).not.toHaveBeenCalled();
  });
});

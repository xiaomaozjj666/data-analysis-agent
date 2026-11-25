/// <reference types="vitest/globals" />
import "@testing-library/jest-dom/vitest";
import { createRef } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TaskBox from "../TaskBox";
import { presets } from "../../constants";

const baseProps = {
  task: "比较各区域销售",
  running: false,
  stopping: false,
  configured: true,
  hasSession: true,
  currentNodeTitle: "",
  elapsedSeconds: null,
  onTaskChange: () => {},
  onRun: () => {},
  onRunPlanReview: () => {},
  onStop: () => {},
  taskInputRef: createRef<HTMLTextAreaElement>(),
};

describe("TaskBox", () => {
  it("uses generate-plan as the primary action", () => {
    render(<TaskBox {...baseProps} />);
    expect(screen.getByRole("button", { name: "生成计划" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "跳过审阅" })).toBeEnabled();
  });

  it("keeps the question visible and blocks skip while a plan is waiting", () => {
    render(<TaskBox {...baseProps} awaitingApproval />);
    expect(screen.getByDisplayValue("比较各区域销售")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "跳过审阅" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled();
    expect(screen.getByText(/计划已生成/)).toBeInTheDocument();
  });
});

describe("TaskBox 主要操作接线", () => {
  it("「生成计划」与「跳过审阅」分别回调对应入口", () => {
    const onRunPlanReview = vi.fn();
    const onRun = vi.fn();
    render(<TaskBox {...baseProps} onRun={onRun} onRunPlanReview={onRunPlanReview} />);

    fireEvent.click(screen.getByRole("button", { name: "生成计划" }));
    expect(onRunPlanReview).toHaveBeenCalledTimes(1);
    expect(onRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "跳过审阅" }));
    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it("Ctrl/Cmd+Enter 生成计划；等待审批时不重复提交", () => {
    const onRunPlanReview = vi.fn();
    const { rerender } = render(
      <TaskBox {...baseProps} onRunPlanReview={onRunPlanReview} />,
    );

    const textarea = screen.getByRole("textbox");
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onRunPlanReview).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onRunPlanReview).toHaveBeenCalledTimes(2);

    // 计划待审阅：Ctrl+Enter 不再触发（避免覆盖用户正在编辑的计划）
    rerender(<TaskBox {...baseProps} awaitingApproval onRunPlanReview={onRunPlanReview} />);
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", ctrlKey: true });
    expect(onRunPlanReview).toHaveBeenCalledTimes(2);

    // 普通 Enter 是换行，不触发
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onRunPlanReview).toHaveBeenCalledTimes(2);
  });

  it("未配置 API Key 时主操作与预设都不可用，且预设给出配置提示", () => {
    render(<TaskBox {...baseProps} configured={false} />);

    expect(screen.getByRole("button", { name: "生成计划" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过审阅" })).toBeDisabled();
    const preset = screen.getByRole("button", { name: presets[0].title });
    expect(preset).toBeDisabled();
    expect(preset).toHaveAttribute("title", "请先在左下角配置 API Key");
  });

  it("没有会话时不能生成计划（审阅计划需要已有数据集）", () => {
    render(<TaskBox {...baseProps} hasSession={false} />);
    expect(screen.getByRole("button", { name: "生成计划" })).toBeDisabled();
    // 跳过审阅只依赖任务文本与配置
    expect(screen.getByRole("button", { name: "跳过审阅" })).toBeEnabled();
  });

  it("任务为空时两个执行入口都禁用", () => {
    render(<TaskBox {...baseProps} task="" />);
    expect(screen.getByRole("button", { name: "生成计划" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "跳过审阅" })).toBeDisabled();
  });

  it("点击预设填入任务文本，并把焦点送回输入框", async () => {
    const onTaskChange = vi.fn();
    const ref = createRef<HTMLTextAreaElement>();
    render(<TaskBox {...baseProps} onTaskChange={onTaskChange} taskInputRef={ref} />);

    fireEvent.click(screen.getByRole("button", { name: presets[1].title }));
    expect(onTaskChange).toHaveBeenCalledWith(presets[1].task);
    // 下一帧把焦点送回输入框，用户可直接继续编辑
    await waitFor(() => expect(ref.current).toHaveFocus());
  });

  it("运行中显示当前节点与耗时，停止按钮回调 onStop 且在停止中禁用", () => {
    const onStop = vi.fn();
    const { rerender } = render(
      <TaskBox
        {...baseProps}
        running
        currentNodeTitle="正在执行分析步骤"
        elapsedSeconds={65}
        onStop={onStop}
      />,
    );

    expect(screen.getByText(/正在：正在执行分析步骤/)).toBeInTheDocument();
    expect(screen.getByText(/1 分 5 秒|01:05|1:05/)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeDisabled();
    // 运行中不再展示执行入口
    expect(screen.queryByRole("button", { name: "生成计划" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "停止分析" }));
    expect(onStop).toHaveBeenCalledTimes(1);

    rerender(
      <TaskBox {...baseProps} running stopping onStop={onStop} />,
    );
    expect(screen.getByRole("button", { name: "停止中…" })).toBeDisabled();
  });

  it("等待审批时「查看计划」把焦点交给计划面板回调", () => {
    const onFocusPlan = vi.fn();
    render(<TaskBox {...baseProps} awaitingApproval onFocusPlan={onFocusPlan} />);

    fireEvent.click(screen.getByRole("button", { name: "查看计划" }));
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
  });
});

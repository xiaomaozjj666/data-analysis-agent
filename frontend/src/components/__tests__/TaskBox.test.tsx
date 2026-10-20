/// <reference types="vitest/globals" />
import "@testing-library/jest-dom/vitest";
import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import TaskBox from "../TaskBox";

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

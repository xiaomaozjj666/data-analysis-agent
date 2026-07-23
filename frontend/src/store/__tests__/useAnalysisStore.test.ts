/// <reference types="vitest/globals" />
import { useAnalysisStore } from "../useAnalysisStore";
import type { AnalysisResult, CompletedStep, PlanStep } from "../../types";

// Zustand stores are singletons; reset state between tests for isolation.
beforeEach(() => {
  useAnalysisStore.setState({
    task: "",
    plan: [],
    completed: [],
    result: null,
    running: false,
    awaitingApproval: false,
    pendingObjective: "",
    stepProgress: null,
    currentNodeTitle: "",
  });
});

const samplePlan: PlanStep[] = [
  { id: "s1", title: "Step 1", instruction: "do 1", success_criteria: "ok" },
];

const sampleCompleted: CompletedStep[] = [
  { id: "s1", title: "Step 1", status: "completed", summary: "done" },
];

const sampleResult: AnalysisResult = {
  response: "result text",
  artifacts: [],
  plan: samplePlan,
  completed_steps: sampleCompleted,
};

describe("useAnalysisStore", () => {
  it("has the correct initial state", () => {
    const s = useAnalysisStore.getState();
    expect(s.task).toBe("");
    expect(s.plan).toEqual([]);
    expect(s.completed).toEqual([]);
    expect(s.result).toBeNull();
    expect(s.running).toBe(false);
    expect(s.awaitingApproval).toBe(false);
    expect(s.pendingObjective).toBe("");
    expect(s.stepProgress).toBeNull();
    expect(s.currentNodeTitle).toBe("");
  });

  it("setTask sets the task", () => {
    useAnalysisStore.getState().setTask("analyze sales");
    expect(useAnalysisStore.getState().task).toBe("analyze sales");
  });

  it("setPlan sets the plan", () => {
    useAnalysisStore.getState().setPlan(samplePlan);
    expect(useAnalysisStore.getState().plan).toBe(samplePlan);
  });

  it("setCompleted sets the completed steps", () => {
    useAnalysisStore.getState().setCompleted(sampleCompleted);
    expect(useAnalysisStore.getState().completed).toBe(sampleCompleted);
  });

  it("setRunning toggles the running flag", () => {
    useAnalysisStore.getState().setRunning(true);
    expect(useAnalysisStore.getState().running).toBe(true);
    useAnalysisStore.getState().setRunning(false);
    expect(useAnalysisStore.getState().running).toBe(false);
  });

  it("setResult accepts a direct value", () => {
    useAnalysisStore.getState().setResult(sampleResult);
    expect(useAnalysisStore.getState().result).toBe(sampleResult);
  });

  it("setResult accepts null directly", () => {
    useAnalysisStore.getState().setResult(sampleResult);
    useAnalysisStore.getState().setResult(null);
    expect(useAnalysisStore.getState().result).toBeNull();
  });

  it("setResult supports a functional updater building on previous state", () => {
    useAnalysisStore.getState().setResult(sampleResult);
    useAnalysisStore.getState().setResult((prev) => ({
      ...(prev as AnalysisResult),
      response: "updated response",
    }));
    expect(useAnalysisStore.getState().result?.response).toBe("updated response");
    expect(useAnalysisStore.getState().result?.artifacts).toEqual([]);
  });

  it("setResult functional updater receives null when no result exists", () => {
    useAnalysisStore.getState().setResult((prev) =>
      prev
        ? prev
        : { response: "from updater", artifacts: [], plan: [], completed_steps: [] },
    );
    expect(useAnalysisStore.getState().result?.response).toBe("from updater");
  });

  it("setAwaitingApproval toggles the flag", () => {
    useAnalysisStore.getState().setAwaitingApproval(true);
    expect(useAnalysisStore.getState().awaitingApproval).toBe(true);
    useAnalysisStore.getState().setAwaitingApproval(false);
    expect(useAnalysisStore.getState().awaitingApproval).toBe(false);
  });

  it("setStepProgress sets the step progress object", () => {
    const progress = { progress: 50, toolCalls: 2, message: "running" };
    useAnalysisStore.getState().setStepProgress(progress);
    expect(useAnalysisStore.getState().stepProgress).toEqual(progress);
  });

  it("setStepProgress accepts null to clear", () => {
    useAnalysisStore.getState().setStepProgress({ progress: 1, toolCalls: 0, message: "" });
    useAnalysisStore.getState().setStepProgress(null);
    expect(useAnalysisStore.getState().stepProgress).toBeNull();
  });

  it("setCurrentNodeTitle accepts a direct value", () => {
    useAnalysisStore.getState().setCurrentNodeTitle("node-1");
    expect(useAnalysisStore.getState().currentNodeTitle).toBe("node-1");
  });

  it("setCurrentNodeTitle supports a functional updater", () => {
    useAnalysisStore.getState().setCurrentNodeTitle("node-1");
    useAnalysisStore.getState().setCurrentNodeTitle((prev) => `${prev}!`);
    expect(useAnalysisStore.getState().currentNodeTitle).toBe("node-1!");
  });

  it("setPendingObjective sets the objective", () => {
    useAnalysisStore.getState().setPendingObjective("my objective");
    expect(useAnalysisStore.getState().pendingObjective).toBe("my objective");
  });
});

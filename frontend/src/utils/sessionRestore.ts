// 会话状态恢复纯函数：从完整 session payload 恢复分析结果 / 追问历史。
// 提取自 App.tsx（selectSession 与 retryAnalysis 共用），只做状态变换、
// 不持有任何 hook —— setter 由调用方注入，便于单测与复用。
import type { AnalysisResult, CompletedStep, FollowUpMessage, PlanStep, Session } from "../types";

// setter 最小接口：只要求实际用到的成员（store setter 兼容值/函数两种
// 调用形式，满足这里的窄化签名）。
export interface SessionRestoreSetters {
  setResult: (value: AnalysisResult | null) => void;
  setPlan: (value: PlanStep[]) => void;
  setCompleted: (value: CompletedStep[]) => void;
}

// 从 session.last_result（或最后一条 assistant 消息）恢复分析结果。
// 返回是否恢复出了结果——调用方据此决定后续提示。
export function restoreCompletedAnalysis(latest: Session, setters: SessionRestoreSetters): boolean {
  const { setResult, setPlan, setCompleted } = setters;
  const savedResult = latest.last_result;
  if (savedResult) {
    // 前端 UI 不消费 trace 字段，恢复时丢弃以减小内存占用；
    // 后端持久化时 trace 也已截断到最近 20 条，这里不再透传。
    setResult({
      response: savedResult.response,
      artifacts: savedResult.artifacts || latest.artifacts || [],
      dataset_profile: savedResult.dataset_profile || latest.profile,
      plan: savedResult.plan || [],
      completed_steps: savedResult.completed_steps || [],
    });
    setPlan(savedResult.plan || []);
    setCompleted(savedResult.completed_steps || []);
    return true;
  }
  const assistantMessage = [...(latest.chat || [])]
    .reverse()
    .find((item) => item.role === "assistant" && !!item.content);
  if (!assistantMessage) return false;
  setResult({
    response: assistantMessage.content || "",
    trace: [],
    artifacts: latest.artifacts || [],
    dataset_profile: latest.profile,
    plan: [],
    completed_steps: [],
  });
  setPlan([]);
  setCompleted([]);
  return true;
}

// 从 session.chat 恢复追问历史。chat 数组结构为
// [user(分析任务), assistant(分析报告), user(追问1), assistant(追问1回答), ...]，
// 跳过前两条（首轮分析对），后续的都是追问。
export function restoreFollowUps(
  latest: Session,
  setFollowUps: (value: FollowUpMessage[]) => void,
): void {
  const chat = latest?.chat || [];
  const tail = chat.length > 2 ? chat.slice(2) : [];
  // 恢复完整字段：除 role/content 外，还保留 tools（工具调用 chip）、
  // reasoning（思考过程）、usage（token 用量），让历史会话的追问回复
  // 仍能展示这些信息，而非降级为纯文本。
  setFollowUps(tail.map((item) => ({
    role: item.role,
    content: item.content || "",
    tools: item.tools,
    reasoning: item.reasoning,
    usage: item.usage,
  } as FollowUpMessage)));
}

/// <reference types="vitest/globals" />
import {
  tryFormatJson,
  formatTokens,
  wait,
  formatDuration,
  formatRelativeTime,
  groupSessionsByTime,
  describeHistoryStatus,
  formatBytes,
} from "../format";
import type { HistorySessionItem } from "../../types";

describe("tryFormatJson", () => {
  it("returns empty string for null", () => {
    expect(tryFormatJson(null)).toBe("");
  });
  it("returns empty string for undefined", () => {
    expect(tryFormatJson(undefined)).toBe("");
  });
  it("returns empty string for empty string", () => {
    expect(tryFormatJson("")).toBe("");
  });
  it("returns empty string for falsy 0", () => {
    expect(tryFormatJson(0)).toBe("");
  });
  it("formats a JSON object with 2-space indent", () => {
    expect(tryFormatJson('{"a":1,"b":2}')).toBe('{\n  "a": 1,\n  "b": 2\n}');
  });
  it("formats a JSON array with 2-space indent", () => {
    expect(tryFormatJson("[1,2,3]")).toBe("[\n  1,\n  2,\n  3\n]");
  });
  it("trims surrounding whitespace before parsing", () => {
    expect(tryFormatJson('  {"a":1}  ')).toBe('{\n  "a": 1\n}');
  });
  it("returns the original string when not JSON-like (no { or [ prefix)", () => {
    expect(tryFormatJson("hello world")).toBe("hello world");
  });
  it("returns the original string when JSON parse fails", () => {
    expect(tryFormatJson("{invalid}")).toBe("{invalid}");
  });
});

describe("formatTokens", () => {
  it("returns 0 for zero", () => {
    expect(formatTokens(0)).toBe("0");
  });
  it("returns 0 for negative", () => {
    expect(formatTokens(-5)).toBe("0");
  });
  it("returns 0 for NaN", () => {
    expect(formatTokens(NaN)).toBe("0");
  });
  it("returns 0 for Infinity", () => {
    expect(formatTokens(Infinity)).toBe("0");
  });
  it("returns the raw number below 1000", () => {
    expect(formatTokens(500)).toBe("500");
  });
  it("returns the raw number at 999", () => {
    expect(formatTokens(999)).toBe("999");
  });
  it("returns k format at 1000", () => {
    expect(formatTokens(1000)).toBe("1.0k");
  });
  it("returns k format for 1500", () => {
    expect(formatTokens(1500)).toBe("1.5k");
  });
  it("returns k format for 10000", () => {
    expect(formatTokens(10000)).toBe("10.0k");
  });
});

describe("formatDuration", () => {
  it("returns empty string for null", () => {
    expect(formatDuration(null)).toBe("");
  });
  it("returns empty string for undefined", () => {
    expect(formatDuration(undefined)).toBe("");
  });
  it("returns empty string for negative", () => {
    expect(formatDuration(-1)).toBe("");
  });
  it("returns empty string for NaN", () => {
    expect(formatDuration(NaN)).toBe("");
  });
  it("formats 0 seconds", () => {
    expect(formatDuration(0)).toBe("0 秒");
  });
  it("formats seconds below 60", () => {
    expect(formatDuration(45)).toBe("45 秒");
  });
  it("floors fractional seconds", () => {
    expect(formatDuration(59.9)).toBe("59 秒");
  });
  it("formats exactly 60 seconds as m:ss", () => {
    expect(formatDuration(60)).toBe("1:00");
  });
  it("formats 125 seconds as m:ss", () => {
    expect(formatDuration(125)).toBe("2:05");
  });
  it("formats exactly 3600 seconds as h:mm:ss", () => {
    expect(formatDuration(3600)).toBe("1:00:00");
  });
  it("formats 3661 seconds as h:mm:ss", () => {
    expect(formatDuration(3661)).toBe("1:01:01");
  });
  it("formats 3725 seconds as h:mm:ss", () => {
    expect(formatDuration(3725)).toBe("1:02:05");
  });
});

describe("formatBytes", () => {
  it("returns empty string for 0", () => {
    expect(formatBytes(0)).toBe("");
  });
  it("returns empty string for default (undefined)", () => {
    expect(formatBytes()).toBe("");
  });
  it("formats bytes below 1024", () => {
    expect(formatBytes(512)).toBe("512 B");
  });
  it("formats exactly 1024 as KB", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
  });
  it("formats 2048 as KB", () => {
    expect(formatBytes(2048)).toBe("2.0 KB");
  });
  it("formats exactly 1MB", () => {
    expect(formatBytes(1048576)).toBe("1.0 MB");
  });
  it("formats 1.5MB", () => {
    expect(formatBytes(1572864)).toBe("1.5 MB");
  });
});

describe("describeHistoryStatus", () => {
  it("describes completed", () => {
    expect(describeHistoryStatus("completed")).toEqual({ dot: "is-done", label: "已完成" });
  });
  it("describes running", () => {
    expect(describeHistoryStatus("running")).toEqual({ dot: "is-running", label: "运行中" });
  });
  it("describes cancelling", () => {
    expect(describeHistoryStatus("cancelling")).toEqual({ dot: "is-cancelling", label: "取消中" });
  });
  it("describes cancelled", () => {
    expect(describeHistoryStatus("cancelled")).toEqual({ dot: "is-cancelled", label: "已取消" });
  });
  it("describes failed", () => {
    expect(describeHistoryStatus("failed")).toEqual({ dot: "is-failed", label: "失败" });
  });
  it("falls back to idle for undefined", () => {
    expect(describeHistoryStatus(undefined)).toEqual({ dot: "is-idle", label: "未运行" });
  });
  it("falls back to idle for an unknown status", () => {
    expect(describeHistoryStatus("unknown_status")).toEqual({ dot: "is-idle", label: "未运行" });
  });
  it("falls back to idle for awaiting_approval (no dedicated case)", () => {
    expect(describeHistoryStatus("awaiting_approval")).toEqual({ dot: "is-idle", label: "未运行" });
  });
});

describe("wait", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves after the given delay", async () => {
    let resolved = false;
    wait(100).then(() => {
      resolved = true;
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(resolved).toBe(true);
  });

  it("does not resolve before the delay elapses", async () => {
    let resolved = false;
    wait(100).then(() => {
      resolved = true;
    });
    await vi.advanceTimersByTimeAsync(50);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(50);
    expect(resolved).toBe(true);
  });
});

describe("formatRelativeTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin to 2024-06-12 12:00:00 local time (a Wednesday at noon).
    vi.setSystemTime(new Date(2024, 5, 12, 12, 0, 0));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns empty string for undefined", () => {
    expect(formatRelativeTime(undefined)).toBe("");
  });
  it("returns empty string for 0", () => {
    expect(formatRelativeTime(0)).toBe("");
  });
  it("returns empty string for NaN", () => {
    expect(formatRelativeTime(NaN)).toBe("");
  });
  it("returns 刚刚 for the current time", () => {
    const now = Date.now() / 1000;
    expect(formatRelativeTime(now)).toBe("刚刚");
  });
  it("returns 刚刚 within 60 seconds", () => {
    const now = Date.now() / 1000;
    expect(formatRelativeTime(now - 30)).toBe("刚刚");
  });
  it("returns minutes ago", () => {
    const now = Date.now() / 1000;
    expect(formatRelativeTime(now - 120)).toBe("2 分钟前");
  });
  it("returns hours ago", () => {
    const now = Date.now() / 1000;
    expect(formatRelativeTime(now - 7200)).toBe("2 小时前");
  });
  it("returns days ago", () => {
    const now = Date.now() / 1000;
    expect(formatRelativeTime(now - 86400)).toBe("1 天前");
  });
  it("returns a month/day date for more than a week ago", () => {
    const now = Date.now() / 1000;
    // 8 days before June 12 = June 4
    expect(formatRelativeTime(now - 8 * 86400)).toBe("6/4");
  });
});

describe("groupSessionsByTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin to 2024-06-12 12:00:00 local time (Wednesday noon).
    vi.setSystemTime(new Date(2024, 5, 12, 12, 0, 0));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function makeSession(id: string, createdAt: number): HistorySessionItem {
    return {
      id,
      filename: `${id}.csv`,
      analysis_status: "completed",
      created_at: createdAt,
    };
  }

  it("returns an empty array for null", () => {
    expect(groupSessionsByTime(null)).toEqual([]);
  });
  it("returns an empty array for undefined", () => {
    expect(groupSessionsByTime(undefined)).toEqual([]);
  });
  it("returns an empty array for an empty list", () => {
    expect(groupSessionsByTime([])).toEqual([]);
  });

  it("groups sessions into 今天/昨天/本周/更早", () => {
    const now = Math.floor(Date.now() / 1000);
    const sessions = [
      makeSession("today", now - 3600),
      makeSession("yesterday", now - 86400 - 3600),
      makeSession("thisWeek", now - 2 * 86400 - 3600),
      makeSession("earlier", now - 10 * 86400),
    ];
    const groups = groupSessionsByTime(sessions);
    expect(groups.map((g) => g.label)).toEqual(["今天", "昨天", "本周", "更早"]);
    expect(groups[0].items[0].id).toBe("today");
    expect(groups[1].items[0].id).toBe("yesterday");
    expect(groups[2].items[0].id).toBe("thisWeek");
    expect(groups[3].items[0].id).toBe("earlier");
  });

  it("omits empty groups", () => {
    const now = Math.floor(Date.now() / 1000);
    const sessions = [makeSession("today", now - 3600), makeSession("today2", now - 60)];
    const groups = groupSessionsByTime(sessions);
    expect(groups.map((g) => g.label)).toEqual(["今天"]);
    expect(groups[0].items).toHaveLength(2);
  });

  it("groups multiple items into the same bucket preserving order", () => {
    const now = Math.floor(Date.now() / 1000);
    const sessions = [
      makeSession("y1", now - 86400 - 3600),
      makeSession("y2", now - 86400 - 7200),
    ];
    const groups = groupSessionsByTime(sessions);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("昨天");
    expect(groups[0].items.map((i) => i.id)).toEqual(["y1", "y2"]);
  });

  it("treats sessions missing created_at (0) as 更早", () => {
    const sessions = [makeSession("no-time", 0)];
    const groups = groupSessionsByTime(sessions);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("更早");
  });
});

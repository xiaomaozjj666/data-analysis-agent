import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import EmptyWorkspace from "../EmptyWorkspace";

function renderWorkspace(overrides: Record<string, unknown> = {}) {
  const props = {
    uploading: false,
    uploadProgress: null,
    onUpload: vi.fn(),
    onFileDrop: vi.fn(),
    onCancelUpload: vi.fn(),
    ...overrides,
  };
  const utils = render(<EmptyWorkspace {...props} />);
  return { ...utils, props };
}

function csv(name = "sales.csv"): File {
  return new File(["region,sales\nEast,1\n"], name, { type: "text/csv" });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("EmptyWorkspace", () => {
  it("点击主按钮触发上传，不会因冒泡重复触发", () => {
    const { props } = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: /选择数据文件/ }));
    expect(props.onUpload).toHaveBeenCalledTimes(1);
  });

  it("点击空白区域也可上传；选中文字时忽略", () => {
    const { props, container } = renderWorkspace();
    const section = container.querySelector(".empty-workspace") as HTMLElement;
    fireEvent.click(section);
    expect(props.onUpload).toHaveBeenCalledTimes(1);

    vi.spyOn(window, "getSelection").mockReturnValue({ toString: () => "选中的文案" } as Selection);
    fireEvent.click(section);
    expect(props.onUpload).toHaveBeenCalledTimes(1);

    vi.spyOn(window, "getSelection").mockReturnValue({ toString: () => "" } as Selection);
    fireEvent.click(section);
    expect(props.onUpload).toHaveBeenCalledTimes(2);
  });

  it("上传中禁用入口，进度展示与取消按钮按状态出现", () => {
    const { rerender, props } = renderWorkspace({ uploading: true, uploadProgress: 42 });
    expect(screen.getByText("上传中 42%")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
    fireEvent.click(screen.getByRole("button", { name: "取消上传" }));
    expect(props.onCancelUpload).toHaveBeenCalled();

    rerender(<EmptyWorkspace {...props} uploading uploadProgress={100} />);
    expect(screen.getByText(/正在解析数据…/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消上传" })).toBeNull();

    rerender(<EmptyWorkspace {...props} uploading uploadProgress={null} />);
    expect(screen.getByText("正在读取")).toBeInTheDocument();
  });

  it("上传中点击空白区域不再触发上传", () => {
    const { props, container } = renderWorkspace({ uploading: true });
    fireEvent.click(container.querySelector(".empty-workspace") as HTMLElement);
    expect(props.onUpload).not.toHaveBeenCalled();
  });

  it("鼠标移动更新光斑变量，主按钮有磁吸位移与复位", () => {
    const { container } = renderWorkspace();
    const section = container.querySelector(".empty-workspace") as HTMLElement;
    fireEvent.mouseMove(section, { clientX: 10, clientY: 20 });
    expect(section.style.getPropertyValue("--mouse-x")).toContain("%");

    const button = screen.getByRole("button", { name: /选择数据文件/ });
    fireEvent.mouseMove(button, { clientX: 5, clientY: 5 });
    expect(button.style.transform).toContain("translate");
    fireEvent.mouseLeave(button);
    expect(button.style.transform).toBe("translate(0px)");
  });

  it("拖拽进入高亮，离开复位", () => {
    const { container } = renderWorkspace();
    const section = container.querySelector(".empty-workspace") as HTMLElement;
    fireEvent.dragEnter(section, { dataTransfer: { types: ["Files"] } });
    expect(section.className).toContain("is-drag-over");
    expect(screen.getByText(/松开以上传/)).toBeInTheDocument();
    fireEvent.dragLeave(section);
    expect(section.className).not.toContain("is-drag-over");
  });

  it("拖入多个受支持文件：首个上传，其余派发自定义事件", () => {
    const { container, props } = renderWorkspace();
    const section = container.querySelector(".empty-workspace") as HTMLElement;
    const events: CustomEvent[] = [];
    const listener = (event: Event) => events.push(event as CustomEvent);
    window.addEventListener("empty-workspace:extra-files", listener);
    fireEvent.drop(section, {
      dataTransfer: { files: [csv("a.csv"), csv("b.csv"), new File(["x"], "readme.exe")] },
    });
    expect(props.onFileDrop).toHaveBeenCalledWith(expect.objectContaining({ name: "a.csv" }));
    expect(events).toHaveLength(1);
    expect(events[0].detail.files).toHaveLength(1);
    expect(events[0].detail.files[0].name).toBe("b.csv");
    window.removeEventListener("empty-workspace:extra-files", listener);
  });

  it("拖入全是不支持的文件时忽略", () => {
    const { container, props } = renderWorkspace();
    fireEvent.drop(container.querySelector(".empty-workspace") as HTMLElement, {
      dataTransfer: { files: [new File(["x"], "a.exe")] },
    });
    expect(props.onFileDrop).not.toHaveBeenCalled();
  });

  it("示例入口与更多示例派发载入事件", () => {
    const { container } = renderWorkspace();
    const events: CustomEvent[] = [];
    const listener = (event: Event) => events.push(event as CustomEvent);
    window.addEventListener("empty-workspace:load-sample", listener);

    fireEvent.click(screen.getByRole("button", { name: /体验简报核验/ }));
    const demoButtons = Array.from(container.querySelectorAll(".empty-demo-item")) as HTMLElement[];
    fireEvent.click(demoButtons[0]);
    fireEvent.click(demoButtons.at(-1) as HTMLElement);

    expect(events.map((event) => event.detail)).toEqual([
      { kind: "sales_audit", auditDemo: true },
      { kind: "sales_audit", auditDemo: true },
      { kind: "bigdata", auditDemo: false },
    ]);
    window.removeEventListener("empty-workspace:load-sample", listener);
  });
});

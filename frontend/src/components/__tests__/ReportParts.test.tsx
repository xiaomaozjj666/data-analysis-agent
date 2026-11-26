import "@testing-library/jest-dom/vitest";
import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { markdownComponents, ReasoningBlock, UsageChip } from "../ReportParts";

type ComponentFn = (props: Record<string, unknown>) => ReactNode;

describe("ReasoningBlock", () => {
  it("无内容且非流式时不渲染", () => {
    const { container } = render(<ReasoningBlock />);
    expect(container).toBeEmptyDOMElement();
  });

  it("流式时自动展开，结束后可手动收起", () => {
    const { container, rerender } = render(<ReasoningBlock content="先看趋势" streaming />);
    expect(screen.getByRole("button", { name: /正在思考/ })).toHaveAttribute("aria-expanded", "true");
    expect(container.textContent).toContain("先看趋势");
    rerender(<ReasoningBlock content="先看趋势" />);
    fireEvent.click(screen.getByRole("button", { name: /思考过程/ }));
    expect(screen.getByRole("button", { name: /思考过程/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("流式且无内容时展示省略号与光标", () => {
    const { container } = render(<ReasoningBlock streaming />);
    expect(container.querySelector(".reasoning-cursor")).toBeTruthy();
    expect(container.textContent).toContain("…");
  });
});

describe("UsageChip", () => {
  it("无用量的空对象不渲染", () => {
    const { container } = render(<UsageChip usage={{ prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }} />);
    expect(container).toBeEmptyDOMElement();
    expect(render(<UsageChip usage={null} />).container).toBeEmptyDOMElement();
  });

  it("分别展示输入、输出与总计", () => {
    const { container } = render(
      <UsageChip usage={{ prompt_tokens: 1200, completion_tokens: 300, total_tokens: 0 }} />,
    );
    expect(container.textContent).toContain("输入");
    expect(container.textContent).toContain("输出");
    expect(container.textContent).toContain("共");
    expect(container.textContent).toContain("1.2k");
  });

  it("仅给定部分字段时回退计算总计", () => {
    const { container } = render(<UsageChip usage={{ prompt_tokens: 10, completion_tokens: 0, total_tokens: 10 }} />);
    expect(container.textContent).toContain("共");
    expect(container.textContent).not.toContain("输出");
  });
});

describe("markdownComponents", () => {
  const artifact = {
    name: "柱状图_1.html",
    kind: "visualization",
    path: "/x",
    description: "",
    thumbnail_url: "/thumb.png",
  };

  it("图表占位符渲染为可点击卡片", () => {
    const onPreview = vi.fn();
    const components = markdownComponents([artifact], onPreview, "light");
    const img = components.img as unknown as ComponentFn;
    render(<>{img({ src: "artifact:柱状图_1.html", alt: "柱状图" })}</>);
    fireEvent.click(screen.getByRole("button", { name: /柱状图/ }));
    expect(onPreview).toHaveBeenCalledWith(artifact);
  });

  it("无缩略图时用类型图标；图表丢失时给出提示", () => {
    const components = markdownComponents(
      [{ ...artifact, thumbnail_url: undefined }],
      vi.fn(),
      "dark",
    );
    const img = components.img as unknown as ComponentFn;
    const { container } = render(<>{img({ src: "artifact:柱状图_1.html", alt: "" })}</>);
    expect(container.querySelector(".embedded-chart")).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();

    const missing = markdownComponents([], vi.fn(), "light");
    const missingImg = missing.img as unknown as ComponentFn;
    render(<>{missingImg({ src: "artifact:不存在.html", alt: "不存在" })}</>);
    expect(screen.getByText(/已丢失/)).toBeInTheDocument();
  });

  it("普通图片原样渲染", () => {
    const components = markdownComponents([], vi.fn(), "light");
    const img = components.img as unknown as ComponentFn;
    const { container } = render(
      <>{img({ src: "https://example.com/a.png", alt: "普通图" })}</>,
    );
    expect(container.querySelector("img")).toHaveAttribute("src", "https://example.com/a.png");
  });

  it("行内代码与代码块分别渲染", () => {
    const components = markdownComponents([], vi.fn(), "dark");
    const inline = components.code as unknown as ComponentFn;
    const { container } = render(<>{inline({ inline: true, children: "x = 1" })}</>);
    expect(container.querySelector("code.inline-code")).toBeTruthy();

    const block = render(
      <>{inline({ className: "language-sql", children: "SELECT 1\n" })}</>,
    );
    expect(block.container.querySelector(".code-block")).toBeTruthy();
    expect(block.container.querySelector(".code-block-lang")).toHaveTextContent("sql");

    const plain = components.code as unknown as ComponentFn;
    const noLang = render(<>{plain({ children: "plain" })}</>);
    expect(noLang.container.querySelector(".code-block-lang")).toHaveTextContent("text");
  });

  it("pre 直接透传子节点", () => {
    const components = markdownComponents([], vi.fn(), "light");
    const pre = components.pre as unknown as ComponentFn;
    const { container } = render(<pre>{pre({ children: <span>inner</span> })}</pre>);
    expect(container.querySelector("span")).toHaveTextContent("inner");
  });
});

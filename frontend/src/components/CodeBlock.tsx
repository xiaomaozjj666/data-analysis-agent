import React, { useState } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight, oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import { Check, FileSpreadsheet } from "lucide-react";

// 按需注册 Prism 语言：数据分析场景仅涉及 SQL/Python/JSON/JS/Bash 等。
// 全量导入 Prism 会注册 200+ 语言定义（gzip 后数百 KB），PrismLight 只注册
// 用到的语言，可减少 bundle 体积 200-400KB（gzip）。
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("markdown", markdown);

interface CodeBlockProps {
  language?: string;
  value?: string;
  theme?: "light" | "dark";
}

// 代码块组件：Prism 语法高亮 + 一键复制 + 语言标签。
// 替代 ReactMarkdown 默认的 <pre><code> 渲染，让报告中的 SQL/Python/JSON
// 代码块具备 IDE 级别的可读性。主题随当前 theme 切换 oneLight/oneDark。
const CodeBlock = React.memo(function CodeBlock({ language, value, theme }: CodeBlockProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value || "");
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1800);
    } catch {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 3000);
    }
  };
  const lang = (language || "").toLowerCase() || "text";
  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">{lang}</span>
        <button
          type="button"
          className={`code-block-copy ${copyState === "copied" ? "is-copied" : ""}`}
          onClick={handleCopy}
          aria-label="复制代码"
        >
          {copyState === "copied" ? <Check size={11} /> : <FileSpreadsheet size={11} />}
          {copyState === "copied" ? "已复制" : copyState === "failed" ? "失败" : "复制"}
        </button>
      </div>
      <SyntaxHighlighter
        language={lang}
        style={theme === "dark" ? oneDark : oneLight}
        customStyle={{ margin: 0, padding: "12px 14px", background: "transparent", fontSize: "12.5px" }}
        codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
        wrapLongLines
      >
        {value || ""}
      </SyntaxHighlighter>
    </div>
  );
});

export default CodeBlock;

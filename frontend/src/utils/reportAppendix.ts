/** 把 finalize 追加的「核验附录」从正文拆开，正文先读、附录按需展开。 */

const APPENDIX_HEADING = /(^|\n)##\s*核验附录[^\n]*\n/;

export function splitReportAppendix(markdown: string): { body: string; appendix: string } {
  const text = markdown.replace(/\r\n/g, "\n");
  const match = text.match(APPENDIX_HEADING);
  if (!match || match.index == null) {
    return { body: markdown, appendix: "" };
  }
  const start = match.index + (match[1] === "\n" ? 1 : 0);
  return {
    body: text.slice(0, start).trimEnd(),
    appendix: text.slice(start).trim(),
  };
}

"""从分析报告 Markdown 生成 Word / PPT / PDF，供业务转发。"""

from __future__ import annotations

import io
import re
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt
from pptx import Presentation
from pptx.util import Inches as PptInches
from pptx.util import Pt as PptPt


def _sections(markdown: str) -> list[tuple[str, str]]:
    """按 ``## `` 切分章节；围栏代码块里的 ``## `` 不当作标题。

    报告会把工具原始输出放进代码围栏，输出里一旦出现 ``## ``，旧实现
    会把它误切成一节，Word/PPT/PDF 三种导出都受影响。
    """
    text = (markdown or "").strip()
    if not text:
        return []
    parts: list[list[str]] = [[]]
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## ") and parts[-1]:
            parts.append([])
        parts[-1].append(line)
    sections: list[tuple[str, str]] = []
    for lines in parts:
        if not lines:
            continue
        stripped = [line for line in lines if line.strip()]
        if not stripped:
            continue
        title = re.sub(r"^#+\s*", "", stripped[0]).strip() or "报告"
        body = "\n".join(lines[1:]).strip()
        sections.append((title, body))
    return sections


def build_docx(title: str, markdown: str, chart_paths: list[Path] | None = None) -> bytes:
    document = Document()
    heading = document.add_heading(title or "数据分析报告", level=0)
    heading.runs[0].font.size = Pt(22)
    for section_title, body in _sections(markdown) or [("报告", markdown or "暂无内容")]:
        document.add_heading(section_title, level=1)
        for paragraph in body.split("\n"):
            if paragraph.strip():
                document.add_paragraph(paragraph.strip())
    for path in (chart_paths or [])[:8]:
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"} and path.is_file():
            document.add_heading(path.stem, level=2)
            document.add_picture(str(path), width=Inches(6.2))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_pptx(title: str, markdown: str, chart_paths: list[Path] | None = None) -> bytes:
    presentation = Presentation()
    blank = (
        presentation.slide_layouts[6]
        if len(presentation.slide_layouts) > 6
        else presentation.slide_layouts[0]
    )
    title_layout = presentation.slide_layouts[0]
    title_slide = presentation.slides.add_slide(title_layout)
    title_slide.shapes.title.text = title or "数据分析报告"
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = "由 Data Analysis Agent 根据工具结果生成"

    sections = _sections(markdown)
    overview = next((body for name, body in sections if "结论" in name or "Executive" in name), "")
    if overview:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "结论速览"
        slide.placeholders[1].text = overview[:1800]

    for path in (chart_paths or [])[:3]:
        if not (path.suffix.lower() in {".png", ".jpg", ".jpeg"} and path.is_file()):
            continue
        slide = presentation.slides.add_slide(blank)
        slide.shapes.add_picture(str(path), PptInches(0.4), PptInches(0.8), width=PptInches(9.2))
        box = slide.shapes.add_textbox(PptInches(0.4), PptInches(0.2), PptInches(9), PptInches(0.5))
        box.text_frame.text = path.stem
        box.text_frame.paragraphs[0].font.size = PptPt(16)

    caveats = next((body for name, body in sections if "注意" in name or "Caveat" in name), "")
    if caveats:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "局限与建议"
        slide.placeholders[1].text = caveats[:1800]

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def build_pdf(title: str, markdown: str, chart_paths: list[Path] | None = None) -> bytes:
    """从报告 Markdown 生成 PDF（reportlab，按需导入）。

    运行时依赖：``reportlab``（开发/CI 环境已由 dev extras 安装；生产环境需
    ``pip install reportlab``）。中文使用 reportlab 内置 CID 字体
    ``STSong-Light``，不依赖外部字体文件。
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:  # pragma: no cover - 依赖缺失环境
        raise RuntimeError("PDF 导出需要 reportlab：pip install reportlab") from exc
    from xml.sax.saxutils import escape

    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CnTitle", parent=styles["Title"], fontName=font_name, fontSize=20, leading=26
    )
    heading_style = ParagraphStyle(
        "CnHeading",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=14,
        leading=20,
        spaceBefore=10,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "CnBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        spaceAfter=4,
    )
    bullet_style = ParagraphStyle("CnBullet", parent=body_style, leftIndent=14, bulletIndent=4)

    def _escape(text: str) -> str:
        return escape(text.replace("\n", " ")).strip()

    story: list[object] = [
        Paragraph(_escape(title or "数据分析报告"), title_style),
        Spacer(1, 0.4 * cm),
    ]
    sections = _sections(markdown) or [("报告", markdown or "暂无内容")]
    for section_title, section_body in sections:
        story.append(Paragraph(_escape(section_title), heading_style))
        for raw in section_body.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("- ", "* ")):
                story.append(Paragraph(_escape(line[2:]), bullet_style, bulletText="•"))
            elif line.startswith("#"):
                story.append(Paragraph(_escape(line.lstrip("# ").strip()), heading_style))
            else:
                story.append(Paragraph(_escape(line), body_style))
    for path in (chart_paths or [])[:8]:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"} or not path.is_file():
            continue
        try:
            width_px, height_px = ImageReader(str(path)).getSize()
        except Exception:  # noqa: BLE001 — 单张坏图不影响整份报告
            continue
        width = 16 * cm
        height = width * height_px / width_px if width_px else width
        # 超长图（如 800×4000 截图）按 A4 可用高度等比缩回，否则 reportlab
        # 会在 document.build 抛 LayoutError 把整份导出变成 500。
        max_height = A4[1] - 6 * cm
        if height > max_height:
            width = max_height * width_px / height_px if height_px else width
            height = max_height
        try:
            image = Image(str(path), width=width, height=height)
        except Exception:  # noqa: BLE001 — 单张坏图不影响整份报告
            continue
        story.extend(
            [
                Spacer(1, 0.3 * cm),
                Paragraph(_escape(path.stem), body_style),
                image,
            ]
        )
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title or "数据分析报告",
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    document.build(story)
    return buffer.getvalue()

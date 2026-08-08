from html import escape
from pathlib import Path

from docx import Document
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "M-AI Master技术说明文档V5.1_企业Agent增强版_最新版.docx"
TARGET = SOURCE.with_suffix(".pdf")
PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT = RIGHT = 16 * mm
TOP = 18 * mm
BOTTOM = 16 * mm


def register_chinese_fonts():
    candidates = [
        ("MaiSans", Path("C:/Windows/Fonts/msyh.ttc")),
        ("MaiSans", Path("C:/Windows/Fonts/simhei.ttf")),
        ("MaiSans", Path("C:/Windows/Fonts/simsun.ttc")),
    ]
    bold_candidates = [
        ("MaiSansBold", Path("C:/Windows/Fonts/msyhbd.ttc")),
        ("MaiSansBold", Path("C:/Windows/Fonts/simhei.ttf")),
    ]
    selected_regular = None
    for name, path in candidates:
        if path.is_file():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            selected_regular = path
            break
    else:
        raise RuntimeError("未找到可用中文字体，请安装微软雅黑、黑体或宋体")
    for name, path in bold_candidates:
        if path.is_file():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            break
    else:
        pdfmetrics.registerFont(TTFont("MaiSansBold", str(selected_regular)))


def iter_blocks(document):
    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield DocxParagraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield DocxTable(child, document)


def cell_paragraph(text, style):
    value = escape(text.strip() or "-").replace("\n", "<br/>")
    return Paragraph(value, style)


def build_styles():
    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "MaiBody",
        parent=styles["BodyText"],
        fontName="MaiSans",
        fontSize=9.2,
        leading=15,
        textColor=colors.HexColor("#26364A"),
        wordWrap="CJK",
        spaceAfter=4,
    )
    return {
        "body": base,
        "title": ParagraphStyle(
            "MaiTitle", parent=base, fontName="MaiSansBold", fontSize=22,
            leading=30, alignment=TA_CENTER, textColor=colors.HexColor("#12385B"),
            spaceBefore=12, spaceAfter=18,
        ),
        "h1": ParagraphStyle(
            "MaiH1", parent=base, fontName="MaiSansBold", fontSize=16,
            leading=22, textColor=colors.HexColor("#1266A8"),
            spaceBefore=14, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "MaiH2", parent=base, fontName="MaiSansBold", fontSize=12,
            leading=18, textColor=colors.HexColor("#174B73"),
            spaceBefore=10, spaceAfter=6,
        ),
        "small": ParagraphStyle(
            "MaiSmall", parent=base, fontSize=7.2, leading=10, spaceAfter=0,
        ),
        "small_header": ParagraphStyle(
            "MaiSmallHeader", parent=base, fontName="MaiSansBold", fontSize=7.4,
            leading=10, textColor=colors.white, spaceAfter=0,
        ),
    }


def header_footer(canvas, document):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#C8D8E6"))
    canvas.line(LEFT, PAGE_HEIGHT - 12 * mm, PAGE_WIDTH - RIGHT, PAGE_HEIGHT - 12 * mm)
    canvas.setFont("MaiSans", 7.5)
    canvas.setFillColor(colors.HexColor("#60758A"))
    canvas.drawString(LEFT, PAGE_HEIGHT - 9 * mm, "M-AI Master 5.1 · 企业 Agent 增强版")
    canvas.drawRightString(PAGE_WIDTH - RIGHT, 9 * mm, f"第 {document.page} 页")
    canvas.restoreState()


def export_pdf():
    register_chinese_fonts()
    document = Document(str(SOURCE))
    styles = build_styles()
    story = []
    available_width = PAGE_WIDTH - LEFT - RIGHT

    for block in iter_blocks(document):
        if isinstance(block, DocxParagraph):
            text = block.text.strip()
            if not text:
                story.append(Spacer(1, 2.5 * mm))
                continue
            style_name = (block.style.name or "").lower()
            if "title" in style_name:
                style = styles["title"]
            elif "heading 1" in style_name or "标题 1" in style_name:
                style = styles["h1"]
            elif "heading 2" in style_name or "标题 2" in style_name:
                style = styles["h2"]
            else:
                style = styles["body"]
            if 'w:type="page"' in block._p.xml and story:
                story.append(PageBreak())
            story.append(Paragraph(escape(text).replace("\n", "<br/>"), style))
        else:
            rows = []
            for row_index, row in enumerate(block.rows):
                cell_style = styles["small_header"] if row_index == 0 else styles["small"]
                rows.append([cell_paragraph(cell.text, cell_style) for cell in row.cells])
            if not rows or not rows[0]:
                continue
            columns = len(rows[0])
            table = Table(
                rows,
                colWidths=[available_width / columns] * columns,
                repeatRows=1,
                hAlign="LEFT",
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1769AA")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F6F9FC")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B9CAD8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.extend((Spacer(1, 2 * mm), table, Spacer(1, 3 * mm)))

    pdf = BaseDocTemplate(
        str(TARGET), pagesize=A4, leftMargin=LEFT, rightMargin=RIGHT,
        topMargin=TOP, bottomMargin=BOTTOM,
        title="M-AI Master 技术说明文档 V5.1 企业 Agent 增强版",
        author="M-AI Master",
    )
    frame = Frame(LEFT, BOTTOM, available_width, PAGE_HEIGHT - TOP - BOTTOM, id="main")
    pdf.addPageTemplates(PageTemplate(id="technical", frames=[frame], onPage=header_footer))
    pdf.build(story)
    print(TARGET)


if __name__ == "__main__":
    export_pdf()

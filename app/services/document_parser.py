# 文档解析：PDF / Word / Markdown / TXT → 纯文本
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ParsedDocument:
    text: str
    page_count: int = 0
    meta: dict | None = None


class UnsupportedFileType(Exception):
    pass


# ---------------------------------------------------------------------------
# 各类型解析器
# ---------------------------------------------------------------------------
def _parse_pdf(path: Path) -> ParsedDocument:
    """优先 PyMuPDF（快、稳），失败回退 pdfplumber"""
    text_parts: list[str] = []
    page_count = 0

    try:
        import fitz  # PyMuPDF

        with fitz.open(str(path)) as doc:
            page_count = doc.page_count
            for page in doc:
                text_parts.append(page.get_text("text"))
        text = "\n".join(text_parts)
        if text.strip():
            return ParsedDocument(text=text, page_count=page_count, meta={"parser": "pymupdf"})
        logger.warning("PyMuPDF 未提取到文本，回退 pdfplumber: %s", path.name)
    except Exception as e:  # noqa: BLE001
        logger.warning("PyMuPDF 解析失败(%s)，回退 pdfplumber", e)

    # 回退
    import pdfplumber

    parts = []
    with pdfplumber.open(str(path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    text = "\n".join(parts)
    return ParsedDocument(text=text, page_count=page_count, meta={"parser": "pdfplumber"})


def _parse_docx(path: Path) -> ParsedDocument:
    """python-docx：段落 + 表格 + 页眉页脚"""
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    parts: list[str] = []

    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    # 表格按 Markdown 风格保留结构，便于模型理解
    for ti, table in enumerate(doc.tables):
        parts.append(f"\n[表格 {ti + 1}]")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            parts.append(" | ".join(cells))

    # 页眉页脚（部分文档正文在这）
    for section in doc.sections:
        for holder in (section.header, section.footer):
            for para in holder.paragraphs:
                if para.text.strip():
                    parts.append(para.text)

    return ParsedDocument(
        text="\n".join(parts),
        page_count=0,
        meta={"parser": "python-docx", "tables": len(doc.tables)},
    )


def _parse_markdown(path: Path) -> ParsedDocument:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return ParsedDocument(text=text, meta={"parser": "plain"})


def _parse_txt(path: Path) -> ParsedDocument:
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
            return ParsedDocument(text=text, meta={"parser": "plain", "encoding": enc})
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(f"无法解码文本文件: {path.name}")


def _parse_doc(path: Path) -> ParsedDocument:
    """老式 .doc —— 尝试 antiword / libreoffice，都没有则报错提示转 docx"""
    import shutil
    import subprocess

    if shutil.which("antiword"):
        out = subprocess.run(
            ["antiword", str(path)], capture_output=True, text=True, timeout=60
        )
        if out.returncode == 0 and out.stdout.strip():
            return ParsedDocument(text=out.stdout, meta={"parser": "antiword"})

    if shutil.which("libreoffice"):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "docx", "--outdir", td, str(path)],
                capture_output=True,
                timeout=180,
            )
            converted = list(Path(td).glob("*.docx"))
            if converted:
                return _parse_docx(converted[0])

    raise UnsupportedFileType(
        ".doc 格式需要 antiword 或 libreoffice 支持，建议转为 .docx 后上传"
    )


_PARSERS = {
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
    ".doc": _parse_doc,
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".txt": _parse_txt,
}


def parse_document(path: str | Path) -> ParsedDocument:
    p = Path(path)
    ext = p.suffix.lower()
    parser = _PARSERS.get(ext)
    if parser is None:
        raise UnsupportedFileType(f"不支持的文件类型: {ext}")

    logger.info("正在解析文档 %s (%s)", p.name, ext)
    result = parser(p)
    result.text = normalize_text(result.text)
    return result


# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """清洗：去多余空白、修复断行、统一换行符"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 全角空格 → 半角
    text = text.replace("\u3000", " ")
    # 压缩 3 个以上连续换行为 2 个（保留段落感）
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 行内多余空格
    text = re.sub(r"[ \t]{2,}", " ", text)
    # 去掉行首行尾空白
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
def split_text(
    text: str,
    chunk_size: int = 800,
    chunk_overlap: int = 150,
) -> list[str]:
    """
    中文友好的递归分块。
    优先在段落/句号/换行处切分，避免把句子切断。
    """
    if not text.strip():
        return []

    separators = ["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ",", " ", ""]

    chunks: list[str] = []

    def _split(segment: str, seps: list[str]) -> None:
        if len(segment) <= chunk_size:
            if segment.strip():
                chunks.append(segment.strip())
            return

        if not seps:
            # 无分隔符可用，硬切
            for i in range(0, len(segment), chunk_size - chunk_overlap):
                piece = segment[i:i + chunk_size]
                if piece.strip():
                    chunks.append(piece.strip())
            return

        sep = seps[0]
        rest = seps[1:]

        if sep == "":
            for i in range(0, len(segment), chunk_size - chunk_overlap):
                piece = segment[i:i + chunk_size]
                if piece.strip():
                    chunks.append(piece.strip())
            return

        parts = segment.split(sep)
        buffer = ""
        for part in parts:
            candidate = buffer + sep + part if buffer else part
            if len(candidate) <= chunk_size:
                buffer = candidate
            else:
                if buffer:
                    _split(buffer, rest)
                buffer = part
        if buffer:
            _split(buffer, rest)

    _split(text, separators)

    # 合并过短的块（避免碎片）
    merged: list[str] = []
    min_len = max(50, chunk_size // 10)
    for c in chunks:
        if merged and len(c) < min_len and len(merged[-1]) + len(c) <= chunk_size:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)

    return merged

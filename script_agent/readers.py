from __future__ import annotations

from pathlib import Path


class ScriptReadError(RuntimeError):
    pass


TEXT_EXTENSIONS = {".txt", ".md", ".fountain", ".screenplay"}


def read_script(path: Path) -> str:
    """Read a screenplay without sending it anywhere."""
    path = path.expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ScriptReadError("找不到剧本文件：{}".format(path))

    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return _read_text(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    raise ScriptReadError(
        "暂不支持 {}。请使用 TXT、Markdown、Fountain、PDF 或 DOCX。".format(suffix or "无扩展名文件")
    )


def _read_text(path: Path) -> str:
    errors = []
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            text = path.read_text(encoding=encoding)
            if text.strip():
                return text
        except (UnicodeError, OSError) as exc:
            errors.append("{}: {}".format(encoding, exc))
    raise ScriptReadError("无法读取文本编码：{}".format("; ".join(errors)))


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ScriptReadError("读取 PDF 需要 pypdf，请运行 python -m pip install pypdf。") from exc

    reader = PdfReader(str(path))
    pages = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            # Keep words and screenplay lines together. Plain extraction can
            # turn some PDFs into one character per line and bloat the input.
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            # Compatibility with older pypdf releases.
            text = page.extract_text() or ""
        pages.append("[PDF 第 {} 页]\n{}".format(number, text))
    result = "\n\n".join(pages).strip()
    if not result:
        raise ScriptReadError("PDF 中没有可提取文字；如果是扫描件，需要先进行 OCR。")
    return result


def _read_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise ScriptReadError("读取 DOCX 需要 python-docx，请运行 python -m pip install python-docx。") from exc

    document = Document(str(path))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    result = "\n".join(paragraphs).strip()
    if not result:
        raise ScriptReadError("DOCX 中没有可读取的正文。")
    return result

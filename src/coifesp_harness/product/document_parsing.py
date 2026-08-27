from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

_MAX_CELLS = 200_000
_MAX_ROWS = 65_536
_MAX_COLUMNS = 16_384
_MAX_SLIDES = 512
_MAX_PARAGRAPHS = 200_000


@dataclass(frozen=True, slots=True)
class DocumentLocation:
    kind: str  # paragraph | table | sheet | slide | page
    index: int
    label: str = ""


@dataclass(frozen=True, slots=True)
class DocumentTextItem:
    location: DocumentLocation
    text: str


@dataclass(frozen=True, slots=True)
class StructuredDocument:
    media_type: str
    title: str
    items: tuple[DocumentTextItem, ...] = ()
    tables: tuple[dict[str, Any], ...] = ()
    sheets: tuple[dict[str, Any], ...] = ()
    slides: tuple[dict[str, Any], ...] = ()
    pages: tuple[dict[str, Any], ...] = ()
    error_category: str | None = None

    def plain_text(self, max_chars: int = 512_000) -> str:
        parts = [self.title] if self.title else []
        for item in self.items:
            parts.append(f"[{item.location.kind}#{item.location.index}] {item.text}")
        return "\n".join(parts)[:max_chars]

    def to_json(self, max_chars: int = 1_000_000) -> str:
        payload = {
            "media_type": self.media_type,
            "title": self.title,
            "items": [
                {
                    "location": {
                        "kind": item.location.kind,
                        "index": item.location.index,
                        "label": item.location.label,
                    },
                    "text": item.text,
                }
                for item in self.items
            ],
            "error_category": self.error_category,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_chars:
            payload["items"] = payload["items"][:500]
            payload["truncated"] = True
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return encoded


def parse_document_bytes(raw: bytes, media_type: str, title: str = "") -> StructuredDocument:
    """Parse a supported office format into a bounded structured document.

    Never executes macros, formulas, external links, or embedded scripts.
    Failures are classified into a stable category; the caller keeps the
    original file available regardless of the parse outcome.
    """
    media_type = (media_type or "").lower()
    try:
        if media_type == "application/pdf" or media_type.endswith("/pdf"):
            return _parse_pdf(raw, title)
        if media_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        }:
            return _parse_docx(raw, title)
        if media_type in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "text/csv",
        }:
            return _parse_xlsx(raw, title)
        if media_type in {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-powerpoint",
        }:
            return _parse_pptx(raw, title)
        return StructuredDocument(
            media_type=media_type,
            title=title,
            error_category="unsupported_format",
        )
    except Exception:
        # Any parse failure becomes a stable category; the caller keeps the
        # original file available and never surfaces the internal stack.
        return StructuredDocument(
            media_type=media_type,
            title=title,
            error_category="parse_failed",
        )


def _parse_pdf(raw: bytes, title: str) -> StructuredDocument:
    from pypdf import PdfReader

    reader = PdfReader(__import__("io").BytesIO(raw), strict=False)
    pages = []
    items = []
    for index, page in enumerate(reader.pages):
        if index >= 1024:
            break
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        pages.append({"index": index, "text": text[:200_000]})
        if text:
            items.append(
                DocumentTextItem(
                    DocumentLocation("page", index, f"第 {index + 1} 页"),
                    text[:100_000],
                )
            )
    return StructuredDocument(
        media_type="application/pdf",
        title=title,
        items=tuple(items),
        pages=tuple(pages),
    )


def _parse_docx(raw: bytes, title: str) -> StructuredDocument:
    import io

    from docx import Document

    document = Document(io.BytesIO(raw))
    items: list[DocumentTextItem] = []
    tables: list[dict[str, Any]] = []
    body = document.element.body
    paragraph_index = 0
    table_index = 0
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            if paragraph_index >= _MAX_PARAGRAPHS:
                break
            text = "".join(node.text or "" for node in child.iter() if node.tag.endswith("}t"))
            if text.strip():
                items.append(
                    DocumentTextItem(
                        DocumentLocation("paragraph", paragraph_index),
                        text.strip(),
                    )
                )
            paragraph_index += 1
        elif tag == "tbl":
            if table_index >= 256:
                break
            rows = []
            for row in child.iter():
                if row.tag.endswith("}tr"):
                    cells = []
                    for cell in row.iter():
                        if cell.tag.endswith("}tc"):
                            cell_text = "".join(
                                node.text or ""
                                for node in cell.iter()
                                if node.tag.endswith("}t")
                            ).strip()
                            cells.append(cell_text)
                    rows.append(cells)
            tables.append({"index": table_index, "rows": rows})
            table_index += 1
    return StructuredDocument(
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        title=title,
        items=tuple(items),
        tables=tuple(tables),
    )


def _parse_xlsx(raw: bytes, title: str) -> StructuredDocument:
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(
        io.BytesIO(raw), read_only=True, data_only=False, keep_links=False
    )
    sheets = []
    items = []
    for sheet_index, sheet in enumerate(workbook.worksheets):
        if sheet_index >= 64:
            break
        rows = []
        total_cells = 0
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > _MAX_ROWS:
                break
            values = []
            for cell_index, value in enumerate(row, start=1):
                if cell_index > _MAX_COLUMNS:
                    break
                if value is None:
                    values.append(None)
                    continue
                total_cells += 1
                if total_cells > _MAX_CELLS:
                    break
                values.append(_cell_text(value))
            rows.append(values)
            if total_cells > _MAX_CELLS:
                break
        sheets.append({"index": sheet_index, "name": sheet.title, "rows": rows})
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            if row_index > 1000:
                break
            for col_index, value in enumerate(row, start=1):
                if value is None:
                    continue
                items.append(
                    DocumentTextItem(
                        DocumentLocation(
                            "sheet",
                            sheet_index,
                            f"{sheet.title}!{col_index}:{row_index}",
                        ),
                        _cell_text(value)[:2_000],
                    )
                )
                if len(items) >= 20_000:
                    break
            if len(items) >= 20_000:
                break
        if len(items) >= 20_000:
            break
    workbook.close()
    return StructuredDocument(
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        title=title,
        items=tuple(items),
        sheets=tuple(sheets),
    )


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse_pptx(raw: bytes, title: str) -> StructuredDocument:
    import io

    from pptx import Presentation

    presentation = Presentation(io.BytesIO(raw))
    slides = []
    items = []
    for slide_index, slide in enumerate(presentation.slides):
        if slide_index >= _MAX_SLIDES:
            break
        texts = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs).strip()
                if text:
                    texts.append(text)
        slides.append({"index": slide_index, "texts": texts})
        for text in texts:
            items.append(
                DocumentTextItem(
                    DocumentLocation("slide", slide_index, f"第 {slide_index + 1} 页"),
                    text[:5_000],
                )
            )
    return StructuredDocument(
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        title=title,
        items=tuple(items),
        slides=tuple(slides),
    )

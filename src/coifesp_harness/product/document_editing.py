from __future__ import annotations

import io
import json
from typing import Any

_MAX_MODIFICATION_BYTES = 1_000_000


class DocumentModificationError(ValueError):
    """A modification request violates the strict document-change protocol."""


def validate_modification(modification: dict[str, Any]) -> dict[str, Any]:
    """Validate a strict structured modification; unknown fields are rejected."""
    if not isinstance(modification, dict):
        raise DocumentModificationError("modification must be an object")
    if set(modification) != {"schema", "format", "operations"}:
        raise DocumentModificationError("modification fields are invalid")
    if modification.get("schema") != "coifesp.document-modification.v1":
        raise DocumentModificationError("modification schema is invalid")
    document_format = modification.get("format")
    if document_format not in {"docx", "xlsx", "pptx", "pdf"}:
        raise DocumentModificationError("modification format is unsupported")
    operations = modification.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 500:
        raise DocumentModificationError("modification operations are invalid")
    if len(json.dumps(modification, ensure_ascii=False).encode("utf-8")) > _MAX_MODIFICATION_BYTES:
        raise DocumentModificationError("modification exceeds the size limit")
    validated = []
    for index, operation in enumerate(operations):
        validated.append(_validate_operation(document_format, operation, index))
    return {
        "schema": "coifesp.document-modification.v1",
        "format": document_format,
        "operations": validated,
    }


def _validate_operation(document_format: str, operation: Any, index: int) -> dict[str, Any]:
    if not isinstance(operation, dict):
        raise DocumentModificationError(f"operation {index} must be an object")
    allowed: dict[str, set[str]] = {
        "docx": {"replace_paragraph", "insert_paragraph", "update_table_cell"},
        "xlsx": {"update_cell"},
        "pptx": {"update_slide_text"},
        "pdf": {"add_review_note"},
    }
    op = operation.get("op")
    if op not in allowed[document_format]:
        raise DocumentModificationError(f"operation {index} is unsupported for {document_format}")
    if document_format == "docx":
        if op == "replace_paragraph":
            _require(operation, {"op", "paragraph_index", "text"}, index)
            _require_int(operation["paragraph_index"], 0, 1_000_000, "paragraph_index")
            _require_text(operation["text"], index)
        elif op == "insert_paragraph":
            _require(operation, {"op", "after_paragraph_index", "text"}, index)
            _require_int(operation["after_paragraph_index"], -1, 1_000_000, "after_paragraph_index")
            _require_text(operation["text"], index)
        else:  # update_table_cell
            _require(operation, {"op", "table_index", "row", "column", "text"}, index)
            for key in ("table_index", "row", "column"):
                _require_int(operation[key], 0, 1_000_000, key)
            _require_text(operation["text"], index)
    elif document_format == "xlsx":
        _require(operation, {"op", "sheet", "row", "column", "value"}, index)
        if not isinstance(operation["sheet"], str) or not 1 <= len(operation["sheet"]) <= 128:
            raise DocumentModificationError(f"operation {index} sheet is invalid")
        _require_int(operation["row"], 1, 1_048_576, "row")
        _require_int(operation["column"], 1, 16_384, "column")
        if not isinstance(operation["value"], (str, int, float, bool)) or isinstance(
            operation["value"], bool
        ):
            raise DocumentModificationError(f"operation {index} value is invalid")
    elif document_format == "pptx":
        _require(operation, {"op", "slide_index", "text"}, index)
        _require_int(operation["slide_index"], 0, 4096, "slide_index")
        _require_text(operation["text"], index)
    else:  # pdf: only review notes, never mutate the original
        _require(operation, {"op", "page_index", "note"}, index)
        _require_int(operation["page_index"], 0, 4096, "page_index")
        _require_text(operation["note"], index)
    return dict(operation)


def _require(operation: dict[str, Any], fields: set[str], index: int) -> None:
    if set(operation) != fields:
        raise DocumentModificationError(f"operation {index} fields are invalid")


def _require_int(value: Any, low: int, high: int, name: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise DocumentModificationError(f"{name} is out of bounds")


def _require_text(value: Any, index: int) -> None:
    if not isinstance(value, str) or not 0 < len(value.encode("utf-8")) <= 200_000:
        raise DocumentModificationError(f"operation {index} text is invalid")


def apply_modification(
    raw: bytes,
    media_type: str,
    modification: dict[str, Any],
) -> tuple[bytes, str]:
    """Apply a validated modification and return (new_bytes, new_media_type).

    PDF documents are never mutated: the service generates a review report
    instead, so callers route PDF modifications to ``render_pdf_review``.
    """
    document_format = modification["format"]
    if document_format == "pdf":
        raise DocumentModificationError("PDF documents cannot be modified in place")
    if document_format == "docx":
        return _apply_docx(raw, modification), _docx_media_type(media_type)
    if document_format == "xlsx":
        return _apply_xlsx(raw, modification), _xlsx_media_type(media_type)
    return _apply_pptx(raw, modification), _pptx_media_type(media_type)


def render_pdf_review(modification: dict[str, Any]) -> tuple[bytes, str]:
    """PDF modifications produce a human-readable review report, never a new PDF."""
    lines = [
        "COIFESP 文档审阅报告",
        "修改协议：coifesp.document-modification.v1",
        "",
    ]
    for operation in modification["operations"]:
        lines.append(
            f"- 第 {operation['page_index'] + 1} 页：{operation['note']}"
        )
    body = "\n".join(lines)
    return body.encode("utf-8"), "text/markdown"


def _docx_media_type(media_type: str) -> str:
    return (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if not media_type or "docx" in media_type
        else media_type
    )


def _xlsx_media_type(media_type: str) -> str:
    return (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if not media_type or "xlsx" in media_type
        else media_type
    )


def _pptx_media_type(media_type: str) -> str:
    return (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if not media_type or "pptx" in media_type
        else media_type
    )


def _apply_docx(raw: bytes, modification: dict[str, Any]) -> bytes:
    from docx import Document

    document = Document(io.BytesIO(raw))
    paragraphs = list(document.paragraphs)
    for operation in modification["operations"]:
        op = operation["op"]
        if op == "replace_paragraph":
            target = paragraphs[operation["paragraph_index"]]
            _replace_run_text(target, operation["text"])
        elif op == "insert_paragraph":
            after = paragraphs[operation["after_paragraph_index"]]
            new_paragraph = after.insert_paragraph_before()
            _replace_run_text(new_paragraph, operation["text"])
        else:  # update_table_cell
            table = document.tables[operation["table_index"]]
            cell = table.cell(operation["row"], operation["column"])
            _replace_run_text(cell.paragraphs[0], operation["text"])
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _replace_run_text(paragraph, text: str) -> None:
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _apply_xlsx(raw: bytes, modification: dict[str, Any]) -> bytes:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), data_only=False)
    for operation in modification["operations"]:
        sheet = workbook[operation["sheet"]]
        sheet.cell(row=operation["row"], column=operation["column"], value=operation["value"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _apply_pptx(raw: bytes, modification: dict[str, Any]) -> bytes:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(raw))
    for operation in modification["operations"]:
        slide = presentation.slides[operation["slide_index"]]
        _set_first_text(slide, operation["text"])
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _set_first_text(slide, text: str) -> None:
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        first = shape.text_frame.paragraphs[0]
        if first.runs:
            first.runs[0].text = text
            for run in first.runs[1:]:
                run.text = ""
        else:
            first.add_run().text = text
        return

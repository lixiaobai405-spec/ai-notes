"""Freeze original PDF page identities; never accept storage keys from the browser."""
from sqlalchemy import select

from app.modules.files.models import File, FileVariant, NotePage, PdfImport
from app.modules.notes.models import Note
from app.modules.ai import schemas as s
from app.modules.ai.image_context import read_bounded_jpeg
from app.shared.errors import AppError

BATCH_PAGES = 8
BATCH_BYTES = 12 * 1024 * 1024
PAGE_BYTES = 8 * 1024 * 1024
TOTAL_BYTES = 128 * 1024 * 1024
GROUP = 4


def notebook(db, owner, note_id):
    note = db.scalar(select(Note).where(Note.id == note_id, Note.owner_id == owner, Note.deleted_at.is_(None)))
    item = db.scalar(select(PdfImport).where(PdfImport.note_id == note_id, PdfImport.owner_id == owner))
    if not note or not item:
        raise AppError(404, "pdf_not_found", "PDF 不存在或无权访问")
    if item.status != "ready" or item.page_count != item.completed_pages:
        raise AppError(409, "pdf_not_ready", "请先完成 PDF 的全部页面转换")
    if not item.page_count or item.page_count > 200:
        raise AppError(422, "pdf_context_too_large", "本阶段 PDF 对话最多支持 200 页")
    return note, item


def pages(db, owner, note_id):
    return list(db.execute(select(NotePage, File, FileVariant).join(File, NotePage.file_id == File.id)
                          .join(FileVariant, FileVariant.file_id == File.id).where(
                              NotePage.owner_id == owner, NotePage.note_id == note_id,
                              NotePage.pdf_page_number.is_not(None), File.owner_id == owner,
                              File.status == "ready", FileVariant.kind == "reading")
                          .order_by(NotePage.pdf_page_number)))


def detail(db, owner, note_id):
    note, item = notebook(db, owner, note_id)
    ready = pages(db, owner, note_id)
    if [p.pdf_page_number for p, _, _ in ready] != list(range(1, item.page_count + 1)):
        raise AppError(409, "pdf_pages_unavailable", "原 PDF 页面不完整，不能声明已读取整本；请重新导入原件")
    return s.PdfNotebookDetail(id=note.id, title=note.title, revision=note.revision, page_count=item.page_count,
                              pages=[s.PdfNotebookPage(page_id=p.id, number=p.pdf_page_number, file_id=f.id, bytes=v.bytes) for p, f, v in ready])


def batches(source):
    result, batch, size = [], [], 0
    for page in source["pages"]:
        if batch and (len(batch) >= BATCH_PAGES or size + page["bytes"] > BATCH_BYTES):
            result.append(batch)
            batch, size = [], 0
        batch.append(page)
        size += page["bytes"]
    if batch:
        result.append(batch)
    return result


def planned_calls(count):
    if count == 1:
        return 1
    total = count + 1  # Reads plus final answer.
    while count > GROUP:
        total += count // GROUP + (1 if count % GROUP > 1 else 0)
        count = (count + GROUP - 1) // GROUP
    return total


def _freeze(db, owner, data):
    book = detail(db, owner, data.note_id)
    if book.revision != data.revision:
        raise AppError(409, "pdf_changed", "PDF 笔记已更新，请刷新资料范围后再发送")
    selected = set(data.page_numbers) if data.mode in {"pages", "region"} else set(range(1, book.page_count + 1))
    if not selected or not selected.issubset({p.number for p in book.pages}):
        raise AppError(422, "pdf_page_invalid", "选择的页码超出原 PDF 范围")
    frozen = [{"page_id": str(p.id), "number": p.pdf_page_number, "file_id": str(f.id),
               "object_key": v.object_key, "bytes": v.bytes} for p, f, v in pages(db, owner, data.note_id)
              if p.pdf_page_number in selected]
    if any(p["bytes"] > PAGE_BYTES for p in frozen) or sum(p["bytes"] for p in frozen) > TOTAL_BYTES:
        raise AppError(422, "pdf_context_too_large", "页图超过本次处理大小上限，请选择更少页面；尚未调用模型")
    source = {"note_id": str(book.id), "title": book.title, "revision": book.revision,
              "mode": data.mode, "pages": frozen}
    dimensions = None
    if data.mode == "region":
        frozen[0]["region"] = data.region.model_dump()
        # Only the chosen page is decoded. Whole-page/all preview remains a
        # metadata-only operation, including for a 200-page notebook.
        dimensions = read_region(source)[1:]
    return source, dimensions


def freeze(db, owner, data):
    return _freeze(db, owner, data)[0]


def validate(db, owner, source):
    try:
        public = s.PdfSourceOut.model_validate(source)
        note, item = notebook(db, owner, public.note_id)
        if public.mode == "all" and {p.number for p in public.pages} != set(range(1, item.page_count + 1)):
            raise ValueError("invalid_full_pdf_source")
        actual = {str(p.id): (str(f.id), p.pdf_page_number, v.object_key, v.bytes) for p, f, v in pages(db, owner, note.id)}
        for p in source["pages"]:
            if type(p["bytes"]) is not int or p["bytes"] <= 0 or actual.get(p["page_id"]) != (
                    p["file_id"], p["number"], p["object_key"], p["bytes"]):
                raise ValueError("pdf_identity_changed")
        return note
    except (KeyError, TypeError, ValueError):
        raise AppError(409, "pdf_source_unavailable", "本次引用的 PDF 页面已失效，停止继续调用模型") from None


def read_region(source):
    try:
        public = s.PdfSourceOut.model_validate(source)
        if public.mode != "region":
            raise ValueError("pdf_region_required")
        return read_bounded_jpeg(source["pages"][0], public.pages[0].region, error_prefix="pdf")
    except (KeyError, TypeError, ValueError):
        raise AppError(409, "pdf_source_unavailable", "本次引用的 PDF 选区已失效，停止继续调用模型") from None


def preview(db, owner, data):
    source, dimensions = _freeze(db, owner, data)
    groups = batches(source)
    return s.PdfPreviewOut(source=s.PdfSourceOut.model_validate(source),
                           batches=[[p["number"] for p in group] for group in groups], planned_calls=planned_calls(len(groups)),
                           **(dict(zip(("width", "height", "crop_width", "crop_height"), dimensions, strict=True)) if dimensions else {}))

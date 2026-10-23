"""Freeze private image identities and crop only the bounded reading variant in memory."""
import io
import math
import warnings

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select

from app.modules.ai import image_schemas as s
from app.modules.files import service as files
from app.modules.files.models import File, FileVariant, NotePage
from app.modules.files.oss_storage import StorageUnavailable
from app.modules.notes.models import Note
from app.shared.errors import AppError

PAGE_LIMIT = 200
PAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 2400 * 2400
MIN_EDGE = 8


def notebook(db, owner, note_id):
    note = db.scalar(select(Note).where(Note.id == note_id, Note.owner_id == owner,
                                       Note.kind == "image", Note.deleted_at.is_(None)))
    if not note:
        raise AppError(404, "image_not_found", "图片笔记不存在或无权访问")
    return note


def pages(db, owner, note_id, page_id=None):
    query = (select(NotePage, File, FileVariant)
                          .join(File, NotePage.file_id == File.id)
                          .join(FileVariant, FileVariant.file_id == File.id).where(
                              NotePage.owner_id == owner, NotePage.note_id == note_id,
                              NotePage.pdf_page_number.is_(None), File.owner_id == owner,
                              File.status == "ready", FileVariant.kind == "reading"))
    if page_id is not None:
        query = query.where(NotePage.id == page_id)
    return list(db.execute(query.order_by(NotePage.position).limit(1 if page_id is not None else PAGE_LIMIT + 1)))


def detail(db, owner, note_id):
    note = notebook(db, owner, note_id)
    count = db.scalar(select(func.count()).select_from(NotePage).where(
        NotePage.owner_id == owner, NotePage.note_id == note_id,
        NotePage.pdf_page_number.is_(None)))
    if count > PAGE_LIMIT:
        raise AppError(422, "image_context_too_large", "本阶段图片提问最多支持每本 200 张图片，请拆分笔记后选择")
    ready = pages(db, owner, note_id)
    if not ready:
        raise AppError(409, "image_pages_unavailable", "没有可用的普通图片，请先完成图片上传和处理")
    return s.ImageNotebookDetail(id=note.id, title=note.title, revision=note.revision,
                                 page_count=len(ready), pages=[s.ImageNotebookPage(
                                     page_id=p.id, number=p.position + 1, file_id=f.id,
                                     page_revision=p.revision, bytes=v.bytes) for p, f, v in ready])


def _freeze(db, owner, data):
    book = detail(db, owner, data.note_id)
    if book.revision != data.revision:
        raise AppError(409, "image_changed", "图片笔记已更新，请刷新图片和选区后再发送")
    selected = next(iter(pages(db, owner, book.id, data.page_id)), None)
    if not selected:
        raise AppError(422, "image_page_invalid", "选择的普通图片不存在或尚未处理完成")
    page, file, variant = selected
    region = data.region.model_dump() if data.region else None
    source = {"kind": "image", "note_id": str(book.id), "title": book.title,
              "revision": book.revision, "mode": "region" if region else "page", "pages": [{
                  "page_id": str(page.id), "number": page.position + 1,
                  "page_revision": page.revision, "file_id": str(file.id),
                  "object_key": variant.object_key, "bytes": variant.bytes, "region": region}]}
    # Decode and measure before queuing anything that could incur a model charge.
    return source, _read(source)


def freeze(db, owner, data):
    return _freeze(db, owner, data)[0]


def validate(db, owner, source):
    try:
        public = s.ImageSourceOut.model_validate(source)
        if len(public.pages) != 1 or (public.mode == "region") != (public.pages[0].region is not None):
            raise ValueError("invalid_image_source")
        frozen = source["pages"][0]
        note = notebook(db, owner, public.note_id)
        actual = next(iter(pages(db, owner, note.id, public.pages[0].page_id)), None)
        # Position and page revision may change when the owner reorders pages.
        # The saved original position is a citation label, never a lookup key.
        if not actual or (str(actual[1].id), actual[2].object_key, actual[2].bytes) != (
                frozen["file_id"], frozen["object_key"], frozen["bytes"]):
            raise ValueError("image_identity_changed")
        return note
    except (KeyError, TypeError, ValueError):
        raise AppError(409, "image_source_unavailable", "本次引用的图片已失效，停止继续调用模型") from None


def _read(source):
    try:
        if source["kind"] != "image" or len(source["pages"]) != 1:
            raise ValueError("invalid_image_source")
        page = source["pages"][0]
        expected = page["bytes"]
        if type(expected) is not int or expected <= 0:
            raise ValueError("invalid_image_size")
        if expected > PAGE_BYTES:
            raise AppError(422, "image_context_too_large", "图片超过 8 MiB 处理上限，尚未调用模型")
        region = s.ImageRegion.model_validate(page["region"]) if page.get("region") is not None else None
        if source["mode"] != ("region" if region else "page"):
            raise ValueError("invalid_image_region")
        storage = files.storage()
        if storage.head(page["object_key"]).size != expected:
            raise ValueError("image_size_changed")
        data = storage.read(page["object_key"])
        if len(data) != expected:
            raise ValueError("image_size_changed")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "JPEG" or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("reading_variant_must_be_jpeg")
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise AppError(422, "image_context_too_large", "图片像素超过处理上限，尚未调用模型")
                image.verify()
            # verify checks structure; load also catches truncated scan data.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                bounds = (0, 0, width, height) if region is None else (
                    math.floor(region.x * width), math.floor(region.y * height),
                    math.ceil((region.x + region.width) * width),
                    math.ceil((region.y + region.height) * height))
                crop_width, crop_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                if min(crop_width, crop_height) < MIN_EDGE:
                    raise AppError(422, "image_region_too_small", "选区至少需要 8 × 8 像素，请扩大选区后重试")
                if region is not None:
                    output = io.BytesIO()
                    image.crop(bounds).convert("RGB").save(output, format="JPEG", quality=88, optimize=True)
                    data = output.getvalue()
                if len(data) > PAGE_BYTES:
                    raise AppError(422, "image_context_too_large", "图片选区超过 8 MiB 处理上限，尚未调用模型")
                return data, width, height, crop_width, crop_height
    except StorageUnavailable:
        raise AppError(503, "image_storage_unavailable", "图片存储暂不可用，请稍后重试") from None
    except (KeyError, TypeError, ValueError, UnidentifiedImageError, FileNotFoundError,
            OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AppError(409, "image_source_unavailable", "图片读取失败或内容已损坏，尚未调用模型") from None


def read_image(source):
    return _read(source)[0]


def preview(db, owner, data):
    source, (_, width, height, crop_width, crop_height) = _freeze(db, owner, data)
    return s.ImagePreviewOut(source=s.ImageSourceOut.model_validate(source), width=width, height=height,
                             crop_width=crop_width, crop_height=crop_height)

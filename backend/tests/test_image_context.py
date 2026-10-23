"""Image input/crop regressions, also runnable with pytest --noconftest (no DB)."""
import io
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
from pydantic import ValidationError
import pytest

from app.modules.ai import image_context as context
from app.modules.ai.image_schemas import ImageInput, ImageRegion, ImageSourceOut
from app.modules.files.oss_storage import StorageUnavailable
from app.shared.errors import AppError


class MemoryStorage:
    def __init__(self, data):
        self.data = data
        self.writes = 0
        self.reads = 0

    def head(self, _):
        return SimpleNamespace(size=len(self.data))

    def read(self, _):
        self.reads += 1
        return self.data

    def put(self, *_):
        self.writes += 1
        raise AssertionError("Crops must remain in memory")


def image_bytes(size=(103, 81), fmt="JPEG"):
    # The selected center differs visibly from the surrounding image.
    image = Image.new("RGB", size, "white")
    image.paste((20, 30, 40), (0, 0, size[0] // 2, size[1] // 2))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def frozen(monkeypatch, *, data=None, region=None):
    data = image_bytes() if data is None else data
    storage = MemoryStorage(data)
    monkeypatch.setattr(context.files, "storage", lambda: storage)
    source = {"kind": "image", "note_id": str(uuid4()), "title": "普通图片",
              "revision": 3, "mode": "region" if region else "page", "pages": [{
                  "page_id": str(uuid4()), "file_id": str(uuid4()), "page_revision": 2,
                  "number": 4, "object_key": "private/reading.jpg", "bytes": len(data), "region": region}]}
    return source, storage


@pytest.mark.parametrize("field,value", [
    ("x", True), ("y", "0.2"), ("width", None), ("height", float("nan")),
    ("x", float("inf")), ("height", float("-inf")), ("x", -0.1), ("y", 1.0),
    ("width", 0), ("height", 1.1), ("width", 0.9), ("height", 0.9),
])
def test_regions_reject_non_numeric_nonfinite_and_outside_values(field, value):
    data = {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5, field: value}
    with pytest.raises(ValidationError):
        ImageRegion.model_validate(data)


def test_image_input_accepts_whole_page_and_rejects_storage_details_and_bad_revision():
    data = {"note_id": str(uuid4()), "revision": 1, "page_id": str(uuid4())}
    assert ImageInput.model_validate(data).region is None
    for change in ({"object_key": "private/other.jpg"}, {"file_id": str(uuid4())},
                   {"url": "https://example.com"}, {"revision": True}, {"revision": "1"}, {"revision": 0}):
        with pytest.raises(ValidationError):
            ImageInput.model_validate({**data, **change})
    with pytest.raises(ValidationError):
        ImageRegion.model_validate({"x": 0, "y": 0, "width": 1, "height": 1, "file_id": str(uuid4())})


def test_whole_image_is_original_verified_jpeg_and_public_source_has_no_secrets(monkeypatch):
    source, storage = frozen(monkeypatch)
    assert context.read_image(source) == storage.data
    safe = ImageSourceOut.model_validate(source).model_dump(mode="json")
    assert safe["pages"][0] == {"page_id": source["pages"][0]["page_id"], "number": 4, "region": None}
    assert "object_key" not in str(safe) and "file_id" not in str(safe)
    assert storage.writes == 0


def test_crop_uses_actual_reading_dimensions_and_floor_ceil_and_stays_private(monkeypatch):
    region = {"x": 0.123, "y": 0.111, "width": 0.222, "height": 0.333}
    source, storage = frozen(monkeypatch, region=region)
    crop = context.read_image(source)
    with Image.open(io.BytesIO(crop)) as image:
        assert image.format == "JPEG" and image.size == (24, 28)
        assert all(c < 80 for c in image.getpixel((5, 5)))
    assert crop != storage.data and storage.writes == 0


def test_full_bounds_region_remains_valid_and_exactly_eight_pixel_crop_is_allowed(monkeypatch):
    source, _ = frozen(monkeypatch, region={"x": 0, "y": 0, "width": 1, "height": 1})
    assert context._read(source)[1:] == (103, 81, 103, 81)
    source, _ = frozen(monkeypatch, data=image_bytes((80, 80)),
                       region={"x": 0, "y": 0, "width": 0.1, "height": 0.1})
    assert context._read(source)[3:] == (8, 8)


def test_tiny_crop_rejected_before_any_model_or_storage_write(monkeypatch):
    source, storage = frozen(monkeypatch, region={"x": 0, "y": 0, "width": 0.01, "height": 0.5})
    with pytest.raises(AppError) as error:
        context.read_image(source)
    assert error.value.status == 422 and error.value.code == "image_region_too_small"
    assert storage.writes == 0


@pytest.mark.parametrize("corruption", ["png", "junk", "truncated", "size", "missing"])
def test_corrupt_or_missing_reading_variant_is_never_returned(monkeypatch, corruption):
    data = image_bytes(fmt="PNG") if corruption == "png" else b"junk" if corruption == "junk" else image_bytes()
    if corruption == "truncated":
        data = data[:len(data) // 2]
    source, storage = frozen(monkeypatch, data=data)
    if corruption == "size":
        source["pages"][0]["bytes"] += 1
    if corruption == "missing":
        def missing(_):
            raise FileNotFoundError("private object path must not leak")
        storage.head = missing
    with pytest.raises(AppError) as error:
        context.read_image(source)
    assert error.value.code == "image_source_unavailable"
    assert "private" not in error.value.message


def test_byte_and_pixel_limits_apply_before_crop(monkeypatch):
    source, _ = frozen(monkeypatch)
    monkeypatch.setattr(context, "PAGE_BYTES", source["pages"][0]["bytes"] - 1)
    with pytest.raises(AppError) as error:
        context.read_image(source)
    assert error.value.code == "image_context_too_large"
    monkeypatch.setattr(context, "PAGE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(context, "MAX_PIXELS", 100)
    with pytest.raises(AppError) as error:
        context.read_image(source)
    assert error.value.code == "image_context_too_large"


def test_storage_outage_is_sanitized(monkeypatch):
    source, _ = frozen(monkeypatch)
    def unavailable():
        raise StorageUnavailable()
    monkeypatch.setattr(context.files, "storage", unavailable)
    with pytest.raises(AppError) as error:
        context.read_image(source)
    assert error.value.status == 503 and error.value.code == "image_storage_unavailable"


def test_freeze_revision_preview_dimensions_and_history_survive_reordering(monkeypatch):
    source, storage = frozen(monkeypatch, region={"x": 0.123, "y": 0.111, "width": 0.222, "height": 0.333})
    page = source["pages"][0]
    from uuid import UUID
    book = SimpleNamespace(id=UUID(source["note_id"]), title=source["title"], revision=3)
    actual_page = SimpleNamespace(id=UUID(page["page_id"]), position=3, revision=2)
    actual_file = SimpleNamespace(id=UUID(page["file_id"]))
    actual_variant = SimpleNamespace(object_key=page["object_key"], bytes=page["bytes"])
    monkeypatch.setattr(context, "detail", lambda *_: book)
    monkeypatch.setattr(context, "notebook", lambda *_: book)
    monkeypatch.setattr(context, "pages", lambda *_: [(actual_page, actual_file, actual_variant)])
    data = ImageInput(note_id=book.id, revision=3, page_id=actual_page.id, region=page["region"])
    preview = context.preview(None, uuid4(), data)
    assert (preview.width, preview.height, preview.crop_width, preview.crop_height, preview.planned_calls) == (103, 81, 24, 28, 1)
    assert storage.reads == 1
    snapshot = context.freeze(None, uuid4(), data)
    assert snapshot["pages"][0]["number"] == 4
    actual_page.position, actual_page.revision = 205, 7
    book.revision = 8
    assert context.validate(None, uuid4(), snapshot) is book
    assert snapshot["pages"][0]["number"] == 4
    with pytest.raises(AppError) as error:
        context.freeze(None, uuid4(), data)
    assert error.value.code == "image_changed"
    actual_variant.object_key = "private/replaced.jpg"
    with pytest.raises(AppError) as error:
        context.validate(None, uuid4(), snapshot)
    assert error.value.code == "image_source_unavailable"

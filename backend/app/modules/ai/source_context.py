"""Dispatch frozen visual sources while keeping legacy PDF snapshots readable."""
from app.modules.ai import image_context, pdf_context, schemas as s


def is_image(source):
    return bool(source and source.get("kind") == "image")


def validate(db, owner, source):
    return (image_context if is_image(source) else pdf_context).validate(db, owner, source)


def public_source(source):
    if not source:
        return None
    schema = s.ImageSourceOut if is_image(source) else s.PdfSourceOut
    return schema.model_validate(source)


def region_text(region):
    if not region:
        return "整页"
    return "选区：左 {:.2f}%、上 {:.2f}%、宽 {:.2f}%、高 {:.2f}%".format(
        *(region[name] * 100 for name in ("x", "y", "width", "height")))

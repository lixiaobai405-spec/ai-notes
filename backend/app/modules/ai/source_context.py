"""Dispatch private frozen sources while keeping legacy PDF snapshots readable."""
from app.modules.ai import image_context, pdf_context, text_context, schemas as s


def is_image(source):
    return isinstance(source, dict) and source.get("kind") == "image"


def is_text(source):
    return isinstance(source, dict) and source.get("kind") == "text"


def kind(source):
    if not isinstance(source, dict) or source.get("kind", "pdf") not in {"text", "image", "pdf"}:
        raise ValueError("invalid_source_kind")
    return source.get("kind", "pdf")


def is_visual(source):
    return source is not None and kind(source) in {"image", "pdf"}


def validate(db, owner, source):
    implementation = {"image": image_context, "pdf": pdf_context, "text": text_context}[kind(source)]
    return implementation.validate(db, owner, source)


def public_source(source):
    if source is None:
        return None
    schema = {"image": s.ImageSourceOut, "pdf": s.PdfSourceOut, "text": s.TextSourceOut}[kind(source)]
    return schema.model_validate(source)


def region_text(region):
    if not region:
        return "整页"
    return "选区：左 {:.2f}%、上 {:.2f}%、宽 {:.2f}%、高 {:.2f}%".format(
        *(region[name] * 100 for name in ("x", "y", "width", "height")))

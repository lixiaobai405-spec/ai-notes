"""Public image references and strictly bounded normalized crop coordinates."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImageRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float = Field(strict=True, allow_inf_nan=False, ge=0, lt=1)
    y: float = Field(strict=True, allow_inf_nan=False, ge=0, lt=1)
    width: float = Field(strict=True, allow_inf_nan=False, gt=0, le=1)
    height: float = Field(strict=True, allow_inf_nan=False, gt=0, le=1)

    @model_validator(mode="after")
    def within_image(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("The selected region must remain inside the image")
        return self


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: UUID
    revision: int = Field(strict=True, ge=1)
    page_id: UUID
    region: ImageRegion | None = None


class ImagePageRef(BaseModel):
    page_id: UUID
    number: int
    region: ImageRegion | None = None


class ImageSourceOut(BaseModel):
    # Storage keys and file IDs belong only to the server-side frozen snapshot.
    kind: Literal["image"]
    note_id: UUID
    title: str
    revision: int
    mode: Literal["page", "region"]
    pages: list[ImagePageRef]


class ImageNotebookOut(BaseModel):
    id: UUID
    title: str
    revision: int
    page_count: int


class ImageNotebookPage(ImagePageRef):
    file_id: UUID
    page_revision: int
    bytes: int


class ImageNotebookDetail(ImageNotebookOut):
    pages: list[ImageNotebookPage]


class ImagePreviewOut(BaseModel):
    source: ImageSourceOut
    width: int
    height: int
    crop_width: int
    crop_height: int
    planned_calls: Literal[1] = 1

from typing import Literal
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, computed_field, field_validator, model_validator
from app.modules.ai.answer_format import answer_document
from app.modules.ai.image_schemas import ImageInput, ImageRegion, ImageSourceOut
from app.modules.ai.text_schemas import TextInput, TextSourceOut, TextSourceReadOut
from app.modules.notes.schemas import Input, Title


class AiModelOut(BaseModel):
    id: str
    name: str
    supports_text: bool
    supports_vision: bool
    supports_streaming: bool
    supports_embedding: bool


class AiProviderOut(BaseModel):
    id: str
    name: str
    models: list[AiModelOut]


class AiCatalogOut(BaseModel):
    credential_source: Literal["shared"] = "shared"
    platform_enabled: bool
    providers: list[AiProviderOut]


class AiConnectionOut(BaseModel):
    status: Literal["valid", "invalid", "unavailable"]
    code: str
    available_models: list[str]


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    created_at: datetime


class PdfInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: UUID
    revision: int = Field(strict=True, ge=1)
    mode: Literal["pages", "all", "region"]
    page_numbers: list[StrictInt] = Field(default_factory=list, max_length=200)
    region: ImageRegion | None = None

    @model_validator(mode="after")
    def valid_pages(self):
        if self.mode == "all" and self.page_numbers or self.mode == "pages" and not self.page_numbers:
            raise ValueError("Choose all pages or a nonempty explicit selection")
        if self.mode == "region":
            if len(self.page_numbers) != 1 or self.region is None:
                raise ValueError("A PDF region requires exactly one page and a rectangle")
        elif self.region is not None:
            raise ValueError("A rectangle is only allowed in PDF region mode")
        if any(type(n) is not int or not 1 <= n <= 200 for n in self.page_numbers) or len(set(self.page_numbers)) != len(self.page_numbers):
            raise ValueError("Page numbers must be unique integers between 1 and 200")
        return self


class PdfPageRef(BaseModel):
    page_id: UUID
    number: StrictInt = Field(ge=1, le=200)


class PdfSourcePageRef(PdfPageRef):
    region: ImageRegion | None = None


class PdfSourceOut(BaseModel):
    # Intentionally excludes internal object keys, file IDs and storage metadata.
    kind: Literal["pdf"] = "pdf"
    note_id: UUID
    revision: int
    title: str
    mode: Literal["pages", "all", "region"]
    pages: list[PdfSourcePageRef] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_region(self):
        if self.mode == "region":
            if len(self.pages) != 1 or self.pages[0].region is None:
                raise ValueError("A PDF region source requires exactly one page and rectangle")
        elif any(page.region is not None for page in self.pages):
            raise ValueError("Whole PDF pages cannot have rectangles")
        if (any(type(page.number) is not int or not 1 <= page.number <= 200 for page in self.pages)
                or len({page.page_id for page in self.pages}) != len(self.pages)
                or len({page.number for page in self.pages}) != len(self.pages)):
            raise ValueError("PDF page references must be unique original pages")
        return self


class PdfProgress(BaseModel):
    stage: Literal["queued", "reading", "reducing", "answering", "done"]
    completed_pages: int
    total_pages: int
    completed_batches: int
    total_batches: int
    call_count: int
    planned_calls: int
    current_pages: list[int] = Field(default_factory=list)
    usage_complete: bool = False


class PdfNotebookOut(BaseModel):
    id: UUID
    title: str
    revision: int
    page_count: int


class PdfNotebookPage(PdfPageRef):
    file_id: UUID
    bytes: int


class PdfNotebookDetail(PdfNotebookOut):
    pages: list[PdfNotebookPage]


class PdfPreviewOut(BaseModel):
    source: PdfSourceOut
    batches: list[list[int]]
    planned_calls: int
    width: int | None = None
    height: int | None = None
    crop_width: int | None = None
    crop_height: int | None = None


class SourcePageOut(BaseModel):
    file_id: UUID
    title: str
    page_number: int
    source_revision: int
    source_changed: bool
    kind: Literal["pdf", "image"] = "pdf"
    region: ImageRegion | None = None


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    previous_run_id: UUID | None = None
    model: Literal["deepseek-flash", "deepseek-v4-pro"] = "deepseek-flash"
    prompt: str = Field(min_length=1, max_length=8000)
    pdf: PdfInput | None = None
    image: ImageInput | None = None
    text: TextInput | None = None

    @model_validator(mode="after")
    def one_source(self):
        if sum(item is not None for item in (self.pdf, self.image, self.text)) > 1:
            raise ValueError("Choose either PDF, image or text input")
        return self

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Prompt must contain text without null characters")
        value.encode("utf-8")
        return value


class FormattedAnswer(BaseModel):
    body_json: dict | None
    fallback: bool


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    request_key: UUID
    model: str
    prompt: str
    answer: str
    status: Literal["queued", "running", "succeeded", "cancelled", "interrupted", "failed", "truncated"]
    usage: dict[str, int] | None
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None
    source: PdfSourceOut | ImageSourceOut | TextSourceOut | None = None
    progress: PdfProgress | None = None

    @computed_field
    @property
    def formatted_answer(self) -> FormattedAnswer | None:
        # Stream deltas remain plain text. Format one terminal snapshot, using
        # the same parser as saved notes, without changing the original answer.
        if self.status in {"queued", "running"} or not self.answer:
            return None
        body = answer_document(self.answer)
        return FormattedAnswer(body_json=body, fallback=body is None)


class ConversationDetail(ConversationOut):
    runs: list[RunOut]


class SaveAnswerIn(Input):
    title: Title
    notebook_id: UUID | None = None


class SavedAnswerOut(BaseModel):
    run_id: UUID
    note_id: UUID
    note_title: str
    notebook_id: UUID | None
    deleted_at: datetime | None
    saved_at: datetime
    source: PdfSourceOut | ImageSourceOut | TextSourceOut | None
    cited_page_numbers: list[int]


class SavedAnswerProvenanceOut(SavedAnswerOut):
    model: str

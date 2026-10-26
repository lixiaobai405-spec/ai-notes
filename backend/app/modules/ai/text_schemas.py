"""One continuous rich-text selection, measured in Unicode codepoints."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.notes.schemas import validate_pg_text


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: UUID
    revision: int = Field(strict=True, ge=1)
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=1)
    quote: str = Field(min_length=1, max_length=8000)

    @field_validator("quote")
    @classmethod
    def nonempty_quote(cls, value):
        validate_pg_text(value)
        if not value.strip():
            raise ValueError("Select nonempty text")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError("The half-open codepoint range must match the quote length")
        return self


class TextSourceOut(TextInput):
    kind: Literal["text"]
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        validate_pg_text(value)
        if not value.strip():
            raise ValueError("Source title cannot be empty")
        return value


class TextSourceReadOut(BaseModel):
    source: TextSourceOut
    text: str
    source_changed: bool

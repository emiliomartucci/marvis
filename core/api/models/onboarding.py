from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


ProjectKind = Literal["code", "no-code"]
SetupSection = Literal["Identità", "Sorgenti", "Ritmo", "Fonti del brain"]
DemoLang = Literal["it", "en"]


class ScanWorkdirRequest(BaseModel):
    root: str = Field(..., min_length=1)
    exclusions: list[str] = Field(default_factory=list, max_length=100)


class ScanWorkdirCandidate(BaseModel):
    path: str
    name: str
    kind: ProjectKind


class ScanWorkdirResponse(BaseModel):
    root: str
    exclusions: list[str]
    proposals: list[ScanWorkdirCandidate]


class SetupReadResponse(BaseModel):
    path: str
    content: str
    sections: dict[str, str]
    checkboxes: dict[str, bool]


class SetupWriteRequest(BaseModel):
    section: SetupSection
    content: str | None = Field(None, max_length=20000)
    checkboxes: dict[str, bool] | None = None

    @field_validator("content")
    @classmethod
    def _clean_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class DemoSeedRequest(BaseModel):
    lang: DemoLang = "it"


class DemoSeedResponse(BaseModel):
    project: str
    created: bool
    tasks: list[str]
    todos: list[str]
    lang: DemoLang


class DemoTeardownResponse(BaseModel):
    project: str
    tasks_deleted: int
    todos_deleted: int
    project_deleted: bool

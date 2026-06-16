# v1.0.0 - 2026-06-12 - Todos subsystem DTOs
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


TodoType = Literal["promemoria", "azione", "idea", "decidi", "approva", "rivedi"]
PersistedTodoType = Literal["promemoria", "azione", "idea", "decidi", "rivedi"]
TodoFamily = Literal["captured", "system"]
TodoDoer = Literal["human", "agent", "hybrid"]
TodoSource = Literal["user", "agent", "brain"]
TodoStatus = Literal[
    "aperto",
    "in_revisione",
    "fatto",
    "delegato",
    "scartato",
    "promosso",
    "deciso",
    "approvato",
    "rifiutato",
]
TodoOriginKind = Literal["task_review", "finding", "memory_op"]

VALID_TODO_TYPES = {"promemoria", "azione", "idea", "decidi", "approva", "rivedi"}
PERSISTED_TODO_TYPES = {"promemoria", "azione", "idea", "decidi", "rivedi"}
VALID_TODO_DOERS = {"human", "agent", "hybrid"}
VALID_TODO_STATUSES = {
    "aperto",
    "in_revisione",
    "fatto",
    "delegato",
    "scartato",
    "promosso",
    "deciso",
    "approvato",
    "rifiutato",
}
TERMINAL_TODO_STATUSES = {
    "fatto",
    "delegato",
    "scartato",
    "promosso",
    "deciso",
    "approvato",
    "rifiutato",
}

VALID_TODO_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "promemoria": {"aperto": {"fatto", "scartato"}},
    "azione": {"aperto": {"fatto", "delegato", "scartato"}},
    "idea": {"aperto": {"promosso", "scartato"}},
    "decidi": {"aperto": {"in_revisione"}, "in_revisione": {"deciso"}},
    "approva": {
        "aperto": {"in_revisione"},
        "in_revisione": {"approvato", "rifiutato"},
    },
    "rivedi": {
        "aperto": {"in_revisione"},
        "in_revisione": {"fatto", "delegato", "scartato", "deciso"},
    },
}


class TodoOrigin(BaseModel):
    kind: TodoOriginKind
    id: str


class TodoCreateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    type: TodoType | None = None
    project: str | None = Field(
        None, max_length=127, pattern=r"^[a-z0-9][a-z0-9_.&\-]{0,126}$"
    )
    fu: str | None = Field(
        None, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    payload: dict[str, Any] | None = None
    source: TodoSource = "user"
    source_ref: str | None = Field(None, max_length=500)
    doer: TodoDoer | None = None


class TodoUpdateRequest(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=5000)
    type: TodoType | None = None
    status: TodoStatus | None = None
    project: str | None = Field(
        None, max_length=127, pattern=r"^[a-z0-9][a-z0-9_.&\-]{0,126}$"
    )
    fu: str | None = Field(
        None, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    payload: dict[str, Any] | None = None
    doer: TodoDoer | None = None


class TodoDelegateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    project: str | None = Field(
        None, max_length=127, pattern=r"^[a-z0-9][a-z0-9_.&\-]{0,126}$"
    )


class TodoResponse(BaseModel):
    id: str
    type: TodoType
    family: TodoFamily
    status: str
    text: str
    payload: dict[str, Any] | None = None
    fu: str
    project: str | None = None
    source: str
    source_ref: str | None = None
    doer: TodoDoer | None = None
    linked_task_id: str | None = None
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    virtual: bool = False
    origin: TodoOrigin | None = None

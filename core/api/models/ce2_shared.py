"""Versioned CE2 shared contracts with no commercial or table coupling."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.api.models.auth import UserInfo

if TYPE_CHECKING:
    from core.api.use_cases._errors import ServiceError


CE2_SHARED_CONTRACT_VERSION = "ce2-shared-v1"


class _CE2SharedContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ce2-shared-v1"] = CE2_SHARED_CONTRACT_VERSION


class ResourceRefV1(_CE2SharedContractV1):
    workspace_id: str = Field(min_length=1, max_length=128)
    resource_type: Literal[
        "tenant", "workspace", "project", "library", "code_snapshot"
    ]
    resource_id: str = Field(min_length=1, max_length=256)


class ActorIdentityV1(_CE2SharedContractV1):
    subject_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=128)
    actor_type: Literal["human", "agent"]
    role: str = Field(min_length=1, max_length=64)
    scopes: tuple[str, ...] = ()


class ErrorEnvelopeV1(_CE2SharedContractV1):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=512)
    http_status: int = Field(ge=400, le=599)
    retry_after_seconds: int | None = Field(default=None, ge=1)
    recovery: tuple[str, ...] = ()
    retryable: bool = False

STORAGE_POLICY_V1 = "storage-policy-v1"


class StorageEncryptionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    encrypted: Literal[True] = True
    key_version: str = Field(min_length=1, max_length=256)


class StorageRetentionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["active", "tombstoned", "purged"]
    retention_days: int = Field(ge=0)
    tombstone_marker: str | None = Field(default=None, min_length=1, max_length=256)
    tombstone_replay_required: bool

    @model_validator(mode="after")
    def require_consistent_tombstone(self) -> "StorageRetentionV1":
        if self.state == "active":
            if self.tombstone_marker is not None or self.tombstone_replay_required:
                raise ValueError("active retention cannot carry a tombstone")
        elif not self.tombstone_marker or not self.tombstone_replay_required:
            raise ValueError("deleted retention requires a replayable tombstone")
        return self


class LogicalOperationBudgetV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(ge=0)
    used: int = Field(ge=0)
    window_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def reject_exhausted_snapshot(self) -> "LogicalOperationBudgetV1":
        if self.used > self.limit:
            raise ValueError("logical operation budget exceeds its limit")
        return self


class StoragePolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["storage-policy-v1"] = STORAGE_POLICY_V1
    resource: ResourceRefV1
    effective_customer_quota_bytes: int = Field(ge=0)
    operational_reserve_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    encryption: StorageEncryptionV1
    retention: StorageRetentionV1
    logical_operation_budget: LogicalOperationBudgetV1

    @model_validator(mode="after")
    def reject_incomplete_policy(self) -> "StoragePolicyV1":
        if self.resource.resource_type != "tenant":
            raise ValueError("storage policy requires a tenant resource")
        if self.used_bytes > self.effective_customer_quota_bytes:
            raise ValueError("used storage exceeds effective customer quota")
        return self

def identity_from_user(user: UserInfo) -> ActorIdentityV1:
    if user.user_type not in {"human", "agent"}:
        raise ValueError("unsupported actor type for ce2-shared-v1")
    return ActorIdentityV1(
        subject_id=user.user_id or user.username,
        workspace_id=user.workspace_id or "ws_default",
        actor_type=user.user_type,
        role=user.system_role,
        scopes=tuple(sorted(set(user.scopes))),
    )


def error_envelope_from_service_error(error: ServiceError) -> ErrorEnvelopeV1:
    context = error.context
    retry_after = context.get("retry_after")
    if (
        not isinstance(retry_after, int)
        or isinstance(retry_after, bool)
        or retry_after < 1
    ):
        retry_after = None
    recovery_value = context.get("recovery")
    recovery = (
        tuple(recovery_value)
        if isinstance(recovery_value, (list, tuple))
        and all(isinstance(value, str) for value in recovery_value)
        else ()
    )
    return ErrorEnvelopeV1(
        code=error.code,
        message=error.message,
        http_status=error.http_status,
        retry_after_seconds=retry_after,
        recovery=recovery,
        retryable=context.get("retryable") is True,
    )

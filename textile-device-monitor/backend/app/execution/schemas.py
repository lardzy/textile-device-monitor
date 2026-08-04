from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", min_length=2, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=10, max_length=500)
    role: Literal["admin", "user"] = "user"


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    password: Optional[str] = Field(default=None, min_length=10, max_length=500)
    role: Optional[Literal["admin", "user"]] = None
    is_active: Optional[bool] = None


class UserView(ORMModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CredentialUpsert(BaseModel):
    account_name: Optional[str] = Field(default=None, max_length=200)
    secret: str = Field(min_length=1, max_length=10000)


class CredentialView(ORMModel):
    id: str
    system_key: str
    account_name: Optional[str]
    is_active: bool
    configured: bool = True
    secret_mask: str = "••••••••"
    updated_at: datetime


class CategoryView(ORMModel):
    id: str
    key: str
    name: str
    description: Optional[str]
    sort_order: int
    is_active: bool


class WorkflowCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", min_length=2, max_length=100)
    category_id: str
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    definition: dict[str, Any]
    capabilities: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True


class WorkflowDraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    definition: dict[str, Any]
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    capabilities: Optional[dict[str, Any]] = None
    is_enabled: Optional[bool] = None


class WorkflowPublishRequest(BaseModel):
    revision: int = Field(ge=1)
    release_note: Optional[str] = Field(default=None, max_length=2000)


class WorkflowImportRequest(BaseModel):
    document: dict[str, Any]
    overwrite_workflow_id: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)


class WorkflowTestRequest(BaseModel):
    inspection_number: str = Field(min_length=1, max_length=200)
    target_sample_number: Optional[str] = Field(default=None, max_length=200)
    input_data: dict[str, Any] = Field(default_factory=dict)
    global_data: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("target_sample_number")
    @classmethod
    def normalize_target_sample_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class RunCreate(BaseModel):
    workflow_id: str
    inspection_number: str = Field(min_length=1, max_length=200)
    target_sample_number: Optional[str] = Field(default=None, max_length=200)
    input_data: dict[str, Any] = Field(default_factory=dict)
    global_data: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("inspection_number")
    @classmethod
    def normalize_inspection_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("检验编号不能为空")
        return normalized

    @field_validator("target_sample_number")
    @classmethod
    def normalize_target_sample_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class HumanTaskClaimRequest(BaseModel):
    revision: int = Field(ge=1)


class HumanTaskDraftRequest(BaseModel):
    revision: int = Field(ge=1)
    data: dict[str, Any] = Field(default_factory=dict)


class HumanTaskSubmitRequest(BaseModel):
    revision: int = Field(ge=1)
    data: dict[str, Any] = Field(default_factory=dict)


class HumanTaskRejectRequest(BaseModel):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class TaskSnapshotBridgeClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )


class TaskSnapshotBridgeCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(min_length=1, max_length=100)
    claim_token: str = Field(min_length=36, max_length=36)
    snapshot: dict[str, Any]


class TaskSnapshotBridgeFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(min_length=1, max_length=100)
    claim_token: str = Field(min_length=36, max_length=36)
    error_code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)


class NodeRetryRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)


class ExternalOperationApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: Literal[True]
    payload_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_sample_number: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=1000)


class ExternalReconciliationCompletedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: datetime
    exact_record_count: Literal[1]
    remote_record_id: str = Field(min_length=1, max_length=200)
    business_fields_match: Literal[True]
    inspector_match: Literal[True]
    target_file_count: Literal[1]
    remote_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("remote_record_id")
    @classmethod
    def normalize_remote_record_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Remote record id cannot be empty")
        return normalized


class ExternalReconciliationNoSideEffectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: datetime
    exact_record_count: Literal[0]
    contains_record_count: Literal[0]
    target_file_count: Literal[0]


class ExternalOperationReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm_completed", "confirm_no_side_effect"]
    attempt_id: str = Field(min_length=1, max_length=36)
    payload_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_sample_number: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=2000)
    evidence: (
        ExternalReconciliationCompletedEvidence
        | ExternalReconciliationNoSideEffectEvidence
    )

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Reconciliation note cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_evidence_for_action(self):
        if (
            self.action == "confirm_completed"
            and not isinstance(
                self.evidence,
                ExternalReconciliationCompletedEvidence,
            )
        ):
            raise ValueError("Completed action requires completed evidence")
        if (
            self.action == "confirm_no_side_effect"
            and not isinstance(
                self.evidence,
                ExternalReconciliationNoSideEffectEvidence,
            )
        ):
            raise ValueError(
                "No-side-effect action requires absence evidence"
            )
        return self


class ExternalBridgeClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    account_name: str = Field(min_length=1, max_length=200)
    supported_operation_types: list[
        Literal[
            "legacy_regenerated_fiber_count_upload",
            "legacy_special_wool_image_upload",
            "legacy_special_wool_review",
        ]
    ] = Field(
        default_factory=lambda: [
            "legacy_regenerated_fiber_count_upload"
        ],
        min_length=1,
        max_length=10,
    )

    @field_validator("account_name")
    @classmethod
    def normalize_account_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Bridge account name cannot be empty")
        return normalized


class ExternalBridgeHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    stage: Optional[str] = Field(default=None, min_length=1, max_length=50)
    stdout_append: Optional[str] = Field(default=None, max_length=10000)


class ExternalBridgeStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    stage: str = Field(min_length=1, max_length=50)
    detail: Optional[str] = Field(default=None, max_length=2000)


class ExternalBridgeCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    receipt: dict[str, Any]
    stdout_summary: Optional[str] = Field(default=None, max_length=20000)


class ExternalBridgeFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    stage: str = Field(min_length=1, max_length=50)
    error_code: Optional[str] = Field(default=None, max_length=100)
    message: Optional[str] = Field(default=None, max_length=2000)


class FileRefreshRequest(BaseModel):
    root_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,99}$")
    # 首版索引边界固定为数据根及一级子目录。共享盘的更深层遍历必须由
    # 后续受控的管理员索引策略显式开放，不能由普通 API 请求放大。
    max_depth: Optional[int] = Field(default=1, ge=0, le=1)


class ArtifactReferenceRequest(BaseModel):
    root_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    relative_path: str = Field(min_length=1, max_length=2000)


class MutationPreflightRequest(BaseModel):
    mutation_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    source: ArtifactReferenceRequest
    working_relative_path: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=2000,
    )
    node_id: Optional[str] = Field(default=None, min_length=1, max_length=100)


class MutationStageRequest(BaseModel):
    node_id: Optional[str] = Field(default=None, min_length=1, max_length=100)


class MutationCellWriteRequest(BaseModel):
    sheet: str = Field(min_length=1, max_length=31)
    cell: str = Field(min_length=1, max_length=32)
    value: Any = None


class MutationWriteRequest(MutationStageRequest):
    writes: list[MutationCellWriteRequest] = Field(min_length=1, max_length=500)


class MutationVerifyRequest(MutationStageRequest):
    target: ArtifactReferenceRequest
    writes: Optional[list[MutationCellWriteRequest]] = Field(
        default=None,
        min_length=1,
        max_length=500,
    )


class MutationApprovalWorkingCopyRequest(ArtifactReferenceRequest):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MutationApprovalContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: Literal[True]
    mutation_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
        min_length=1,
        max_length=100,
    )
    working_copy: MutationApprovalWorkingCopyRequest
    target: ArtifactReferenceRequest
    change_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class MutationPublishRequest(MutationStageRequest):
    target: ArtifactReferenceRequest
    approval_context: MutationApprovalContextRequest

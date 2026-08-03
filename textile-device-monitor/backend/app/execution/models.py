from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base


JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionUser(Base):
    __tablename__ = "execution_users"

    id = Column(String(36), primary_key=True, default=new_id)
    username = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(100), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user", index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    sessions = relationship(
        "ExecutionSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    credentials = relationship(
        "ExecutionCredential",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    role_bindings = relationship(
        "ExecutionUserRole",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="ExecutionUserRole.user_id",
    )


class ExecutionRole(Base):
    __tablename__ = "execution_roles"

    id = Column(String(36), primary_key=True, default=new_id)
    key = Column(String(50), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    is_system = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    permission_bindings = relationship(
        "ExecutionRolePermission",
        back_populates="role",
        cascade="all, delete-orphan",
    )
    user_bindings = relationship(
        "ExecutionUserRole",
        back_populates="role",
        cascade="all, delete-orphan",
    )


class ExecutionPermission(Base):
    __tablename__ = "execution_permissions"

    id = Column(String(36), primary_key=True, default=new_id)
    key = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    role_bindings = relationship(
        "ExecutionRolePermission",
        back_populates="permission",
        cascade="all, delete-orphan",
    )


class ExecutionRolePermission(Base):
    __tablename__ = "execution_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "role_id",
            "permission_id",
            name="uq_execution_role_permission",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    role_id = Column(
        String(36),
        ForeignKey("execution_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permission_id = Column(
        String(36),
        ForeignKey("execution_permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    role = relationship("ExecutionRole", back_populates="permission_bindings")
    permission = relationship(
        "ExecutionPermission",
        back_populates="role_bindings",
    )


class ExecutionUserRole(Base):
    __tablename__ = "execution_user_roles"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "role_id",
            name="uq_execution_user_role",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    user_id = Column(
        String(36),
        ForeignKey("execution_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id = Column(
        String(36),
        ForeignKey("execution_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id = Column(String(36), ForeignKey("execution_users.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship(
        "ExecutionUser",
        back_populates="role_bindings",
        foreign_keys=[user_id],
    )
    role = relationship("ExecutionRole", back_populates="user_bindings")


class ExecutionSession(Base):
    __tablename__ = "execution_sessions"

    id = Column(String(36), primary_key=True, default=new_id)
    user_id = Column(
        String(36),
        ForeignKey("execution_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    csrf_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship("ExecutionUser", back_populates="sessions")


class ExecutionLoginThrottle(Base):
    __tablename__ = "execution_login_throttles"

    key_hash = Column(String(64), primary_key=True)
    failure_count = Column(Integer, nullable=False, default=0)
    blocked_until = Column(DateTime(timezone=True), index=True)
    last_failed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ExecutionCredential(Base):
    __tablename__ = "execution_credentials"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "system_key",
            name="uq_execution_credentials_user_system",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    user_id = Column(
        String(36),
        ForeignKey("execution_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    system_key = Column(String(50), nullable=False)
    account_name = Column(String(200))
    encrypted_secret = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    user = relationship("ExecutionUser", back_populates="credentials")


class ExecutionCategory(Base):
    __tablename__ = "execution_categories"

    id = Column(String(36), primary_key=True, default=new_id)
    key = Column(String(50), nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    workflows = relationship("ExecutionWorkflow", back_populates="category")


class ExecutionWorkflow(Base):
    __tablename__ = "execution_workflows"

    id = Column(String(36), primary_key=True, default=new_id)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    category_id = Column(
        String(36),
        ForeignKey("execution_categories.id"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text)
    draft_definition = Column(JSON_VARIANT, nullable=False)
    draft_revision = Column(Integer, nullable=False, default=1)
    published_version_number = Column(Integer)
    capabilities = Column(JSON_VARIANT, nullable=False, default=dict)
    required_input_count = Column(Integer, nullable=False, default=0)
    is_enabled = Column(Boolean, nullable=False, default=True)
    availability_code = Column(String(100))
    availability_message = Column(Text)
    created_by_id = Column(String(36), ForeignKey("execution_users.id"))
    updated_by_id = Column(String(36), ForeignKey("execution_users.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        index=True,
    )

    category = relationship("ExecutionCategory", back_populates="workflows")
    versions = relationship(
        "ExecutionWorkflowVersion",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="ExecutionWorkflowVersion.version_number",
    )


class ExecutionWorkflowVersion(Base):
    __tablename__ = "execution_workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "version_number",
            name="uq_execution_workflow_versions_number",
        ),
        Index(
            "ix_execution_workflow_versions_checksum",
            "workflow_id",
            "checksum",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    workflow_id = Column(
        String(36),
        ForeignKey("execution_workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    schema_version = Column(String(20), nullable=False)
    definition = Column(JSON_VARIANT, nullable=False)
    checksum = Column(String(64), nullable=False)
    capabilities = Column(JSON_VARIANT, nullable=False, default=dict)
    contract_checksum = Column(String(64), nullable=False)
    release_note = Column(Text)
    published_by_id = Column(String(36), ForeignKey("execution_users.id"))
    published_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    workflow = relationship("ExecutionWorkflow", back_populates="versions")


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        UniqueConstraint(
            "created_by_id",
            "idempotency_key",
            name="uq_execution_runs_creator_idempotency",
        ),
        Index("ix_execution_runs_status_created", "status", "created_at"),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    workflow_id = Column(
        String(36),
        ForeignKey("execution_workflows.id"),
        nullable=False,
        index=True,
    )
    workflow_version_id = Column(
        String(36),
        ForeignKey("execution_workflow_versions.id"),
        index=True,
    )
    created_by_id = Column(
        String(36),
        ForeignKey("execution_users.id"),
        nullable=False,
        index=True,
    )
    idempotency_key = Column(String(100), nullable=False)
    inspection_number = Column(String(200), nullable=False, index=True)
    mode = Column(String(20), nullable=False, default="live")
    status = Column(String(30), nullable=False, default="queued", index=True)
    definition_snapshot = Column(JSON_VARIANT, nullable=False)
    definition_checksum = Column(String(64), nullable=False)
    capabilities_snapshot = Column(JSON_VARIANT, nullable=False, default=dict)
    contract_checksum = Column(String(64), nullable=False)
    input_data = Column(JSON_VARIANT, nullable=False, default=dict)
    global_data = Column(JSON_VARIANT, nullable=False, default=dict)
    output_data = Column(JSON_VARIANT, nullable=False, default=dict)
    error_code = Column(String(100))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        index=True,
    )

    workflow = relationship("ExecutionWorkflow")
    workflow_version = relationship("ExecutionWorkflowVersion")
    created_by = relationship("ExecutionUser")
    node_runs = relationship(
        "ExecutionNodeRun",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    edge_runs = relationship(
        "ExecutionEdgeRun",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    events = relationship(
        "ExecutionEvent",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    external_operations = relationship(
        "ExecutionExternalOperation",
        back_populates="run",
        cascade="all, delete-orphan",
    )


class ExecutionNodeRun(Base):
    __tablename__ = "execution_node_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "node_id",
            name="uq_execution_node_runs_run_node",
        ),
        Index(
            "ix_execution_node_runs_claim",
            "status",
            "ready_at",
            "lease_expires_at",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id = Column(String(100), nullable=False)
    node_type = Column(String(100), nullable=False)
    node_type_version = Column(Integer, nullable=False, default=1)
    node_name = Column(String(200), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    input_data = Column(JSON_VARIANT, nullable=False, default=dict)
    output_data = Column(JSON_VARIANT, nullable=False, default=dict)
    error_code = Column(String(100))
    error_message = Column(Text)
    ready_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    lease_owner = Column(String(100))
    lease_token = Column(String(36))
    lease_expires_at = Column(DateTime(timezone=True), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    run = relationship("ExecutionRun", back_populates="node_runs")
    attempts = relationship(
        "ExecutionNodeAttempt",
        back_populates="node_run",
        cascade="all, delete-orphan",
    )
    human_task = relationship(
        "ExecutionHumanTask",
        back_populates="node_run",
        uselist=False,
        cascade="all, delete-orphan",
    )
    external_operation = relationship(
        "ExecutionExternalOperation",
        back_populates="node_run",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ExecutionNodeAttempt(Base):
    __tablename__ = "execution_node_attempts"
    __table_args__ = (
        UniqueConstraint(
            "node_run_id",
            "attempt_number",
            name="uq_execution_node_attempts_number",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    node_run_id = Column(
        String(36),
        ForeignKey("execution_node_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number = Column(Integer, nullable=False)
    worker_id = Column(String(100), nullable=False)
    lease_token = Column(String(36), nullable=False)
    status = Column(String(30), nullable=False, default="running")
    input_data = Column(JSON_VARIANT, nullable=False, default=dict)
    output_data = Column(JSON_VARIANT, nullable=False, default=dict)
    error_code = Column(String(100))
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True))

    node_run = relationship("ExecutionNodeRun", back_populates="attempts")


class ExecutionEdgeRun(Base):
    __tablename__ = "execution_edge_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "edge_id",
            name="uq_execution_edge_runs_run_edge",
        ),
        Index(
            "ix_execution_edge_runs_target",
            "run_id",
            "target_node_id",
            "status",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    edge_id = Column(String(100), nullable=False)
    source_node_id = Column(String(100), nullable=False)
    target_node_id = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    condition = Column(JSON_VARIANT)
    join_policy = Column(String(20), nullable=False, default="all")
    resolved_at = Column(DateTime(timezone=True))

    run = relationship("ExecutionRun", back_populates="edge_runs")


class ExecutionHumanTask(Base):
    __tablename__ = "execution_human_tasks"

    id = Column(String(36), primary_key=True, default=new_id)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_run_id = Column(
        String(36),
        ForeignKey("execution_node_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    title = Column(String(200), nullable=False)
    description = Column(Text)
    form_schema = Column(JSON_VARIANT, nullable=False, default=dict)
    draft_data = Column(JSON_VARIANT, nullable=False, default=dict)
    result_data = Column(JSON_VARIANT, nullable=False, default=dict)
    status = Column(String(30), nullable=False, default="open", index=True)
    revision = Column(Integer, nullable=False, default=1)
    assigned_user_id = Column(
        String(36),
        ForeignKey("execution_users.id"),
        index=True,
    )
    candidate_role_key = Column(String(50), index=True)
    claimed_by_id = Column(String(36), ForeignKey("execution_users.id"))
    claimed_at = Column(DateTime(timezone=True))
    completed_by_id = Column(String(36), ForeignKey("execution_users.id"))
    completed_at = Column(DateTime(timezone=True))
    due_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    node_run = relationship("ExecutionNodeRun", back_populates="human_task")


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    __table_args__ = (Index("ix_execution_events_run_id_id", "run_id", "id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), nullable=False, unique=True, default=new_id)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(100), nullable=False, index=True)
    actor_type = Column(String(30), nullable=False, default="system")
    actor_id = Column(String(100))
    payload = Column(JSON_VARIANT, nullable=False, default=dict)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    run = relationship("ExecutionRun", back_populates="events")


class ExecutionOutbox(Base):
    __tablename__ = "execution_outbox"
    __table_args__ = (
        Index(
            "ix_execution_outbox_dispatch",
            "dispatched_at",
            "available_at",
            "lease_expires_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), nullable=False, unique=True)
    topic = Column(String(100), nullable=False)
    aggregate_id = Column(String(100), nullable=False, index=True)
    payload = Column(JSON_VARIANT, nullable=False, default=dict)
    available_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    attempts = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(100))
    lease_expires_at = Column(DateTime(timezone=True))
    dispatched_at = Column(DateTime(timezone=True))
    dead_lettered_at = Column(DateTime(timezone=True), index=True)
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExecutionWorkerHeartbeat(Base):
    __tablename__ = "execution_worker_heartbeats"

    worker_id = Column(String(100), primary_key=True)
    status = Column(String(30), nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    last_error = Column(Text)
    capabilities = Column(JSON_VARIANT, nullable=False, default=dict)


class ExecutionAuditLog(Base):
    __tablename__ = "execution_audit_logs"
    __table_args__ = (
        Index("ix_execution_audit_resource", "resource_type", "resource_id"),
        Index("ix_execution_audit_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_user_id = Column(String(36), ForeignKey("execution_users.id"))
    action = Column(String(100), nullable=False)
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(100), nullable=False)
    request_id = Column(String(100))
    details = Column(JSON_VARIANT, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExecutionStorageRoot(Base):
    __tablename__ = "execution_storage_roots"

    id = Column(String(36), primary_key=True, default=new_id)
    root_id = Column(String(100), nullable=False, unique=True, index=True)
    name = Column(String(200), nullable=False)
    local_path = Column(Text, nullable=False)
    source_uri = Column(Text)
    access_mode = Column(String(20), nullable=False, default="read")
    category_key = Column(String(50), index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_available = Column(Boolean, nullable=False, default=False)
    availability_message = Column(Text)
    scan_generation = Column(Integer, nullable=False, default=0)
    last_scan_started_at = Column(DateTime(timezone=True))
    last_scan_finished_at = Column(DateTime(timezone=True))
    last_scan_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ExecutionFileIndexEntry(Base):
    __tablename__ = "execution_file_index_entries"
    __table_args__ = (
        UniqueConstraint(
            "storage_root_id",
            "relative_path",
            name="uq_execution_file_index_root_path",
        ),
        Index(
            "ix_execution_file_index_lookup",
            "storage_root_id",
            "inspection_number",
            "modified_at",
        ),
        Index(
            "ix_execution_file_index_group",
            "storage_root_id",
            "group_key",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    storage_root_id = Column(
        String(36),
        ForeignKey("execution_storage_roots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path = Column(Text, nullable=False)
    filename = Column(String(500), nullable=False, index=True)
    extension = Column(String(30), nullable=False, index=True)
    file_kind = Column(String(50), nullable=False, default="file", index=True)
    inspection_number = Column(String(200), index=True)
    group_key = Column(String(500), index=True)
    size_bytes = Column(BigInteger, nullable=False)
    modified_at = Column(DateTime(timezone=True), nullable=False, index=True)
    fingerprint = Column(String(200), nullable=False)
    content_sha256 = Column(String(64))
    metadata_json = Column(JSON_VARIANT, nullable=False, default=dict)
    scan_generation = Column(Integer, nullable=False, default=0, index=True)
    indexed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    missing_since = Column(DateTime(timezone=True), index=True)


class ExecutionTaskSnapshotCache(Base):
    """Read-only FibreCheck task facts cached for workflow recommendations.

    The cache is also the small hand-off queue used by the Windows read-only
    probe.  No legacy-system mutation is represented by this table.
    """

    __tablename__ = "execution_task_snapshot_cache"

    inspection_number = Column(String(200), primary_key=True)
    status = Column(String(30), nullable=False, default="queued", index=True)
    snapshot = Column(JSON_VARIANT, nullable=False, default=dict)
    revision = Column(Integer, nullable=False, default=1)
    refresh_requested_at = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    fetched_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True), index=True)
    claimed_by = Column(String(100))
    claim_token = Column(String(36))
    claim_expires_at = Column(DateTime(timezone=True), index=True)
    error_code = Column(String(100))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ExecutionIndexJob(Base):
    __tablename__ = "execution_index_jobs"
    __table_args__ = (
        Index(
            "ix_execution_index_jobs_claim",
            "status",
            "created_at",
            "lease_expires_at",
        ),
        Index(
            "uq_execution_index_jobs_active_root",
            "storage_root_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    storage_root_id = Column(
        String(36),
        ForeignKey("execution_storage_roots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_by_id = Column(String(36), ForeignKey("execution_users.id"))
    status = Column(String(30), nullable=False, default="queued", index=True)
    max_depth = Column(Integer)
    added_count = Column(Integer, nullable=False, default=0)
    updated_count = Column(Integer, nullable=False, default=0)
    removed_count = Column(Integer, nullable=False, default=0)
    total_count = Column(Integer, nullable=False, default=0)
    errors = Column(JSON_VARIANT, nullable=False, default=list)
    lease_owner = Column(String(100))
    lease_token = Column(String(36))
    lease_expires_at = Column(DateTime(timezone=True), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))


class ExecutionArtifact(Base):
    __tablename__ = "execution_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "storage_root_id",
            "relative_path",
            "content_sha256",
            name="uq_execution_artifact_identity",
        ),
        Index("ix_execution_artifacts_run_role", "run_id", "role"),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="SET NULL"),
        index=True,
    )
    node_run_id = Column(
        String(36),
        ForeignKey("execution_node_runs.id", ondelete="SET NULL"),
        index=True,
    )
    storage_root_id = Column(
        String(36),
        ForeignKey("execution_storage_roots.id"),
        nullable=False,
        index=True,
    )
    relative_path = Column(Text, nullable=False)
    filename = Column(String(500), nullable=False)
    role = Column(String(30), nullable=False, index=True)
    media_type = Column(String(200))
    size_bytes = Column(BigInteger, nullable=False)
    modified_at = Column(DateTime(timezone=True))
    content_sha256 = Column(String(64), nullable=False, index=True)
    immutable = Column(Boolean, nullable=False, default=True)
    metadata_json = Column(JSON_VARIANT, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    storage_root = relationship("ExecutionStorageRoot")


class ExecutionArtifactRelation(Base):
    __tablename__ = "execution_artifact_relations"
    __table_args__ = (
        UniqueConstraint(
            "parent_artifact_id",
            "child_artifact_id",
            "relation_type",
            name="uq_execution_artifact_relation",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    parent_artifact_id = Column(
        String(36),
        ForeignKey("execution_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    child_artifact_id = Column(
        String(36),
        ForeignKey("execution_artifacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation_type = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExecutionFileMutation(Base):
    __tablename__ = "execution_file_mutations"

    id = Column(String(36), primary_key=True, default=new_id)
    mutation_id = Column(String(100), nullable=False, unique=True, index=True)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_run_id = Column(
        String(36),
        ForeignKey("execution_node_runs.id", ondelete="SET NULL"),
        index=True,
    )
    source_artifact_id = Column(
        String(36),
        ForeignKey("execution_artifacts.id"),
        nullable=False,
    )
    working_artifact_id = Column(
        String(36),
        ForeignKey("execution_artifacts.id"),
    )
    status = Column(String(30), nullable=False, default="planned", index=True)
    source_fingerprint = Column(String(200), nullable=False)
    change_plan = Column(JSON_VARIANT, nullable=False, default=dict)
    verification_result = Column(JSON_VARIANT, nullable=False, default=dict)
    publish_fence_token = Column(String(36))
    publish_started_at = Column(DateTime(timezone=True))
    error_code = Column(String(100))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ExecutionExternalOperation(Base):
    """Durable fence for an external-system side effect.

    Creating or approving this row never performs the remote operation.  A
    future, separately authenticated connector must claim the fence and record
    a receipt before the waiting node can be completed.
    """

    __tablename__ = "execution_external_operations"
    __table_args__ = (
        UniqueConstraint(
            "operation_key",
            name="uq_execution_external_operations_key",
        ),
        UniqueConstraint(
            "node_run_id",
            name="uq_execution_external_operations_node_run",
        ),
        Index(
            "ix_execution_external_operations_claim",
            "status",
            "created_at",
            "lease_expires_at",
        ),
        Index(
            "ix_execution_external_operations_run_created",
            "run_id",
            "created_at",
        ),
        Index(
            "ix_execution_external_operations_account_scope",
            "account_scope_key",
        ),
        Index(
            "uq_execution_external_operations_active_remote_key",
            "remote_business_key",
            unique=True,
            postgresql_where=text(
                "status IN "
                "('prepared', 'approved', 'in_progress', "
                "'cancel_pending', 'reconciliation_required')"
            ),
            sqlite_where=text(
                "status IN "
                "('prepared', 'approved', 'in_progress', "
                "'cancel_pending', 'reconciliation_required')"
            ),
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    operation_key = Column(String(64), nullable=False)
    run_id = Column(
        String(36),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_run_id = Column(
        String(36),
        ForeignKey("execution_node_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_key = Column(String(100), nullable=False, index=True)
    credential_id = Column(
        String(36),
        ForeignKey("execution_credentials.id", ondelete="SET NULL"),
        index=True,
    )
    credential_revision = Column(Integer, nullable=False)
    account_scope_key = Column(String(64), nullable=False)
    remote_business_key = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default="prepared", index=True)
    payload_checksum = Column(String(64), nullable=False)
    request_summary = Column(JSON_VARIANT, nullable=False, default=dict)
    preflight_expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
    )
    approved_by_id = Column(
        String(36),
        ForeignKey("execution_users.id", ondelete="SET NULL"),
        index=True,
    )
    approved_at = Column(DateTime(timezone=True))
    approval_expires_at = Column(DateTime(timezone=True))
    approval_note = Column(Text)
    fence_token = Column(String(36))
    lease_owner = Column(String(100))
    lease_expires_at = Column(DateTime(timezone=True), index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    remote_record_id = Column(String(200))
    receipt = Column(JSON_VARIANT, nullable=False, default=dict)
    verification = Column(JSON_VARIANT, nullable=False, default=dict)
    error_code = Column(String(100))
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    run = relationship("ExecutionRun", back_populates="external_operations")
    node_run = relationship(
        "ExecutionNodeRun",
        back_populates="external_operation",
    )
    attempts = relationship(
        "ExecutionExternalAttempt",
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by="ExecutionExternalAttempt.attempt_no",
    )


class ExecutionExternalAttempt(Base):
    """One Bridge claim against an approved external-operation fence.

    The attempt records client-side progress checkpoints so a lost lease can
    be reconciled: a failure before ``file_copy_started`` never touched the
    legacy system, anything later may have left a remote side effect behind.
    """

    __tablename__ = "execution_external_attempts"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "attempt_no",
            name="uq_execution_external_attempts_no",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    operation_id = Column(
        String(36),
        ForeignKey("execution_external_operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_no = Column(Integer, nullable=False)
    bridge_id = Column(String(100), nullable=False)
    status = Column(String(30), nullable=False, default="claimed", index=True)
    current_stage = Column(String(50))
    lease_expires_at = Column(DateTime(timezone=True), index=True)
    checkpoints = Column(JSON_VARIANT, nullable=False, default=list)
    stdout_summary = Column(Text, nullable=False, default="")
    exit_code = Column(Integer)
    error_code = Column(String(100))
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    operation = relationship(
        "ExecutionExternalOperation",
        back_populates="attempts",
    )


class ExecutionPublishReceipt(Base):
    __tablename__ = "execution_publish_receipts"
    __table_args__ = (
        UniqueConstraint(
            "mutation_id",
            "target_storage_root_id",
            "target_relative_path",
            name="uq_execution_publish_receipt_target",
        ),
    )

    id = Column(String(36), primary_key=True, default=new_id)
    mutation_id = Column(
        String(36),
        ForeignKey("execution_file_mutations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id = Column(
        String(36),
        ForeignKey("execution_artifacts.id"),
        nullable=False,
    )
    target_storage_root_id = Column(
        String(36),
        ForeignKey("execution_storage_roots.id"),
        nullable=False,
    )
    target_relative_path = Column(Text, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    status = Column(String(30), nullable=False, default="published")
    published_by_id = Column(String(36), ForeignKey("execution_users.id"))
    published_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    details = Column(JSON_VARIANT, nullable=False, default=dict)

from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.events import append_audit_log
from app.execution.models import (
    ExecutionCategory,
    ExecutionPermission,
    ExecutionRole,
    ExecutionRolePermission,
    ExecutionUser,
    ExecutionUserRole,
    ExecutionWorkflow,
    ExecutionWorkflowVersion,
)
from app.execution.security import hash_password
from app.execution.validation import (
    definition_checksum,
    runtime_definition,
    validate_definition,
    workflow_contract_checksum,
)


DEFAULT_CATEGORIES = (
    ("special_wool", "特种毛", "特种动物纤维原始记录", 10),
    ("regenerated_fiber", "再生纤", "再生纤维素纤维原始记录", 20),
    ("hemp_cotton", "麻棉", "麻棉类原始记录", 30),
    ("electron_microscopy", "电镜", "电镜图片与微观形貌项目", 40),
)

DEFAULT_PERMISSIONS = (
    ("workflow.read", "查看流程"),
    ("workflow.run", "运行流程"),
    ("workflow.design", "设计流程"),
    ("workflow.publish", "发布流程"),
    ("human_task.handle", "处理人工任务"),
    ("file.read", "读取文件索引"),
    ("file.write", "创建工作副本"),
    ("file.publish", "发布执行制品"),
    ("audit.read", "查看审计记录"),
    ("user.manage", "管理用户"),
    ("credential.manage", "管理个人凭据"),
)

DEFAULT_ROLE_PERMISSIONS = {
    "admin": {key for key, _name in DEFAULT_PERMISSIONS},
    "user": {
        "workflow.read",
        "workflow.run",
        "human_task.handle",
        "file.read",
        "file.write",
        "file.publish",
        "credential.manage",
    },
}


def _default_definition(
    *,
    slug: str,
    name: str,
    category_key: str,
    root_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "metadata": {
            "slug": slug,
            "name": name,
            "category": category_key,
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "inspection_number": {
                    "type": "string",
                    "title": "检验编号",
                }
            },
            "required": ["inspection_number"],
        },
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [{"name": "source", "root_id": root_id, "access": "read"}],
        "credential_slots": [],
        "nodes": [
            {
                "id": "start",
                "type": "core.start",
                "type_version": 1,
                "name": "开始",
                "config": {},
                "input_mapping": {},
                "ui": {"x": 40, "y": 180},
            },
            {
                "id": "query",
                "type": (
                    "electron.group"
                    if category_key == "electron_microscopy"
                    else "file.index_query"
                ),
                "type_version": 1,
                "name": "查询原始资料",
                "config": (
                    {"root_id": root_id, "limit": 6, "recent_days": 7}
                    if category_key != "electron_microscopy"
                    else {"root_id": root_id, "limit": 6, "recent_days": 7}
                ),
                "input_mapping": {
                    "inspection_number": "$.inputs.inspection_number"
                },
                "ui": {"x": 280, "y": 180},
            },
            {
                "id": "select",
                "type": "human.file_selection",
                "type_version": 1,
                "name": "选择原始资料",
                "config": {
                    "title": "请选择本次执行使用的原始资料",
                    "allow_multiple": True,
                },
                "input_mapping": (
                    {"groups": "$.nodes.query.output.groups"}
                    if category_key == "electron_microscopy"
                    else {
                        "candidates":
                            "$.nodes.query.output.candidates"
                    }
                ),
                "ui": {"x": 540, "y": 180},
            },
            {
                "id": "result",
                "type": "result.aggregate",
                "type_version": 1,
                "name": "汇总选择结果",
                "config": {},
                "input_mapping": {"selection": "$.nodes.select.output"},
                "ui": {"x": 800, "y": 180},
            },
            {
                "id": "end",
                "type": "core.end",
                "type_version": 1,
                "name": "结束",
                "config": {},
                "input_mapping": {"result": "$.nodes.result.output"},
                "ui": {"x": 1040, "y": 180},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "query"},
            {"id": "e2", "source": "query", "target": "select"},
            {"id": "e3", "source": "select", "target": "result"},
            {"id": "e4", "source": "result", "target": "end"},
        ],
    }


def _controlled_write_test_definition() -> dict[str, Any]:
    node_specs = (
        ("start", "core.start", "开始", {}),
        ("classify", "excel.classify", "识别工作簿类型", {}),
        ("extract", "excel.extract_summary", "读取摘要", {}),
        (
            "copy",
            "workbook.copy",
            "创建工作副本",
            {"staging_root_id": "execution_staging"},
        ),
        ("write", "workbook.write_cells", "写入映射字段", {}),
        ("verify", "workbook.verify", "保存后重读核对", {}),
        (
            "confirm",
            "human.confirm",
            "人工确认",
            {"title": "确认测试发布"},
        ),
        (
            "publish",
            "artifact.publish",
            "发布测试制品",
            {
                "publish_root_id": "execution_publish",
                "confirmation_node_id": "confirm",
            },
        ),
        ("end", "core.end", "结束", {}),
    )
    mappings = {
        "classify": {"source": "$.inputs.source"},
        "extract": {"source": "$.inputs.source"},
        "copy": {
            "source": "$.inputs.source",
            "mutation_id": "$.inputs.mutation_id",
        },
        "write": {
            "mutation_id": "$.inputs.mutation_id",
            "working_copy": "$.nodes.copy.output.working_copy",
            "writes": "$.inputs.writes",
        },
        "verify": {
            "mutation_id": "$.inputs.mutation_id",
            "working_copy": "$.nodes.copy.output.working_copy",
            "writes": "$.inputs.writes",
            "target": "$.inputs.target",
        },
        "confirm": {
            "approval_context": "$.nodes.verify.output.approval_context",
        },
        "publish": {
            "mutation_id": "$.inputs.mutation_id",
            "working_copy": "$.nodes.copy.output.working_copy",
            "target": "$.inputs.target",
        },
        "end": {"published": "$.nodes.publish.output"},
    }
    return {
        "schema_version": "1.0",
        "metadata": {
            "slug": "system-controlled-xlsx-write-test",
            "name": "受控 Excel 写入验收",
            "category": "special_wool",
            "system_test": True,
        },
        "input_schema": {
            "type": "object",
            "properties": {
                "inspection_number": {
                    "type": "string",
                    "title": "检验编号",
                },
                "source": {
                    "type": "object",
                    "title": "源工作簿引用",
                },
                "mutation_id": {
                    "type": "string",
                    "title": "变更幂等键",
                },
                "writes": {
                    "type": "array",
                    "title": "单元格写入计划",
                },
                "target": {
                    "type": "object",
                    "title": "测试发布目标",
                },
            },
            "required": [
                "inspection_number",
                "source",
                "mutation_id",
                "writes",
                "target",
            ],
            "additionalProperties": False,
        },
        "global_schema": {"type": "object", "properties": {}},
        "root_slots": [
            {
                "name": "source",
                "root_id": "special_wool_records",
                "access": "read",
            },
            {
                "name": "staging",
                "root_id": "execution_staging",
                "access": "write",
            },
            {
                "name": "publish",
                "root_id": "execution_publish",
                "access": "publish",
            },
        ],
        "credential_slots": [],
        "nodes": [
            {
                "id": node_id,
                "type": node_type,
                "type_version": 1,
                "name": name,
                "config": config,
                "input_mapping": mappings.get(node_id, {}),
                "ui": {"x": 60 + index * 230, "y": 180},
            }
            for index, (node_id, node_type, name, config) in enumerate(
                node_specs
            )
        ],
        "edges": [
            {
                "id": f"edge-{index + 1}",
                "source": node_specs[index][0],
                "target": node_specs[index + 1][0],
            }
            for index in range(len(node_specs) - 1)
        ],
    }


DEFAULT_WORKFLOWS = (
    (
        "special-wool-source-selection",
        "特种毛原始资料发现与选择",
        "special_wool",
        "special_wool_records",
        True,
        None,
    ),
    (
        "regenerated-fiber-source-selection",
        "再生纤原始资料发现与选择",
        "regenerated_fiber",
        "regenerated_fiber_records",
        True,
        "数据根未配置",
    ),
    (
        "hemp-cotton-source-selection",
        "麻棉原始资料发现与选择",
        "hemp_cotton",
        "hemp_cotton_records",
        True,
        None,
    ),
    (
        "electron-source-selection",
        "电镜原始资料发现与选择",
        "electron_microscopy",
        "electron_microscopy_records",
        True,
        None,
    ),
)


def _required_input_count(definition: dict[str, Any]) -> int:
    input_schema = definition.get("input_schema") or {}
    required = input_schema.get("required") or []
    return len(required) if isinstance(required, list) else 0


def ensure_default_catalog(db: Session) -> None:
    categories_by_key = {
        category.key: category
        for category in db.query(ExecutionCategory).all()
    }
    for key, name, description, sort_order in DEFAULT_CATEGORIES:
        if key not in categories_by_key:
            category = ExecutionCategory(
                key=key,
                name=name,
                description=description,
                sort_order=sort_order,
            )
            db.add(category)
            db.flush()
            categories_by_key[key] = category

    existing_slugs = {
        workflow.slug for workflow in db.query(ExecutionWorkflow.slug).all()
    }
    for (
        slug,
        name,
        category_key,
        root_id,
        enabled,
        unavailable_message,
    ) in DEFAULT_WORKFLOWS:
        if slug in existing_slugs:
            continue
        definition = _default_definition(
            slug=slug,
            name=name,
            category_key=category_key,
            root_id=root_id,
        )
        workflow = ExecutionWorkflow(
            slug=slug,
            category_id=categories_by_key[category_key].id,
            name=name,
            description="按检验编号从文件索引中发现并人工确认原始资料。",
            draft_definition=deepcopy(definition),
            draft_revision=1,
            published_version_number=1,
            capabilities={"read": True, "write": False},
            required_input_count=1,
            is_enabled=enabled,
            availability_code=(
                "root_not_configured" if unavailable_message else None
            ),
            availability_message=unavailable_message,
        )
        db.add(workflow)
        db.flush()
        published_capabilities = {"read": True, "write": False}
        db.add(
            ExecutionWorkflowVersion(
                workflow_id=workflow.id,
                version_number=1,
                schema_version="1.0",
                definition=deepcopy(definition),
                checksum=definition_checksum(definition),
                capabilities=deepcopy(published_capabilities),
                contract_checksum=workflow_contract_checksum(
                    definition,
                    published_capabilities,
                ),
                release_note="系统初始化版本",
            )
        )
    system_test_slug = "system-controlled-xlsx-write-test"
    if system_test_slug not in existing_slugs:
        definition = _controlled_write_test_definition()
        workflow = ExecutionWorkflow(
            slug=system_test_slug,
            category_id=categories_by_key["special_wool"].id,
            name="受控 Excel 写入验收",
            description="仅管理员可见，用于验证复制、写入、重读、确认和发布链路。",
            draft_definition=deepcopy(definition),
            draft_revision=1,
            published_version_number=1,
            capabilities={
                "read": True,
                "write": True,
                "hidden": True,
                "system_test": True,
            },
            required_input_count=_required_input_count(definition),
            is_enabled=True,
        )
        db.add(workflow)
        db.flush()
        published_capabilities = {
            "read": True,
            "write": True,
            "hidden": True,
            "system_test": True,
        }
        db.add(
            ExecutionWorkflowVersion(
                workflow_id=workflow.id,
                version_number=1,
                schema_version="1.0",
                definition=deepcopy(definition),
                checksum=definition_checksum(definition),
                capabilities=deepcopy(published_capabilities),
                contract_checksum=workflow_contract_checksum(
                    definition,
                    published_capabilities,
                ),
                release_note="系统写入验收流程",
            )
        )
    db.flush()


def ensure_default_rbac(db: Session) -> dict[str, ExecutionRole]:
    permissions_by_key = {
        permission.key: permission
        for permission in db.query(ExecutionPermission).all()
    }
    for key, name in DEFAULT_PERMISSIONS:
        if key not in permissions_by_key:
            permission = ExecutionPermission(key=key, name=name)
            db.add(permission)
            db.flush()
            permissions_by_key[key] = permission

    roles_by_key = {
        role.key: role for role in db.query(ExecutionRole).all()
    }
    for key, name, description in (
        ("admin", "管理员", "可设计、发布、运行和管理执行系统"),
        ("user", "普通用户", "可运行流程并处理自己的人工任务"),
    ):
        if key not in roles_by_key:
            role = ExecutionRole(
                key=key,
                name=name,
                description=description,
                is_system=True,
            )
            db.add(role)
            db.flush()
            roles_by_key[key] = role

    existing_bindings = {
        (binding.role_id, binding.permission_id)
        for binding in db.query(ExecutionRolePermission).all()
    }
    for role_key, permission_keys in DEFAULT_ROLE_PERMISSIONS.items():
        role = roles_by_key[role_key]
        for permission_key in permission_keys:
            permission = permissions_by_key[permission_key]
            key = (role.id, permission.id)
            if key not in existing_bindings:
                db.add(
                    ExecutionRolePermission(
                        role_id=role.id,
                        permission_id=permission.id,
                    )
                )
                existing_bindings.add(key)
    db.flush()
    return roles_by_key


def bind_user_role(
    db: Session,
    user: ExecutionUser,
    role_key: str,
    *,
    created_by_id: Optional[str] = None,
) -> None:
    role = (
        db.query(ExecutionRole)
        .filter(ExecutionRole.key == role_key)
        .one_or_none()
    )
    if role is None:
        raise RuntimeError(f"execution role not seeded: {role_key}")
    exists = (
        db.query(ExecutionUserRole.id)
        .filter(
            ExecutionUserRole.user_id == user.id,
            ExecutionUserRole.role_id == role.id,
        )
        .first()
    )
    if exists is None:
        db.add(
            ExecutionUserRole(
                user_id=user.id,
                role_id=role.id,
                created_by_id=created_by_id,
            )
        )


def ensure_bootstrap_admin(db: Session) -> Optional[ExecutionUser]:
    if db.query(ExecutionUser.id).first() is not None:
        return None
    username = getattr(settings, "EXECUTION_BOOTSTRAP_ADMIN_USERNAME", "")
    password = getattr(settings, "EXECUTION_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        return None
    if len(password) < 10:
        raise RuntimeError("EXECUTION_BOOTSTRAP_ADMIN_PASSWORD must be at least 10 chars")
    user = ExecutionUser(
        username=username,
        display_name="执行系统管理员",
        password_hash=hash_password(password),
        role="admin",
    )
    db.add(user)
    db.flush()
    bind_user_role(db, user, "admin", created_by_id=user.id)
    append_audit_log(
        db,
        action="user.bootstrap",
        resource_type="execution_user",
        resource_id=user.id,
        actor_user_id=user.id,
    )
    return user


def bootstrap_execution_system(db: Session) -> None:
    """Idempotently seed the base catalog and optional environment admin."""

    try:
        ensure_default_rbac(db)
        ensure_default_catalog(db)
        ensure_bootstrap_admin(db)
        db.commit()
    except Exception:
        db.rollback()
        raise


def get_workflow(db: Session, workflow_id: str) -> ExecutionWorkflow:
    workflow = db.get(ExecutionWorkflow, workflow_id)
    if workflow is None:
        raise not_found("流程", workflow_id)
    return workflow


def create_workflow(
    db: Session,
    *,
    actor: ExecutionUser,
    slug: str,
    category_id: str,
    name: str,
    description: Optional[str],
    definition: dict[str, Any],
    capabilities: dict[str, Any],
    is_enabled: bool,
) -> ExecutionWorkflow:
    if db.get(ExecutionCategory, category_id) is None:
        raise not_found("流程分类", category_id)
    validation = validate_definition(definition)
    if not validation.valid:
        raise ExecutionApiError(
            422,
            "workflow_invalid",
            "流程定义校验失败",
            details=validation.as_dict(),
        )
    workflow = ExecutionWorkflow(
        slug=slug,
        category_id=category_id,
        name=name,
        description=description,
        draft_definition=definition,
        capabilities=capabilities,
        required_input_count=_required_input_count(definition),
        is_enabled=is_enabled,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    db.add(workflow)
    try:
        db.flush()
    except IntegrityError as exc:
        raise conflict(
            "workflow_slug_conflict",
            "流程标识已存在",
            slug=slug,
        ) from exc
    append_audit_log(
        db,
        action="workflow.create",
        resource_type="workflow",
        resource_id=workflow.id,
        actor_user_id=actor.id,
    )
    return workflow


def update_workflow_draft(
    db: Session,
    *,
    workflow_id: str,
    expected_revision: int,
    definition: dict[str, Any],
    actor: ExecutionUser,
    name: Optional[str] = None,
    description: Optional[str] = None,
    capabilities: Optional[dict[str, Any]] = None,
    is_enabled: Optional[bool] = None,
) -> ExecutionWorkflow:
    workflow = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.id == workflow_id)
        .with_for_update()
        .one_or_none()
    )
    if workflow is None:
        raise not_found("流程", workflow_id)
    if workflow.draft_revision != expected_revision:
        raise conflict(
            "workflow_revision_conflict",
            "流程草稿已被其他页面修改",
            current_revision=workflow.draft_revision,
            workflow_id=workflow.id,
        )
    validation = validate_definition(definition)
    if not validation.valid:
        raise ExecutionApiError(
            422,
            "workflow_invalid",
            "流程定义校验失败",
            details=validation.as_dict(),
        )
    workflow.draft_definition = definition
    workflow.draft_revision += 1
    workflow.required_input_count = _required_input_count(definition)
    workflow.updated_by_id = actor.id
    if name is not None:
        workflow.name = name
    if description is not None:
        workflow.description = description
    if capabilities is not None:
        workflow.capabilities = capabilities
    if is_enabled is not None:
        workflow.is_enabled = is_enabled
    append_audit_log(
        db,
        action="workflow.draft.update",
        resource_type="workflow",
        resource_id=workflow.id,
        actor_user_id=actor.id,
        details={"revision": workflow.draft_revision},
    )
    return workflow


def publish_workflow(
    db: Session,
    *,
    workflow_id: str,
    expected_revision: int,
    actor: ExecutionUser,
    release_note: Optional[str],
) -> ExecutionWorkflowVersion:
    workflow = (
        db.query(ExecutionWorkflow)
        .filter(ExecutionWorkflow.id == workflow_id)
        .with_for_update()
        .one_or_none()
    )
    if workflow is None:
        raise not_found("流程", workflow_id)
    if workflow.draft_revision != expected_revision:
        raise conflict(
            "workflow_revision_conflict",
            "发布前草稿已发生变化",
            current_revision=workflow.draft_revision,
            workflow_id=workflow.id,
        )
    published_definition = runtime_definition(workflow.draft_definition)
    validation = validate_definition(published_definition, for_publish=True)
    if not validation.valid:
        raise ExecutionApiError(
            422,
            "workflow_not_publishable",
            "流程尚不满足发布条件",
            details=validation.as_dict(),
        )
    candidate_roles = {
        str((node.get("config") or {}).get("candidate_role"))
        for node in published_definition.get("nodes") or []
        if (node.get("config") or {}).get("candidate_role")
    }
    if candidate_roles:
        existing_roles = {
            key
            for (key,) in db.query(ExecutionRole.key)
            .filter(ExecutionRole.key.in_(candidate_roles))
            .all()
        }
        missing_roles = sorted(candidate_roles - existing_roles)
        if missing_roles:
            raise ExecutionApiError(
                422,
                "workflow_candidate_roles_missing",
                "流程引用了不存在的人工任务角色",
                details={"missing_roles": missing_roles},
            )
    published_capabilities = deepcopy(workflow.capabilities or {})
    checksum = definition_checksum(published_definition)
    contract_checksum = workflow_contract_checksum(
        published_definition,
        published_capabilities,
    )
    duplicate = (
        db.query(ExecutionWorkflowVersion)
        .filter(
            ExecutionWorkflowVersion.workflow_id == workflow.id,
            ExecutionWorkflowVersion.contract_checksum == contract_checksum,
            ExecutionWorkflowVersion.version_number
            == workflow.published_version_number,
        )
        .one_or_none()
    )
    if duplicate is not None:
        return duplicate
    version_number = (workflow.published_version_number or 0) + 1
    version = ExecutionWorkflowVersion(
        workflow_id=workflow.id,
        version_number=version_number,
        schema_version=published_definition["schema_version"],
        definition=deepcopy(published_definition),
        checksum=checksum,
        capabilities=published_capabilities,
        contract_checksum=contract_checksum,
        release_note=release_note,
        published_by_id=actor.id,
    )
    db.add(version)
    workflow.published_version_number = version_number
    append_audit_log(
        db,
        action="workflow.publish",
        resource_type="workflow",
        resource_id=workflow.id,
        actor_user_id=actor.id,
        details={
            "version": version_number,
            "checksum": checksum,
            "contract_checksum": contract_checksum,
        },
    )
    db.flush()
    return version


def export_workflow(workflow: ExecutionWorkflow) -> dict[str, Any]:
    capabilities = deepcopy(workflow.capabilities or {})
    return {
        "format": "textile-execution-workflow",
        "format_version": "1.0",
        "workflow": {
            "slug": workflow.slug,
            "name": workflow.name,
            "description": workflow.description,
            "category_key": workflow.category.key,
            "capabilities": capabilities,
        },
        "definition": workflow.draft_definition,
        "checksum": definition_checksum(workflow.draft_definition),
        "contract_checksum": workflow_contract_checksum(
            workflow.draft_definition,
            capabilities,
        ),
    }

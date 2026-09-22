"""Incomplete authoring documents are editable; only releases are executable."""
from copy import deepcopy
import json
import re

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.execution.errors import ExecutionApiError, conflict, not_found
from app.execution.events import append_audit_log
from app.execution.models import ExecutionCategory, ExecutionWorkflow, ExecutionWorkflowRelease
from app.execution.workflow_replacement import assert_not_archived


def _document(document):
    if (not isinstance(document, dict) or document.get("format") != "textile-workflow-release"
            or not isinstance(document.get("release"), dict)
            or not isinstance(document.get("definition"), dict)
            or not isinstance(document["definition"].get("nodes"), list)
            or not isinstance(document["definition"].get("edges"), list)):
        raise ExecutionApiError(422, "designer_draft_invalid", "草稿需要流程信息、节点列表和连接列表")
    if len(json.dumps(document, ensure_ascii=False, allow_nan=False).encode()) > settings.EXECUTION_RELEASE_JSON_MAX_BYTES:
        raise ExecutionApiError(413, "designer_draft_too_large", "草稿超过文件大小限制")
    result = deepcopy(document)
    result.pop("integrity", None)
    slug = result["release"].get("slug", "")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", slug):
        raise ExecutionApiError(422, "workflow_slug_invalid", "标识须以小写字母开头，使用字母、数字、点、下划线或连字符")
    name = result["release"].get("name", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ExecutionApiError(422, "workflow_name_invalid", "请填写 1–200 字的流程名称")
    return result


def view_draft(db, workflow):
    assert_not_archived(workflow)
    draft = deepcopy(workflow.designer_draft)
    if not draft:
        version = next((v for v in workflow.versions if v.version_number == workflow.published_version_number), None)
        release = db.get(ExecutionWorkflowRelease, version.release_id) if version and version.release_id else None
        if not release:
            raise not_found("设计草稿", workflow.id)
        draft = {"document": deepcopy(release.portable_document), "bindings": {}}
    latest = db.query(func.max(ExecutionWorkflowRelease.source_version)).filter_by(source_slug=workflow.slug).scalar() or 0
    draft["document"]["release"]["release_version"] = max(latest + 1, draft["document"]["release"].get("release_version", 1))
    return {"workflow_id": workflow.id, "revision": workflow.draft_revision, **draft,
            "published_version": workflow.published_version_number}


def get_draft(db, workflow_id):
    workflow = db.get(ExecutionWorkflow, workflow_id)
    if workflow is None:
        raise not_found("流程", workflow_id)
    return view_draft(db, workflow)


def save_draft(db, *, document, bindings, actor, workflow_id=None, revision=None):
    document = _document(document)
    category = db.query(ExecutionCategory).filter_by(key=document["release"].get("category_key", "other")).one_or_none()
    if category is None:
        raise ExecutionApiError(422, "workflow_category_invalid", "流程分类不存在")
    if workflow_id:
        workflow = db.query(ExecutionWorkflow).filter_by(id=workflow_id).populate_existing().with_for_update().one_or_none()
        if workflow is None:
            raise not_found("流程", workflow_id)
        assert_not_archived(workflow)
        if revision != workflow.draft_revision:
            raise conflict("draft_revision_changed", "草稿已由其他页面更新；当前修改已保留，请重新载入后合并", current_revision=workflow.draft_revision)
        if document["release"]["slug"] != workflow.slug:
            raise ExecutionApiError(422, "workflow_slug_changed", "已保存流程的标识不能更改，请复制为新流程")
        workflow.draft_revision += 1
    else:
        base = document["release"]["slug"]
        slug, suffix = base, 2
        while db.query(ExecutionWorkflow.id).filter_by(slug=slug).first():
            slug = f"{base[:94]}-{suffix}"; suffix += 1
        document["release"]["slug"] = slug
        workflow = ExecutionWorkflow(slug=slug, name=document["release"]["name"], category_id=category.id,
            management_mode="release_v2", is_enabled=True, draft_definition=deepcopy(document["definition"]),
            draft_revision=1, created_by_id=actor.id)
        db.add(workflow)
    workflow.name = document["release"]["name"].strip()
    workflow.description = document["release"].get("description", "")
    workflow.category_id = category.id
    workflow.updated_by_id = actor.id
    workflow.designer_draft = {"document": document, "bindings": deepcopy(bindings)}
    try:
        db.flush()
    except IntegrityError as exc:
        raise conflict("draft_identity_conflict", "另一页面刚创建了同名流程，请重新保存") from exc
    append_audit_log(db, action="workflow.draft_saved", resource_type="execution_workflow",
                     resource_id=workflow.id, actor_user_id=actor.id, details={"revision": workflow.draft_revision})
    return view_draft(db, workflow)

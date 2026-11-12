"""Deploy bundled native workflows and retire known v1 entries; never runs them.

Run ``python -m app.execution.deploy_builtin_workflows`` inside backend. The
default fully rehearses publication in a transaction and rolls it back. --apply
commits the same checks and changes together. --sync-existing explicitly
updates and enables the two bundled v2 workflows. Files and historical rows
are never deleted; newer or conflicting portable releases are never replaced.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.execution.errors import ExecutionApiError
from app.execution.events import append_audit_log
from app.execution.models import (
    ExecutionCredential, ExecutionDeploymentBinding, ExecutionExternalOperation,
    ExecutionHumanTask, ExecutionRun, ExecutionStorageRoot, ExecutionUser,
    ExecutionWorkflow, ExecutionWorkflowRelease, ExecutionWorkflowVersion, utcnow,
)
from app.execution.release_v2 import (
    _release_digest, apply_release, preflight_release, publish_release,
    put_deployment_binding,
)
from app.execution.security import require_permission
from app.execution.v2.drafts import _document, save_draft


LEGACY_SLUGS = frozenset({
    "regenerated-fiber-count-method", "regenerated-fiber-area-method",
    "system-controlled-xlsx-write-test", "special-wool-source-selection",
    "electron-microscopy-gbt36422", "electron-cross-section-gbt36422",
    "electron-source-selection", "paper-fiber-gbt4688-2020-qualitative",
    "hemp-cotton-source-selection",
})
BUILTIN_SLUGS = ("fiber-microscopy-v2", "paper-fiber-v2")
RESOURCE_DIRECTORY = Path(__file__).parent / "v2/resources/workflows"


def fail(code: str, message: str, **details) -> None:
    raise ExecutionApiError(409, code, message, details=details)


def _actor(db: Session, username: str | None) -> ExecutionUser:
    query = db.query(ExecutionUser).filter(ExecutionUser.is_active.is_(True))
    rows = query.filter(ExecutionUser.username == username).all() if username else query.filter(ExecutionUser.role == "admin").all()
    if len(rows) != 1:
        fail("deployment_operator_required", "请使用 --operator 指定唯一有效管理员账号", candidates=[row.username for row in rows])
    actor = rows[0]
    require_permission(db, actor, "workflow.publish")
    return actor


def _credential(db: Session, actor: ExecutionUser, credential_id: str | None) -> ExecutionCredential:
    query = db.query(ExecutionCredential).filter(
        ExecutionCredential.user_id == actor.id,
        ExecutionCredential.system_key == "legacy_inspection",
        ExecutionCredential.is_active.is_(True),
    )
    rows = query.filter(ExecutionCredential.id == credential_id).all() if credential_id else query.all()
    if len(rows) != 1:
        fail("deployment_credential_required", "管理员需要一个有效的个人检务凭据；请先在网页配置，或用 --credential 指定", credential_ids=[row.id for row in rows])
    return rows[0]


def _check_idle(db: Session, workflow_ids: list[str]) -> None:
    if not workflow_ids:
        return
    runs = db.query(ExecutionRun.id).filter(ExecutionRun.workflow_id.in_(workflow_ids))
    active_runs = runs.filter(ExecutionRun.status.notin_({"completed", "failed", "cancelled"})).all()
    operations = db.query(ExecutionExternalOperation.id).filter(
        ExecutionExternalOperation.run_id.in_(runs),
        ExecutionExternalOperation.status.in_({"prepared", "approved", "in_progress", "cancel_pending", "reconciliation_required"}),
    ).all()
    tasks = db.query(ExecutionHumanTask.id).filter(
        ExecutionHumanTask.run_id.in_(runs),
        ExecutionHumanTask.status.in_({"open", "claimed"}),
    ).all()
    if active_runs or operations or tasks:
        fail("deployment_work_in_progress", "旧流程仍有在途运行、待办或未收尾外部操作，请先正常收尾后重试；本命令不会取消或重传", run_ids=[r[0] for r in active_runs], operation_ids=[r[0] for r in operations], task_ids=[r[0] for r in tasks])


def _bindings(db: Session, document: dict, credential: ExecutionCredential, overrides: dict[str, str]) -> dict:
    roots = {}
    for slot in document["resources"]["root_slots"]:
        root_id = overrides.get(slot["slot_id"], slot["slot_id"])
        root = db.query(ExecutionStorageRoot).filter(ExecutionStorageRoot.root_id == root_id).one_or_none()
        if root is None or not root.is_active or not root.is_available or not Path(root.local_path).is_dir():
            fail("deployment_root_unavailable", "目录未配置或不可用，请先核对挂载并在网页刷新目录状态", slot=slot["slot_id"], root_id=root_id)
        roots[slot["slot_id"]] = {"root_id": root.root_id, "revision": int(root.binding_revision or 1)}
    return {
        "root_slots": roots,
        "credential_slots": {slot["slot_id"]: {"credential_id": credential.id, "revision": credential.revision} for slot in document["resources"]["credential_slots"]},
        "role_slots": {}, "rule_slots": {},
    }


def _require_report(report: dict, *, publish: bool, slug: str) -> None:
    missing_worker = [issue for issue in report["issues"] if issue["code"] == "node_capability_unavailable"]
    if not report["publish_ready" if publish else "content_valid"] or (publish and missing_worker):
        fail("deployment_preflight_failed", "流程预检未通过，请修复以下问题后重试", slug=slug, issues=report["issues"])


def deploy(db: Session, *, apply: bool = False, operator: str | None = None,
           credential_id: str | None = None, root_overrides: dict[str, str] | None = None,
           sync_existing: bool = False) -> dict:
    """Own the transaction: commit once on success, otherwise always roll back."""
    try:
        if db.bind.dialect.name == "postgresql":
            # Serialize this maintenance command even before target rows exist.
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
            db.execute(text("SELECT pg_advisory_xact_lock(824310, 2)"))
        actor = _actor(db, operator)
        credential = _credential(db, actor, credential_id)
        documents = [json.loads((RESOURCE_DIRECTORY / f"{slug}.json").read_text(encoding="utf-8")) for slug in BUILTIN_SLUGS]
        declared_slots = {slot["slot_id"] for doc in documents for slot in doc["resources"]["root_slots"]}
        overrides = root_overrides or {}
        if set(overrides) - declared_slots:
            fail("deployment_unknown_slot", "--root 引用了未声明的目录槽", slots=sorted(set(overrides) - declared_slots))
        locked = db.query(ExecutionWorkflow).filter(ExecutionWorkflow.slug.in_(LEGACY_SLUGS | set(BUILTIN_SLUGS))).order_by(ExecutionWorkflow.id).populate_existing().with_for_update().all()
        legacy = [row for row in locked if row.slug in LEGACY_SLUGS and row.management_mode == "draft_v1"]
        targets = {row.slug: row for row in locked if row.slug in BUILTIN_SLUGS}
        retiring = any(row.archived_at is None or row.is_enabled for row in legacy)
        _check_idle(db, [row.id for row in legacy])
        result = {"applied": apply, "sync_existing": sync_existing, "operator": actor.username, "workflows": [], "retired": [], "history_preserved": True}
        for document in documents:
            slug = document["release"]["slug"]
            current = targets.get(slug)
            version = None
            if current is not None:
                version = db.query(ExecutionWorkflowVersion).filter(ExecutionWorkflowVersion.workflow_id == current.id, ExecutionWorkflowVersion.version_number == current.published_version_number).one_or_none()
                if current.management_mode != "release_v2" or current.archived_at is not None or version is None:
                    fail("deployment_existing_workflow", "目标流程已有其他版本或草稿，本次不会覆盖；请通过设计器核对并发布", slug=slug, workflow_id=current.id)
                installed_release = db.get(ExecutionWorkflowRelease, version.release_id)
                if installed_release is None or installed_release.release_digest != version.release_digest:
                    fail("deployment_existing_workflow", "当前流程的发布记录不完整或摘要不一致，请先核对", slug=slug)
                source_version = document["release"]["release_version"]
                if installed_release.source_version > source_version:
                    fail("deployment_newer_release", "当前流程的来源版本高于安装包，本次不会降级；请更新代码与镜像", slug=slug, installed_source_version=installed_release.source_version, bundled_source_version=source_version)
                if installed_release.source_version == source_version and version.release_digest != _release_digest(document):
                    fail("deployment_release_conflict", "相同来源版本对应不同流程内容，不能覆盖；请核对安装包与已发布版本", slug=slug, source_version=source_version)
                if not sync_existing and version.release_digest != _release_digest(document):
                    fail("deployment_existing_workflow", "已有不同的 v2 版本；确认使用安装包中的两条流程后，加 --sync-existing 同步", slug=slug, workflow_id=current.id)
                if (retiring or sync_existing) and (current.availability_code is not None or (not current.is_enabled and not sync_existing)):
                    fail("deployment_target_unavailable", "接替流程已停用或不可运行，不能归档旧入口；请先核对目标流程状态", slug=slug, workflow_id=current.id, is_enabled=current.is_enabled, availability_code=current.availability_code)
            content = preflight_release(db, document=document, actor=actor)
            _require_report(content, publish=False, slug=slug)
            release = apply_release(db, document=None, preflight_token=content["preflight_token"], actor=actor)
            existing_binding = db.query(ExecutionDeploymentBinding).filter(ExecutionDeploymentBinding.release_id == release.id, ExecutionDeploymentBinding.environment == settings.EXECUTION_ENVIRONMENT_ID).order_by(ExecutionDeploymentBinding.revision.desc()).first()
            raw_bindings = _bindings(db, document, credential, overrides)
            binding = put_deployment_binding(db, release_id=release.id, environment=settings.EXECUTION_ENVIRONMENT_ID,
                expected_revision=existing_binding.revision if existing_binding else 0,
                bindings=raw_bindings, actor=actor)
            if version is not None and not sync_existing and binding.digest != version.deployment_binding_digest:
                fail("deployment_existing_binding", "已发布流程的绑定不同，本次不会覆盖；请通过工作流发布页核对", slug=slug)
            report = preflight_release(db, document=document, release=release, scope="publish", actor=actor)
            _require_report(report, publish=True, slug=slug)
            if version is not None and not sync_existing and report["resolved_dependencies"]["digest"] != version.dependency_lock_digest:
                fail("deployment_existing_dependency", "已发布流程冻结的执行能力不同，请通过工作流发布页更新", slug=slug)
            publishing = version is None or any((
                version.release_digest != release.release_digest,
                binding.digest != version.deployment_binding_digest,
                report["resolved_dependencies"]["digest"] != version.dependency_lock_digest,
            ))
            if publishing:
                _, version, _ = publish_release(db, release_id=release.id, preflight_token=report["preflight_token"], actor=actor, reason="部署原生流程并退役旧业务入口")
                # The UI does not share our maintenance lock. If it published
                # this initially absent target meanwhile, do not append over it.
                if current is None and version.version_number != 1:
                    fail("deployment_target_changed", "目标流程已被并发发布，本次已回滚；请核对后重试", slug=slug)
            target = current or db.get(ExecutionWorkflow, version.workflow_id)
            enabled = sync_existing and not target.is_enabled
            if enabled:
                target.is_enabled = True
                target.draft_revision += 1
                target.updated_by_id = actor.id
                append_audit_log(db, action="workflow.builtin_enable", resource_type="workflow", resource_id=target.id, actor_user_id=actor.id,
                    details={"slug": slug, "release_digest": version.release_digest, "is_enabled": True})
            draft_changed = sync_existing and target.designer_draft != {"document": _document(document), "bindings": raw_bindings}
            if draft_changed:
                # save_draft reloads the locked row; persist our revision and
                # enable changes first, inside this same rollbackable transaction.
                db.flush()
                save_draft(db, document=document, bindings=raw_bindings, actor=actor,
                    workflow_id=target.id, revision=target.draft_revision)
            result["workflows"].append({"slug": slug, "workflow_id": version.workflow_id, "version": version.version_number,
                "release_version": document["release"]["release_version"], "release_digest": version.release_digest,
                "dependency_lock_digest": version.dependency_lock_digest, "deployment_binding_digest": version.deployment_binding_digest,
                "worker_ready": True, "unchanged": not publishing and not enabled and not draft_changed,
                "draft_changed": draft_changed, "is_enabled": target.is_enabled})
        for workflow in legacy:
            changed = workflow.archived_at is None or workflow.is_enabled
            if changed:
                before = {"is_enabled": workflow.is_enabled, "archived_at": workflow.archived_at.isoformat() if workflow.archived_at else None, "draft_revision": workflow.draft_revision}
                workflow.archived_at = workflow.archived_at or utcnow()
                workflow.is_enabled = False
                workflow.draft_revision += 1
                workflow.updated_by_id = actor.id
                append_audit_log(db, action="workflow.legacy_retire", resource_type="workflow", resource_id=workflow.id, actor_user_id=actor.id,
                    details={"slug": workflow.slug, "before": before, "replacement_slugs": list(BUILTIN_SLUGS), "history_preserved": True})
            result["retired"].append({"slug": workflow.slug, "workflow_id": workflow.id, "changed": changed})
        db.flush()
        changed = any(not item["unchanged"] for item in result["workflows"]) or any(item["changed"] for item in result["retired"])
        if apply and changed:
            db.commit()
        else:
            db.rollback()
        return result
    except Exception:
        db.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="提交事务；默认完整预演后回滚")
    parser.add_argument("--sync-existing", action="store_true", help="明确同步并启用安装包中的两条 v2 流程；不降级或覆盖同版本异内容")
    parser.add_argument("--operator", help="执行系统管理员用户名")
    parser.add_argument("--credential", help="该管理员的个人检务凭据 ID")
    parser.add_argument("--root", action="append", default=[], metavar="SLOT=ROOT_ID")
    args = parser.parse_args()
    overrides = {}
    for value in args.root:
        if "=" not in value or not all(value.split("=", 1)):
            parser.error("--root 格式为 slot=root_id")
        slot, root_id = value.split("=", 1)
        if slot in overrides:
            parser.error("同一个目录槽不能重复指定")
        overrides[slot] = root_id
    from app.database import SessionLocal
    try:
        with SessionLocal() as db:
            report = deploy(db, apply=args.apply, operator=args.operator, credential_id=args.credential, root_overrides=overrides, sync_existing=args.sync_existing)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except ExecutionApiError as exc:
        print(json.dumps({"applied": False, "code": exc.code, "message": exc.message, "details": exc.details}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

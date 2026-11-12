"""Remove retired built-in workflows and their database records at deployment.

Files, accounts, credentials, directory settings and unrelated workflows are
never deleted. The caller commits the complete deployment transaction.
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.execution.models import (
    ExecutionArtifact, ExecutionArtifactRelation, ExecutionAuditLog,
    ExecutionDeploymentBinding, ExecutionEdgeRun, ExecutionEvent,
    ExecutionExternalAttempt, ExecutionExternalOperation, ExecutionFileMutation,
    ExecutionHumanApprovalReceipt, ExecutionHumanApprovalReceiptConsumption,
    ExecutionHumanTask, ExecutionNodeAttempt, ExecutionNodeRun, ExecutionOutbox,
    ExecutionPublishReceipt, ExecutionReleasePreflight, ExecutionRun,
    ExecutionWorkflow, ExecutionWorkflowActivationReceipt, ExecutionWorkflowRelease,
    ExecutionWorkflowVersion,
)


LEGACY_SLUGS = frozenset({
    "regenerated-fiber-count-method", "regenerated-fiber-area-method",
    "system-controlled-xlsx-write-test", "special-wool-source-selection",
    "electron-microscopy-gbt36422", "electron-cross-section-gbt36422",
    "electron-source-selection", "paper-fiber-gbt4688-2020-qualitative",
    "hemp-cotton-source-selection",
})


def purge_legacy_workflows(db: Session, workflows=None, *, slugs=LEGACY_SLUGS) -> dict:
    """Delete the retired set, including child runs; never commit or touch disk."""
    def ids(model, predicate):
        return {row[0] for row in db.query(model.id).filter(predicate).all()}

    counts = {}

    def remove(model, predicate):
        count = db.query(model).filter(predicate).delete(synchronize_session=False)
        if count:
            counts[model.__tablename__] = count

    selected_slugs = set(slugs) & LEGACY_SLUGS
    if workflows is None:
        workflows = db.query(ExecutionWorkflow).filter(
            ExecutionWorkflow.slug.in_(selected_slugs),
        ).order_by(ExecutionWorkflow.id).with_for_update().all()
    else:
        workflows = [row for row in workflows if row.slug in selected_slugs]
    workflow_ids = {row.id for row in workflows}
    removed = [{"workflow_id": row.id, "slug": row.slug} for row in workflows]
    version_ids = ids(ExecutionWorkflowVersion, ExecutionWorkflowVersion.workflow_id.in_(workflow_ids))
    release_ids = ids(ExecutionWorkflowRelease, or_(
        ExecutionWorkflowRelease.workflow_id.in_(workflow_ids),
        ExecutionWorkflowRelease.source_slug.in_(selected_slugs),
    ))
    run_ids = ids(ExecutionRun, or_(ExecutionRun.workflow_id.in_(workflow_ids),
        ExecutionRun.workflow_version_id.in_(version_ids)))
    while run_ids:
        children = ids(ExecutionRun, ExecutionRun.parent_run_id.in_(run_ids)) - run_ids
        if not children:
            break
        run_ids |= children
    node_ids = ids(ExecutionNodeRun, ExecutionNodeRun.run_id.in_(run_ids))
    task_ids = ids(ExecutionHumanTask, ExecutionHumanTask.run_id.in_(run_ids))
    operation_ids = ids(ExecutionExternalOperation, ExecutionExternalOperation.run_id.in_(run_ids))
    artifact_ids = ids(ExecutionArtifact, or_(ExecutionArtifact.run_id.in_(run_ids), ExecutionArtifact.node_run_id.in_(node_ids)))
    mutation_ids = ids(ExecutionFileMutation, ExecutionFileMutation.run_id.in_(run_ids))
    approval_ids = ids(ExecutionHumanApprovalReceipt, ExecutionHumanApprovalReceipt.run_id.in_(run_ids))
    binding_ids = ids(ExecutionDeploymentBinding, ExecutionDeploymentBinding.release_id.in_(release_ids))
    preflight_ids = ids(ExecutionReleasePreflight, or_(
        ExecutionReleasePreflight.release_id.in_(release_ids),
        ExecutionReleasePreflight.portable_document["release"]["slug"].as_string().in_(selected_slugs),
    ))

    # A file artifact reused by another workflow belongs to that surviving
    # record too. Preserve it; deleting its run only detaches that identity.
    shared_artifacts = set()
    for source_id, working_id in db.query(ExecutionFileMutation.source_artifact_id, ExecutionFileMutation.working_artifact_id).filter(
        ~ExecutionFileMutation.id.in_(mutation_ids),
        or_(ExecutionFileMutation.source_artifact_id.in_(artifact_ids), ExecutionFileMutation.working_artifact_id.in_(artifact_ids)),
    ).all():
        shared_artifacts.update({source_id, working_id} & artifact_ids)
    shared_artifacts.update(row[0] for row in db.query(ExecutionPublishReceipt.artifact_id).filter(
        ~ExecutionPublishReceipt.mutation_id.in_(mutation_ids), ExecutionPublishReceipt.artifact_id.in_(artifact_ids),
    ).all())
    artifact_ids -= shared_artifacts

    evidence_ids = workflow_ids | version_ids | release_ids | run_ids | node_ids | task_ids | operation_ids | artifact_ids | mutation_ids | approval_ids | binding_ids | preflight_ids
    remove(ExecutionAuditLog, ExecutionAuditLog.resource_id.in_(evidence_ids))
    remove(ExecutionOutbox, ExecutionOutbox.aggregate_id.in_(evidence_ids))
    remove(ExecutionHumanApprovalReceiptConsumption, or_(ExecutionHumanApprovalReceiptConsumption.run_id.in_(run_ids), ExecutionHumanApprovalReceiptConsumption.approval_receipt_id.in_(approval_ids)))
    remove(ExecutionHumanApprovalReceipt, ExecutionHumanApprovalReceipt.id.in_(approval_ids))
    remove(ExecutionPublishReceipt, ExecutionPublishReceipt.mutation_id.in_(mutation_ids))
    remove(ExecutionArtifactRelation, or_(ExecutionArtifactRelation.parent_artifact_id.in_(artifact_ids), ExecutionArtifactRelation.child_artifact_id.in_(artifact_ids)))
    remove(ExecutionExternalAttempt, ExecutionExternalAttempt.operation_id.in_(operation_ids))
    remove(ExecutionExternalOperation, ExecutionExternalOperation.id.in_(operation_ids))
    remove(ExecutionFileMutation, ExecutionFileMutation.id.in_(mutation_ids))
    remove(ExecutionArtifact, ExecutionArtifact.id.in_(artifact_ids))
    remove(ExecutionHumanTask, ExecutionHumanTask.id.in_(task_ids))
    remove(ExecutionNodeAttempt, ExecutionNodeAttempt.node_run_id.in_(node_ids))
    remove(ExecutionEdgeRun, ExecutionEdgeRun.run_id.in_(run_ids))
    remove(ExecutionEvent, ExecutionEvent.run_id.in_(run_ids))
    remove(ExecutionNodeRun, ExecutionNodeRun.id.in_(node_ids))
    # Clear self links for databases with immediate foreign-key checks.
    db.query(ExecutionRun).filter(ExecutionRun.id.in_(run_ids)).update({ExecutionRun.parent_run_id: None}, synchronize_session=False)
    remove(ExecutionRun, ExecutionRun.id.in_(run_ids))
    remove(ExecutionWorkflowActivationReceipt, or_(ExecutionWorkflowActivationReceipt.workflow_id.in_(workflow_ids), ExecutionWorkflowActivationReceipt.release_id.in_(release_ids)))
    remove(ExecutionReleasePreflight, ExecutionReleasePreflight.id.in_(preflight_ids))
    remove(ExecutionDeploymentBinding, ExecutionDeploymentBinding.release_id.in_(release_ids))
    remove(ExecutionWorkflowVersion, ExecutionWorkflowVersion.id.in_(version_ids))
    remove(ExecutionWorkflowRelease, ExecutionWorkflowRelease.id.in_(release_ids))
    db.query(ExecutionWorkflow).filter(ExecutionWorkflow.replaces_workflow_id.in_(workflow_ids)).update({ExecutionWorkflow.replaces_workflow_id: None}, synchronize_session=False)
    remove(ExecutionWorkflow, ExecutionWorkflow.id.in_(workflow_ids))
    db.flush()
    db.expire_all()
    return {"removed": removed, "deleted_counts": counts}

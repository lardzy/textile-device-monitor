#!/usr/bin/env python3
"""One-off execution reset. Run with workers/API stopped after taking a backup.

Reads DATABASE_URL from the environment. Default is a read-only inventory;
--apply deletes only the explicit execution-owned tables below, in one transaction.
Source files, template files, users, credentials and storage roots are never deleted.
"""
import argparse
import json
import os

from sqlalchemy import MetaData, create_engine, delete, func, inspect, select, update


TABLES = (
    "execution_human_approval_receipt_consumptions",
    "execution_publish_receipts", "execution_human_approval_receipts",
    "execution_artifact_relations", "execution_external_attempts",
    "execution_external_operations", "execution_file_mutations",
    "execution_artifacts", "execution_human_tasks", "execution_node_attempts",
    "execution_edge_runs", "execution_events", "execution_outbox", "execution_audit_logs",
    "execution_node_runs", "execution_runs",
    "execution_workflow_activation_receipts", "execution_release_preflights",
    "execution_deployment_bindings", "execution_workflow_versions",
    "execution_workflow_releases", "execution_workflows",
    "execution_project_rules", "execution_task_snapshot_cache",
    "execution_index_jobs", "execution_file_index_entries",
    "execution_worker_node_capabilities", "execution_worker_heartbeats",
)
TERMINAL = {"completed", "failed", "cancelled", "expired"}


def reset(connection, *, apply=False):
    names = set(inspect(connection).get_table_names())
    metadata = MetaData()
    metadata.reflect(bind=connection)
    selected = [name for name in TABLES if name in names]
    counts = lambda names: {
        name: connection.execute(select(func.count()).select_from(metadata.tables[name])).scalar_one()
        for name in sorted(names)
    }
    report = {"apply": apply, "before": counts(selected), "preserved": counts(names - set(selected))}
    for name in ("execution_runs", "execution_external_operations"):
        if name in names:
            table = metadata.tables[name]
            active = connection.execute(select(func.count()).select_from(table).where(~table.c.status.in_(TERMINAL))).scalar_one()
            if active:
                raise RuntimeError(f"{name} has {active} active records; finish/cancel them before reset")
    if apply:
        # Releases/versions/workflows have mutually referencing identities.
        for name, column in (("execution_workflow_versions", "release_id"),
                             ("execution_workflow_releases", "workflow_id"),
                             ("execution_workflows", "replaces_workflow_id")):
            if name in names and column in metadata.tables[name].c:
                connection.execute(update(metadata.tables[name]).values({column: None}))
        for name in selected:
            connection.execute(delete(metadata.tables[name]))
        report["after"] = counts(selected)
        if counts(names - set(selected)) != report["preserved"]:
            raise RuntimeError("A protected table changed during reset")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with create_engine(os.environ["DATABASE_URL"]).begin() as connection:
        report = reset(connection, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

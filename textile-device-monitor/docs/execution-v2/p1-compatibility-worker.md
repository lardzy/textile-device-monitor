# P1 compatibility Worker image gate

P2 does not reinterpret an existing P1 execution binding with current code.
The compatibility pool must be built from commit
`d1fb01bbd12e3b020f1e27a2ecbeab20bf039ac4`; the only additions to
that source tree are the self-contained Alembic revisions
`0009_execution_v2_primitives.py`, `0010_workflow_replacement.py`, and
`0011_connector_operations.py`, and `0012_connector_record_reads.py`, so its schema-head guard can share the current
database.

Before the image is admitted, run the Worker bootstrap self-check and compare
the complete capability document with the frozen deployment record at
`backend/app/execution/v2/resources/snapshots/p1-worker-d1fb01b.json`.
The required identity is protocol `2.0`, engine `2.0.0`, 39 bindings, 37 ready,
and capability digest
`7c3849d8ad705fbff9c9cc7cb706f27ef3805f38323616bdb99fc3b25688c4f1`.

If any field differs, the image must not join the P1 pool. P2 Workers advertise
protocol `2.1` and their own implementation digests; they must never be
labelled with this P1 capability identity.

Run the non-mutating gate from the project root:

```bash
python tools/execution_v2_p1_compat_image.py
```

Build the isolated image only when Docker is available and the deployment tag
has been chosen explicitly:

```bash
python tools/execution_v2_p1_compat_image.py \
  --build --tag textile-execution-worker:p1-d1fb01b
```

The tool creates a temporary detached worktree from the exact baseline, copies
only the three migration files above into it, builds `backend/Dockerfile`, and
boots the built Worker far enough to compare its capability identity with the
frozen snapshot, and then removes the temporary worktree. A mismatch fails the
build gate before the image can join the compatibility pool.

Inventory actual bindings before configuring this pool. Old v1 Runs with no
exact binding retain the compatibility execution path; the frozen P1 image is
needed only when deployed versions or unfinished Runs carry P1 bindings.
Kernel Pack 2.1.1 added grouped-file input support to `human.select@1`.
The 2026-09-17 native domain batch uses Engine 2.3.0, Kernel Pack 2.1.2,
textile.execution-v1-compat 2.1.3, legacy_fibrecheck.v1-compat 1.0.1,
textile.regenerated-fiber 1.0.0 and textile.domain-records 1.0.0
(protocol 2.1; 7 Packs, 63 bindings, 61 ready).

The P4 query batch uses Engine 2.4.0, Kernel Pack 2.2.0 and Legacy Connector
Pack 1.1.0 (protocol 2.1; 7 Packs, 64 bindings, 62 ready). A `connector.query`
binding includes the exact QuerySpec and connector implementation; advertising
the generic shell alone does not make a Worker eligible. The database head
and frozen P1 capability document are unchanged.

The shared regenerated-fiber, microscopy and paper-fiber services change the compatibility Pack's implementation
identity without rewriting existing frozen identities. If an environment
has deployed/unfinished older P2 bindings, retain its frozen Worker as well,
with migration overlays through 0011;
never relabel the new Worker with an old capability digest. Validate each
retained image against the shared migrated database before switching entry points.

The 2026-09-18 standalone operation batch uses Kernel Pack 2.3.0 and Legacy
Connector Pack 1.2.0. Migration 0011 adds optional Run/node links and operation
ownership. Only the current main Worker maintains standalone operation leases;
the P1 pool continues to serve its frozen workflow bindings. API and main
Worker must be updated together. No P1 application or capability bytes change.

The record-read batch adds migration 0012 for two disposable cache fields. The
frozen P1 application and capability bytes are unchanged. Current Kernel Pack
2.4.0 and Legacy Connector Pack 1.3.0 expose 66 bindings, 64 ready.

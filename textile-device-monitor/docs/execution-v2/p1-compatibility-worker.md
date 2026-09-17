# P1 compatibility Worker image gate

P2 does not reinterpret an existing P1 execution binding with current code.
The compatibility pool must be built from commit
`d1fb01bbd12e3b020f1e27a2ecbeab20bf039ac4`; the only additions to
that source tree are the self-contained Alembic revisions
`0009_execution_v2_primitives.py` and `0010_workflow_replacement.py`, so its schema-head guard can share the P2
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
only the two migration files above into it, builds `backend/Dockerfile`, and
boots the built Worker far enough to compare its capability identity with the
frozen snapshot, and then removes the temporary worktree. A mismatch fails the
build gate before the image can join the compatibility pool.

Inventory actual bindings before configuring this pool. Old v1 Runs with no
exact binding retain the compatibility execution path; the frozen P1 image is
needed only when deployed versions or unfinished Runs carry P1 bindings.
Kernel Pack 2.1.1 added grouped-file input support to `human.select@1`.
The 2026-09-17 P3 batch uses Engine 2.2.0, Kernel Pack 2.1.2,
textile.execution-v1-compat 2.1.1, legacy_fibrecheck.v1-compat 1.0.1 and
textile.regenerated-fiber 1.0.0 (protocol 2.1; 59 bindings, 57 ready).
The shared regenerated-fiber services change the compatibility Pack's implementation
identity without rewriting existing frozen identities. If an environment
has deployed/unfinished older P2 bindings, retain its frozen Worker as well,
with the 0010 schema overlay;
never relabel the new Worker with an old capability digest. Validate each
retained image against the shared migrated database before switching entry points.

#!/usr/bin/env python3
"""Check or explicitly update frozen Execution v2 Pack digests.

The default mode is read-only.  ``--update`` is intentionally required before
the script rewrites a manifest.  Contract digests cover canonical NodeSpec
documents; implementation digests cover only the manifest-declared executable
resources; distribution digests cover the final manifest and every Pack
resource byte.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
MANIFEST_ROOT = (
    BACKEND_ROOT
    / "app"
    / "execution"
    / "v2"
    / "resources"
    / "manifests"
)
sys.path.insert(0, str(BACKEND_ROOT))

from app.execution.registry import node_registry  # noqa: E402
from app.execution.v2.canonical import canonical_sha256  # noqa: E402
from app.execution.v2.registry import (  # noqa: E402
    InstalledPack,
    _compat_node_spec,
    _implementation_digest,
    _manifest_distribution_digest,
    _node_spec_from_resource,
    _trusted_renderer_contracts,
)


def _render(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _freeze_manifest(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result.pop("distribution_digest", None)
    pack = InstalledPack(
        pack_id=result["pack_id"],
        pack_version=result["pack_version"],
        engine_version_range=result["engine_version_range"],
        distribution_digest="0" * 64,
        ready=True,
        manifest=result,
    )
    current_nodes = {
        (item.type, item.version): item for item in node_registry.all()
    }
    for descriptor in result["provides"]["nodes"]:
        identity = (descriptor["type"], descriptor["type_version"])
        if descriptor["source"] == "v1_registry_adapter":
            node_type = current_nodes.get(identity)
            if node_type is None:
                raise ValueError(f"unknown v1 NodeSpec adapter: {identity}")
            spec = _compat_node_spec(
                node_type,
                pack=pack,
                handler_channel=descriptor["handler_channel"],
            )
        else:
            spec = _node_spec_from_resource(
                str(descriptor["spec_resource"]), identity
            )
        contract_digest = canonical_sha256(spec)
        descriptor["contract_digest"] = contract_digest
        descriptor["implementation_digest"] = _implementation_digest(
            pack=pack,
            descriptor=descriptor,
            contract_digest=contract_digest,
        )

    trusted_renderers = _trusted_renderer_contracts()
    for renderer in result["provides"].get("renderers") or []:
        identity = (renderer["capability"], renderer["version"])
        try:
            renderer["contract_digest"] = trusted_renderers[identity][
                "contract_digest"
            ]
        except KeyError as exc:
            raise ValueError(f"unknown trusted renderer: {identity}") from exc

    result["distribution_digest"] = _manifest_distribution_digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite manifests with the currently computed frozen digests",
    )
    args = parser.parse_args()
    changed: list[Path] = []
    for path in sorted(MANIFEST_ROOT.glob("*.json")):
        current = json.loads(path.read_text(encoding="utf-8"))
        expected = _freeze_manifest(current)
        expected_bytes = _render(expected)
        if path.read_bytes() == expected_bytes:
            continue
        changed.append(path)
        if args.update:
            path.write_bytes(expected_bytes)
    if changed and not args.update:
        names = ", ".join(path.name for path in changed)
        print(f"Execution v2 Pack digests are stale: {names}", file=sys.stderr)
        print("run with --update after reviewing resource changes", file=sys.stderr)
        return 1
    if changed:
        print(f"updated {len(changed)} Execution v2 Pack manifests")
    else:
        print("Execution v2 Pack digests are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Restore sealed Pack resource bytes in a Docker build copied from Windows.

Only CRLF in declared Python/JSON resources can be changed, and only when the
result matches the existing distribution digest. Manifests, binary templates,
runtime data and published workflow identities are never rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from app.execution.v2.canonical import canonical_sha256, resource_set_digest


def _digest(manifest: dict, resources: dict[str, bytes]) -> str:
    document = dict(manifest)
    document.pop("distribution_digest")
    return canonical_sha256(
        {
            "manifest": document,
            "resources_digest": resource_set_digest(
                (name, resources[name]) for name in manifest["resource_paths"]
            ),
        }
    )


def prepare_execution_packs(root: Path) -> list[str]:
    """Check all built-in packs before writing any approved newline repairs."""
    manifest_paths = sorted((root / "v2/resources/manifests").glob("*.json"))
    if not manifest_paths:
        raise ValueError(f"No built-in execution Pack manifests found in {root}")
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in manifest_paths]
    resources: dict[str, bytes] = {}
    for manifest in manifests:
        for name in manifest["resource_paths"]:
            if (
                "\\" in name
                or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
            ):
                raise ValueError(f"Non-portable Pack resource path: {name!r}")
            if name not in resources:
                resources[name] = (root / name).read_bytes()

    replacements: dict[str, bytes] = {}
    for manifest in manifests:
        expected = manifest["distribution_digest"]
        if _digest(manifest, resources) == expected:
            continue
        normalized = {
            name: (
                data.replace(b"\r\n", b"\n")
                if PurePosixPath(name).suffix in {".py", ".json"}
                else data
            )
            for name, data in resources.items()
        }
        if _digest(manifest, normalized) != expected:
            identity = f"{manifest['pack_id']}@{manifest['pack_version']}"
            raise ValueError(
                f"Pack content differs beyond CRLF line endings: {identity}. "
                "Restore matching release sources; do not regenerate the sealed digest."
            )
        for name in manifest["resource_paths"]:
            if normalized[name] != resources[name]:
                replacements[name] = normalized[name]

    # A resource can be shared by multiple packs. Validate the combined repair
    # against all seals before changing any file, including packs already valid.
    planned = resources | replacements
    for manifest in manifests:
        if _digest(manifest, planned) != manifest["distribution_digest"]:
            raise ValueError(f"Conflicting sealed resource bytes: {manifest['pack_id']}")

    for name, data in sorted(replacements.items()):
        (root / name).write_bytes(data)

    # Check the actual build output as well, so an incomplete write cannot be
    # published as a successful Docker layer.
    actual = {name: (root / name).read_bytes() for name in resources}
    for manifest in manifests:
        if _digest(manifest, actual) != manifest["distribution_digest"]:
            raise ValueError(f"Pack verification failed after preparation: {manifest['pack_id']}")
    return sorted(replacements)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent / "app/execution"
    changed = prepare_execution_packs(root)
    print(f"Execution Packs verified; restored LF in {len(changed)} text resource(s).")

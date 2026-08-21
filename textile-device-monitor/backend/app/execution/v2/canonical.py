"""Canonical JSON, digest, and SemVer helpers for Execution v2 contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import rfc8785
from semantic_version import NpmSpec, Version

SHA256_LENGTH = 64


def canonical_json_bytes(value: Any) -> bytes:
    """Return the RFC 8785 representation used by all v2 identities."""

    return rfc8785.dumps(value)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resource_set_digest(resources: Iterable[tuple[str, bytes]]) -> str:
    """Hash a deterministic path -> content digest inventory.

    Callers pass only manifest-declared resources.  Paths are normalized to
    POSIX separators and duplicate paths are rejected so the result cannot
    depend on filesystem enumeration order.
    """

    inventory: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_path, content in resources:
        path = raw_path.replace("\\", "/")
        if path.startswith("/") or any(
            part in {"", ".", ".."} for part in path.split("/")
        ):
            raise ValueError(f"non-portable resource path: {raw_path!r}")
        if path in seen:
            raise ValueError(f"duplicate resource path: {path}")
        seen.add(path)
        inventory.append({"path": path, "sha256": bytes_sha256(content)})
    inventory.sort(key=lambda item: item["path"])
    return canonical_sha256(inventory)


class VersionedCandidate(Protocol):
    version: str
    ready: bool


@dataclass(frozen=True)
class SemVerCandidate:
    version: str
    ready: bool = True


def parse_semver(value: str) -> Version:
    try:
        return Version(value)
    except ValueError as exc:
        raise ValueError(f"invalid SemVer: {value!r}") from exc


def semver_matches(version: str, version_range: str) -> bool:
    parsed = parse_semver(version)
    try:
        spec = NpmSpec(version_range)
    except ValueError as exc:
        raise ValueError(f"invalid SemVer range: {version_range!r}") from exc
    return spec.match(parsed)


def select_highest_stable(
    candidates: Iterable[VersionedCandidate],
    version_range: str,
) -> VersionedCandidate:
    """Select the highest stable matching version, failing closed on damage.

    Readiness is checked *after* selecting the highest candidate.  This is
    deliberate: silently falling back to an older package would hide a broken
    deployment and make dependency resolution differ between environments.
    """

    matching: list[tuple[Version, VersionedCandidate]] = []
    for candidate in candidates:
        parsed = parse_semver(candidate.version)
        if parsed.prerelease:
            continue
        if semver_matches(candidate.version, version_range):
            matching.append((parsed, candidate))
    if not matching:
        raise LookupError(f"no stable version satisfies {version_range!r}")
    _version, selected = max(matching, key=lambda item: item[0])
    if not selected.ready:
        raise RuntimeError(
            f"highest matching version {selected.version} is installed but not ready"
        )
    return selected

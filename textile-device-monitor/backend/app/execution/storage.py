from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Literal


_ROOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_URL_ESCAPE_PATTERN = re.compile(r"%[0-9a-fA-F]{2}")


class StorageError(RuntimeError):
    """Base class for execution-system storage failures."""


class UnknownRootError(StorageError):
    pass


class UnsafePathError(StorageError):
    pass


class RootPermissionError(StorageError):
    pass


class ArtifactNotFoundError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class StorageRoot:
    root_id: str
    path: Path
    writable: bool = False
    publishable: bool = False

    def __post_init__(self) -> None:
        if not _ROOT_ID_PATTERN.fullmatch(self.root_id):
            raise ValueError("invalid_root_id")

        normalized = Path(self.path).expanduser()
        if not normalized.is_absolute():
            raise ValueError("root_path_must_be_absolute")
        if self.publishable and not self.writable:
            raise ValueError("publishable_root_must_be_writable")

        object.__setattr__(self, "path", normalized)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    root_id: str
    relative_path: str

    def __post_init__(self) -> None:
        if not _ROOT_ID_PATTERN.fullmatch(self.root_id):
            raise ValueError("invalid_root_id")
        object.__setattr__(
            self,
            "relative_path",
            normalize_relative_path(self.relative_path, allow_empty=True),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class ArtifactFingerprint:
    size: int
    modified_ns: int
    sha256: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "size": self.size,
            "modified_ns": self.modified_ns,
            "sha256": self.sha256,
        }


def normalize_relative_path(value: str, *, allow_empty: bool = False) -> str:
    """Validate an API-facing relative path without silently normalizing it."""

    if not isinstance(value, str):
        raise UnsafePathError("relative_path_must_be_string")
    if "\x00" in value:
        raise UnsafePathError("path_contains_null")
    if _URL_ESCAPE_PATTERN.search(value):
        raise UnsafePathError("url_encoded_path_not_allowed")
    if "\\" in value:
        raise UnsafePathError("windows_or_unc_path_not_allowed")
    if value.startswith(("/", "//")):
        raise UnsafePathError("absolute_path_not_allowed")

    windows_path = PureWindowsPath(value)
    if windows_path.drive or windows_path.root or windows_path.is_absolute():
        raise UnsafePathError("windows_or_unc_path_not_allowed")

    if value == "":
        if allow_empty:
            return ""
        raise UnsafePathError("empty_relative_path")

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError("unsafe_path_segment")
    if any(":" in part for part in parts):
        raise UnsafePathError("path_colon_not_allowed")

    return "/".join(parts)


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def fingerprint_file(path: Path, *, chunk_size: int = 1024 * 1024) -> ArtifactFingerprint:
    if chunk_size <= 0:
        raise ValueError("chunk_size_must_be_positive")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)

    stat = path.stat()
    return ArtifactFingerprint(
        size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


class FileGateway:
    """Resolve root aliases while enforcing path and root capabilities."""

    def __init__(self, roots: Iterable[StorageRoot]):
        root_map: dict[str, StorageRoot] = {}
        resolved_paths: dict[str, Path] = {}

        for root in roots:
            if root.root_id in root_map:
                raise ValueError("duplicate_root_id")
            if not root.path.exists():
                raise ValueError(f"root_path_not_found:{root.root_id}")
            if not root.path.is_dir():
                raise ValueError(f"root_path_not_directory:{root.root_id}")

            root_map[root.root_id] = root
            resolved_paths[root.root_id] = root.path.resolve(strict=True)

        if not root_map:
            raise ValueError("at_least_one_storage_root_required")

        self._roots = root_map
        self._resolved_paths = resolved_paths

    @property
    def root_ids(self) -> tuple[str, ...]:
        return tuple(self._roots)

    def get_root(self, root_id: str) -> StorageRoot:
        try:
            return self._roots[root_id]
        except KeyError as exc:
            raise UnknownRootError("unknown_root") from exc

    def root_path(
        self,
        root_id: str,
        *,
        for_write: bool = False,
        for_publish: bool = False,
    ) -> Path:
        root = self.get_root(root_id)
        self._check_access(root, for_write=for_write, for_publish=for_publish)
        return self._resolved_paths[root_id]

    def resolve(
        self,
        ref: ArtifactRef,
        *,
        must_exist: bool = True,
        for_write: bool = False,
        for_publish: bool = False,
        expected_type: Literal["file", "directory"] | None = None,
    ) -> Path:
        root = self.get_root(ref.root_id)
        self._check_access(root, for_write=for_write, for_publish=for_publish)

        relative_path = normalize_relative_path(ref.relative_path, allow_empty=True)
        base = self._resolved_paths[ref.root_id]
        candidate = base if not relative_path else base.joinpath(*relative_path.split("/"))

        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact_not_found") from exc
        except RuntimeError as exc:
            raise UnsafePathError("symlink_resolution_failed") from exc

        if not is_within_root(resolved, base):
            raise UnsafePathError("path_escapes_root")
        if must_exist and not resolved.exists():
            raise ArtifactNotFoundError("artifact_not_found")
        if expected_type == "file" and not resolved.is_file():
            raise ArtifactNotFoundError("artifact_not_file")
        if expected_type == "directory" and not resolved.is_dir():
            raise ArtifactNotFoundError("artifact_not_directory")
        return resolved

    def ensure_directory(
        self,
        ref: ArtifactRef,
        *,
        parents: bool = True,
    ) -> Path:
        path = self.resolve(ref, must_exist=False, for_write=True)
        path.mkdir(parents=parents, exist_ok=True)
        resolved = path.resolve(strict=True)
        base = self.root_path(ref.root_id, for_write=True)
        if not is_within_root(resolved, base):
            raise UnsafePathError("directory_escapes_root")
        return resolved

    def ensure_parent(self, ref: ArtifactRef) -> Path:
        if not ref.relative_path:
            raise UnsafePathError("root_has_no_parent_artifact")
        parent_path = ref.relative_path.rpartition("/")[0]
        if not parent_path:
            return self.root_path(ref.root_id, for_write=True)
        return self.ensure_directory(ArtifactRef(ref.root_id, parent_path))

    def to_ref(self, root_id: str, path: Path) -> ArtifactRef:
        base = self.root_path(root_id)
        try:
            resolved = Path(path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact_not_found") from exc
        if not is_within_root(resolved, base):
            raise UnsafePathError("path_escapes_root")
        relative = resolved.relative_to(base).as_posix()
        return ArtifactRef(root_id=root_id, relative_path=relative)

    def fingerprint(self, ref: ArtifactRef) -> ArtifactFingerprint:
        path = self.resolve(ref, expected_type="file")
        return fingerprint_file(path)

    @staticmethod
    def _check_access(
        root: StorageRoot,
        *,
        for_write: bool,
        for_publish: bool,
    ) -> None:
        if (for_write or for_publish) and not root.writable:
            raise RootPermissionError("root_is_read_only")
        if for_publish and not root.publishable:
            raise RootPermissionError("root_is_not_publishable")


def artifact_ref(root_id: str, relative_path: str) -> ArtifactRef:
    """Small adapter for API layers that receive separate root/path fields."""

    return ArtifactRef(root_id=root_id, relative_path=relative_path)


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())

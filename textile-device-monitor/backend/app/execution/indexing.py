from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from app.execution.storage import ArtifactRef, FileGateway


DEFAULT_DIRECTORY_BLACKLIST = frozenset({".recycle", "旧"})
ELECTRON_MICROSCOPY_SUFFIXES = frozenset({".sif", ".bmp", ".txt"})


def _utc_from_ns(timestamp_ns: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)


def _category_for(relative_path: str, root_id: str) -> str:
    parts = relative_path.split("/")
    return parts[0] if len(parts) > 1 else root_id


def is_blacklisted_directory(
    name: str,
    *,
    blacklist: Iterable[str] = DEFAULT_DIRECTORY_BLACKLIST,
) -> bool:
    normalized = name.strip().casefold()
    return normalized in {item.strip().casefold() for item in blacklist if item.strip()}


def is_office_temporary_file(name: str) -> bool:
    normalized = name.strip().casefold()
    return (
        normalized.startswith("~$")
        or normalized.startswith(".~lock.")
        or (normalized.startswith("~") and normalized.endswith(".tmp"))
        or normalized in {".ds_store", "thumbs.db"}
    )


@dataclass(frozen=True, slots=True)
class IndexedFile:
    ref: ArtifactRef
    name: str
    suffix: str
    size: int
    modified_ns: int
    category: str

    @property
    def modified_at(self) -> datetime:
        return _utc_from_ns(self.modified_ns)

    def as_dict(self) -> dict[str, object]:
        return {
            "root_id": self.ref.root_id,
            "relative_path": self.ref.relative_path,
            "name": self.name,
            "suffix": self.suffix,
            "size": self.size,
            "modified_at": self.modified_at.isoformat(),
            "category": self.category,
        }

    def as_upsert_payload(self) -> dict[str, object]:
        """Stable database adapter payload; the ORM remains outside this module."""

        return {
            "root_id": self.ref.root_id,
            "relative_path": self.ref.relative_path,
            "file_name": self.name,
            "suffix": self.suffix,
            "size_bytes": self.size,
            "modified_ns": self.modified_ns,
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    root_id: str
    added: tuple[IndexedFile, ...]
    updated: tuple[IndexedFile, ...]
    removed: tuple[IndexedFile, ...]
    unchanged_count: int
    total_count: int
    scanned_at: datetime
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "added": [item.as_dict() for item in self.added],
            "updated": [item.as_dict() for item in self.updated],
            "removed": [item.as_dict() for item in self.removed],
            "unchanged_count": self.unchanged_count,
            "total_count": self.total_count,
            "scanned_at": self.scanned_at.isoformat(),
            "errors": list(self.errors),
        }

    def as_persistence_delta(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "upserts": [
                item.as_upsert_payload()
                for item in (*self.added, *self.updated)
            ],
            "removed": [item.ref.as_dict() for item in self.removed],
            "scan_complete": not self.errors,
            "scanned_at": self.scanned_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ElectronMicroscopyGroup:
    root_id: str
    parent_path: str
    base_name: str
    sif: IndexedFile | None = None
    bmp: IndexedFile | None = None
    txt: IndexedFile | None = None
    duplicate_members: tuple[IndexedFile, ...] = ()

    @property
    def complete(self) -> bool:
        return self.sif is not None and self.bmp is not None and self.txt is not None

    @property
    def modified_at(self) -> datetime:
        members = [item for item in (self.sif, self.bmp, self.txt) if item is not None]
        return max((item.modified_at for item in members), default=datetime.min.replace(tzinfo=timezone.utc))

    def as_dict(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "parent_path": self.parent_path,
            "base_name": self.base_name,
            "complete": self.complete,
            "sif": self.sif.as_dict() if self.sif else None,
            "bmp": self.bmp.as_dict() if self.bmp else None,
            "txt": self.txt.as_dict() if self.txt else None,
            "duplicate_members": [item.as_dict() for item in self.duplicate_members],
            "modified_at": self.modified_at.isoformat(),
        }


@dataclass(slots=True)
class _ElectronGroupBuilder:
    root_id: str
    parent_path: str
    base_name: str
    members: dict[str, IndexedFile] = field(default_factory=dict)
    duplicates: list[IndexedFile] = field(default_factory=list)

    def add(self, item: IndexedFile) -> None:
        current = self.members.get(item.suffix)
        if current is None:
            self.members[item.suffix] = item
            return

        if item.modified_ns > current.modified_ns:
            self.duplicates.append(current)
            self.members[item.suffix] = item
        else:
            self.duplicates.append(item)

    def build(self) -> ElectronMicroscopyGroup:
        return ElectronMicroscopyGroup(
            root_id=self.root_id,
            parent_path=self.parent_path,
            base_name=self.base_name,
            sif=self.members.get(".sif"),
            bmp=self.members.get(".bmp"),
            txt=self.members.get(".txt"),
            duplicate_members=tuple(
                sorted(self.duplicates, key=lambda item: item.ref.relative_path.casefold())
            ),
        )


def group_electron_microscopy_files(
    files: Iterable[IndexedFile],
    *,
    require_sif: bool = True,
) -> tuple[ElectronMicroscopyGroup, ...]:
    builders: dict[tuple[str, str, str], _ElectronGroupBuilder] = {}

    for item in files:
        if item.suffix not in ELECTRON_MICROSCOPY_SUFFIXES:
            continue
        relative = Path(item.ref.relative_path)
        parent_path = "" if str(relative.parent) == "." else relative.parent.as_posix()
        key = (item.ref.root_id, parent_path.casefold(), relative.stem.casefold())
        builder = builders.setdefault(
            key,
            _ElectronGroupBuilder(
                root_id=item.ref.root_id,
                parent_path=parent_path,
                base_name=relative.stem,
            ),
        )
        builder.add(item)

    groups = [builder.build() for builder in builders.values()]
    if require_sif:
        groups = [group for group in groups if group.sif is not None]
    groups.sort(
        key=lambda group: (
            -group.modified_at.timestamp(),
            group.parent_path.casefold(),
            group.base_name.casefold(),
        )
    )
    return tuple(groups)


class IncrementalFileIndexer:
    """Thread-safe in-memory index whose scan method is safe for background jobs."""

    def __init__(
        self,
        gateway: FileGateway,
        *,
        directory_blacklist: Iterable[str] = DEFAULT_DIRECTORY_BLACKLIST,
        allowed_suffixes: Iterable[str] | None = None,
    ):
        self._gateway = gateway
        self._directory_blacklist = frozenset(
            item.strip().casefold() for item in directory_blacklist if item.strip()
        )
        self._allowed_suffixes = (
            frozenset(self._normalize_suffix(item) for item in allowed_suffixes)
            if allowed_suffixes is not None
            else None
        )
        self._files: dict[tuple[str, str], IndexedFile] = {}
        self._lock = threading.RLock()

    def scan_incremental(
        self,
        root_id: str,
        *,
        max_depth: int | None = None,
    ) -> ScanResult:
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth_must_be_non_negative")

        root = self._gateway.root_path(root_id)
        discovered: dict[tuple[str, str], IndexedFile] = {}
        errors: list[str] = []

        def on_error(error: OSError) -> None:
            path = getattr(error, "filename", None) or root
            errors.append(f"{type(error).__name__}:{path}")

        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=on_error,
            followlinks=False,
        ):
            current_path = Path(current)
            relative_directory = current_path.relative_to(root)
            depth = 0 if str(relative_directory) == "." else len(relative_directory.parts)

            directory_names[:] = [
                name
                for name in directory_names
                if not self._is_skipped_directory(current_path / name, name)
            ]
            if max_depth is not None and depth >= max_depth:
                directory_names[:] = []

            for name in file_names:
                if is_office_temporary_file(name):
                    continue
                suffix = Path(name).suffix.casefold()
                if self._allowed_suffixes is not None and suffix not in self._allowed_suffixes:
                    continue

                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    stat = path.stat(follow_symlinks=False)
                except OSError as exc:
                    on_error(exc)
                    continue
                if not path.is_file():
                    continue

                relative_path = path.relative_to(root).as_posix()
                item = IndexedFile(
                    ref=ArtifactRef(root_id, relative_path),
                    name=name,
                    suffix=suffix,
                    size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    category=_category_for(relative_path, root_id),
                )
                discovered[(root_id, relative_path)] = item

        with self._lock:
            previous = {
                key: item
                for key, item in self._files.items()
                if key[0] == root_id
            }
            added = tuple(
                item
                for key, item in discovered.items()
                if key not in previous
            )
            updated = tuple(
                item
                for key, item in discovered.items()
                if key in previous and item != previous[key]
            )
            unchanged_count = sum(
                1
                for key, item in discovered.items()
                if key in previous and item == previous[key]
            )

            # A partial scan must not erase entries hidden by a transient mount or
            # permissions failure. A clean scan can safely remove missing files.
            if errors:
                removed: tuple[IndexedFile, ...] = ()
            else:
                removed = tuple(
                    item
                    for key, item in previous.items()
                    if key not in discovered
                )
                for key in previous:
                    if key not in discovered:
                        self._files.pop(key, None)

            self._files.update(discovered)
            total_count = sum(1 for key in self._files if key[0] == root_id)

        sort_key = lambda item: item.ref.relative_path.casefold()
        return ScanResult(
            root_id=root_id,
            added=tuple(sorted(added, key=sort_key)),
            updated=tuple(sorted(updated, key=sort_key)),
            removed=tuple(sorted(removed, key=sort_key)),
            unchanged_count=unchanged_count,
            total_count=total_count,
            scanned_at=datetime.now(timezone.utc),
            errors=tuple(errors),
        )

    def snapshot(self, *, root_ids: Iterable[str] | None = None) -> tuple[IndexedFile, ...]:
        selected_roots = set(root_ids) if root_ids is not None else None
        with self._lock:
            items = [
                item
                for (root_id, _), item in self._files.items()
                if selected_roots is None or root_id in selected_roots
            ]
        items.sort(key=lambda item: item.ref.relative_path.casefold())
        return tuple(items)

    def persistence_snapshot(
        self,
        *,
        root_ids: Iterable[str] | None = None,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            item.as_upsert_payload()
            for item in self.snapshot(root_ids=root_ids)
        )

    def find_candidates(
        self,
        inspection_number: str,
        *,
        root_ids: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        recent_days: int | None = 7,
        limit: int | None = 6,
        now: datetime | None = None,
    ) -> tuple[IndexedFile, ...]:
        query = inspection_number.strip().casefold()
        if not query:
            return ()
        if recent_days is not None and recent_days < 0:
            raise ValueError("recent_days_must_be_non_negative")
        if limit is not None and limit <= 0:
            raise ValueError("limit_must_be_positive")

        selected_categories = (
            {item.strip().casefold() for item in categories if item.strip()}
            if categories is not None
            else None
        )
        candidates = [
            item
            for item in self.snapshot(root_ids=root_ids)
            if query in item.ref.relative_path.casefold()
            and (
                selected_categories is None
                or item.category.casefold() in selected_categories
            )
        ]

        if recent_days is not None:
            reference_time = now or datetime.now(timezone.utc)
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            cutoff = reference_time.astimezone(timezone.utc) - timedelta(days=recent_days)
            candidates = [item for item in candidates if item.modified_at >= cutoff]

        def match_rank(item: IndexedFile) -> tuple[int, int, str]:
            stem = Path(item.name).stem.casefold()
            if stem == query:
                quality = 0
            elif stem.startswith(query):
                quality = 1
            elif query in stem:
                quality = 2
            else:
                quality = 3
            return quality, -item.modified_ns, item.ref.relative_path.casefold()

        candidates.sort(key=match_rank)
        if limit is not None:
            candidates = candidates[:limit]
        return tuple(candidates)

    def electron_groups(
        self,
        *,
        root_ids: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        require_sif: bool = True,
    ) -> tuple[ElectronMicroscopyGroup, ...]:
        selected_categories = (
            {item.strip().casefold() for item in categories if item.strip()}
            if categories is not None
            else None
        )
        files = (
            item
            for item in self.snapshot(root_ids=root_ids)
            if selected_categories is None
            or item.category.casefold() in selected_categories
        )
        return group_electron_microscopy_files(files, require_sif=require_sif)

    def _is_skipped_directory(self, path: Path, name: str) -> bool:
        return (
            name.strip().casefold() in self._directory_blacklist
            or path.is_symlink()
        )

    @staticmethod
    def _normalize_suffix(value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("empty_suffix")
        return normalized if normalized.startswith(".") else f".{normalized}"


def scan_roots_incrementally(
    indexer: IncrementalFileIndexer,
    root_ids: Sequence[str],
    *,
    max_depth: int | None = None,
) -> tuple[ScanResult, ...]:
    """One-shot entry point intended for a scheduler or worker task."""

    return tuple(
        indexer.scan_incremental(root_id, max_depth=max_depth)
        for root_id in root_ids
    )

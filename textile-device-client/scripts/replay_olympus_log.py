"""Replay archived Olympus logs into a live Olympus.log file."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable, Iterator, Sequence


TIMESTAMP_PATTERN = re.compile(
    rb"^(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})(?:\.(\d+))?"
)
CLI_TIMESTAMP_FORMATS = (
    "%m/%d/%Y %H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


@dataclass(frozen=True)
class ReplayStats:
    lines_written: int
    bytes_written: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None


def parse_log_timestamp(line: bytes) -> datetime | None:
    match = TIMESTAMP_PATTERN.match(line)
    if not match:
        return None
    timestamp = match.group(1).decode("ascii")
    fraction = (match.group(2) or b"").decode("ascii")
    fraction = (fraction + "000000")[:6]
    try:
        return datetime.strptime(
            f"{timestamp}.{fraction}",
            "%m/%d/%Y %H:%M:%S.%f",
        )
    except ValueError:
        return None


def parse_cli_timestamp(value: str) -> datetime:
    for timestamp_format in CLI_TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, timestamp_format)
        except ValueError:
            continue
    formats = ", ".join(CLI_TIMESTAMP_FORMATS)
    raise argparse.ArgumentTypeError(f"invalid timestamp {value!r}; expected {formats}")


def first_log_timestamp(path: Path) -> datetime | None:
    with path.open("rb") as source:
        for line in source:
            timestamp = parse_log_timestamp(line)
            if timestamp is not None:
                return timestamp
    return None


def expand_sources(source_paths: Sequence[Path], output_path: Path) -> list[Path]:
    output_resolved = output_path.resolve()
    expanded: list[Path] = []
    for source_path in source_paths:
        if source_path.is_dir():
            candidates = list(source_path.glob("*.log"))
            candidates.sort(
                key=lambda path: (
                    first_log_timestamp(path) or datetime.max,
                    path.name.lower(),
                )
            )
            expanded.extend(candidates)
        else:
            expanded.append(source_path)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in expanded:
        resolved = path.resolve()
        if resolved == output_resolved:
            continue
        if resolved in seen:
            continue
        if not path.is_file():
            raise FileNotFoundError(f"source log does not exist: {path}")
        seen.add(resolved)
        unique.append(path)
    if not unique:
        raise ValueError("no source log files found")
    return unique


def iter_log_lines(paths: Iterable[Path]) -> Iterator[bytes]:
    for path in paths:
        with path.open("rb") as source:
            yield from source


def replay_logs(
    sources: Sequence[Path],
    output: Path,
    *,
    speed: float = 1.0,
    truncate: bool = False,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_progress: Callable[[ReplayStats], None] | None = None,
) -> ReplayStats:
    if speed <= 0:
        raise ValueError("speed must be greater than zero")
    if start_at and end_at and start_at > end_at:
        raise ValueError("start_at must not be later than end_at")

    output.parent.mkdir(parents=True, exist_ok=True)
    source_paths = expand_sources(sources, output)
    mode = "wb" if truncate else "ab"
    replay_started = monotonic()
    scheduled_elapsed = 0.0
    previous_timestamp: datetime | None = None
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    lines_written = 0
    bytes_written = 0
    selection_started = start_at is None

    with output.open(mode) as destination:
        for line in iter_log_lines(source_paths):
            timestamp = parse_log_timestamp(line)
            if timestamp is not None:
                if start_at is not None and timestamp < start_at:
                    continue
                if end_at is not None and timestamp > end_at:
                    break
                selection_started = True
                if previous_timestamp is not None:
                    delta_seconds = max(
                        0.0,
                        (timestamp - previous_timestamp).total_seconds(),
                    )
                    scheduled_elapsed += delta_seconds / speed
                previous_timestamp = timestamp
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp
            elif not selection_started:
                continue

            wait_seconds = scheduled_elapsed - (monotonic() - replay_started)
            if wait_seconds > 0:
                sleep(wait_seconds)

            destination.write(line)
            destination.flush()
            lines_written += 1
            bytes_written += len(line)
            if on_progress and lines_written % 10000 == 0:
                on_progress(
                    ReplayStats(
                        lines_written,
                        bytes_written,
                        first_timestamp,
                        last_timestamp,
                    )
                )

    return ReplayStats(
        lines_written,
        bytes_written,
        first_timestamp,
        last_timestamp,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append Olympus log lines according to their original timestamp intervals. "
            "Directories are expanded to chronologically sorted *.log files."
        )
    )
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="playback multiplier; 1 keeps real time, 30 is thirty times faster",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="clear the destination before replaying",
    )
    parser.add_argument(
        "--start-at",
        type=parse_cli_timestamp,
        help="first source timestamp to include",
    )
    parser.add_argument(
        "--end-at",
        type=parse_cli_timestamp,
        help="last source timestamp to include",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def print_progress(stats: ReplayStats) -> None:
        print(
            f"replayed {stats.lines_written} lines, "
            f"{stats.bytes_written / 1024 / 1024:.1f} MiB, "
            f"source time {stats.last_timestamp}",
            flush=True,
        )

    try:
        stats = replay_logs(
            args.sources,
            args.output,
            speed=args.speed,
            truncate=args.truncate,
            start_at=args.start_at,
            end_at=args.end_at,
            on_progress=print_progress,
        )
    except (OSError, ValueError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("replay stopped", file=sys.stderr)
        return 130

    print_progress(stats)
    print(f"output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

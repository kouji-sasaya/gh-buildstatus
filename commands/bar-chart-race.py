#!/usr/bin/env python3
"""Convert Yocto buildstats raw task records to bar-chart-race JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


def read_value(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, re.MULTILINE)
    return float(match.group(1)) if match else None


def input_paths(input_path: Path) -> tuple[Path, Path, Path]:
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_path}")

    metadata_path = input_path / "build_stats"
    raw_path = input_path / "raw"
    if metadata_path.is_file():
        return input_path, metadata_path, raw_path if raw_path.is_dir() else input_path

    if input_path.name == "raw" and (input_path.parent / "build_stats").is_file():
        return input_path.parent, input_path.parent / "build_stats", input_path

    raise FileNotFoundError(
        f"Could not find build_stats for {input_path}. "
        "Pass the buildstats directory or its raw/ directory."
    )


def convert(input_path: Path) -> dict[str, object]:
    build_dir, metadata_path, raw_path = input_paths(input_path)
    metadata_text = metadata_path.read_text(encoding="utf-8", errors="replace")
    build_started = read_value(r"^Build Started:\s*([0-9.]+)\s*$", metadata_text)
    build_duration = read_value(
        r"^Elapsed time:\s*([0-9.]+)\s*seconds\s*$", metadata_text
    )
    if build_started is None:
        raise ValueError(f"Could not find 'Build Started' in {metadata_path}")

    events: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    skipped = 0
    for recipe_dir in sorted(path for path in raw_path.iterdir() if path.is_dir()):
        recipe = re.sub(r"-r\d+$", "", recipe_dir.name)
        for task_path in sorted(recipe_dir.iterdir()):
            if not task_path.is_file() or not task_path.name.startswith("do_"):
                continue

            text = task_path.read_text(encoding="utf-8", errors="replace")
            duration = read_value(
                r"^Elapsed time:\s*([0-9.]+)\s*seconds?\s*$", text
            )
            started = read_value(r"^Started:\s*([0-9.]+)\s*$", text)
            ended = read_value(r"^Ended:\s*([0-9.]+)\s*$", text)
            status_match = re.search(r"^Status:\s*(\S+)", text, re.MULTILINE)
            if duration is None or started is None or ended is None:
                skipped += 1
                continue
            if ended < started:
                raise ValueError(f"Task end precedes start in {task_path}")

            if status_match:
                status_counts[status_match.group(1)] += 1
            events.append(
                {
                    "time": max(0.0, ended - build_started),
                    "startTime": max(0.0, started - build_started),
                    "recipe": recipe,
                    "recipeId": recipe_dir.name,
                    "task": task_path.name,
                    "duration": duration,
                }
            )

    if not events:
        raise ValueError(f"No completed task records found in {raw_path}")

    events.sort(key=lambda event: (event["time"], event["recipeId"], event["task"]))
    last_event_time = max(float(event["time"]) for event in events)
    if build_duration is None:
        build_duration = last_event_time
    build_duration = max(build_duration, last_event_time)

    return {
        "metadata": {
            "build": build_dir.name,
            "duration": build_duration,
            "taskCount": len(events),
            "skippedCount": skipped,
            "statusCounts": dict(status_counts),
        },
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Yocto buildstats raw task records to bar-chart-race JSON.",
        epilog=(
            "Input may be the buildstats directory (containing build_stats and raw/) "
            "or its raw/ directory."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Yocto buildstats directory or its raw/ directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for the bar-chart-race JSON file",
    )
    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()
    try:
        data = convert(args.input.resolve())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")

    print(
        f"Created {args.output} from {data['metadata']['taskCount']} task records "
        f"({data['metadata']['skippedCount']} records without timing data skipped)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

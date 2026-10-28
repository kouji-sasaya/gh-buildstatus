#!/usr/bin/env python3
"""Convert Yocto buildstats host monitor logs to per-metric time-series JSON.

Each metric is written to its own file (cpu.json, memory.json, disk.json,
net.json) so that charts can load only what they need. Times are seconds
relative to the build start, matching bar-chart-race.py.

Column meanings of reduced_proc_* logs are inferred from the data; they are
not documented by buildstats.bbclass.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

Record = tuple[int, list[str]]


def read_records(path: Path) -> list[Record]:
    """Parse blocks of '<epoch>' followed by value lines."""
    records: list[Record] = []
    if not path.is_file():
        return records
    current: Record | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"\d+", line):
            current = (int(line), [])
            records.append(current)
        elif current is not None:
            current[1].append(line)
    return records


def numbers(text: str) -> list[float]:
    return [float(v) for v in text.split()]


class Series:
    """Bucketed time series; each column is aggregated per bucket."""

    def __init__(self, names: list[str], aggs: list[str], t0: float, dt: int):
        self.names, self.aggs, self.t0, self.dt = names, aggs, t0, dt
        self.buckets: dict[int, list[list[float]]] = {}

    def add(self, epoch: float, values: list[float]) -> None:
        if epoch < self.t0 or len(values) != len(self.names):
            return
        index = int((epoch - self.t0) // self.dt)
        cols = self.buckets.setdefault(index, [[] for _ in self.names])
        for col, value in zip(cols, values):
            col.append(value)

    def to_json(self, digits: int = 3) -> dict[str, object]:
        if not self.buckets:
            return {}
        length = max(self.buckets) + 1
        series: dict[str, list[float | None]] = {n: [None] * length for n in self.names}
        for index, cols in self.buckets.items():
            for name, agg, col in zip(self.names, self.aggs, cols):
                if agg == "mean":
                    value = sum(col) / len(col)
                elif agg == "min":
                    value = min(col)
                elif agg == "max":
                    value = max(col)
                else:  # last
                    value = col[-1]
                series[name][index] = round(value, digits)
        return {"t0": 0, "dt": self.dt, "length": length, "series": series}


def convert_cpu(build: Path, t0: float, dt: int) -> dict[str, object]:
    out: dict[str, object] = {}
    # reduced_proc_stat.log: three CPU time fractions (user, system, iowait)
    s = Series(["user", "system", "iowait"], ["mean"] * 3, t0, dt)
    for epoch, lines in read_records(build / "reduced_proc_stat.log"):
        if lines and len(numbers(lines[0])) == 3:
            s.add(epoch, [v * 100 for v in numbers(lines[0])])
    out["usage"] = s.to_json()
    out["pressure"] = convert_pressure(build / "reduced_proc_pressure" / "cpu.log", t0, dt)
    return {k: v for k, v in out.items() if v}


def convert_pressure(path: Path, t0: float, dt: int) -> dict[str, object]:
    # avg10, avg60, avg300 (percent) and cumulative stall time (us)
    s = Series(["avg10", "avg60", "avg300", "total_us"],
               ["mean", "mean", "mean", "last"], t0, dt)
    for epoch, lines in read_records(path):
        if lines and len(numbers(lines[0])) == 4:
            s.add(epoch, numbers(lines[0]))
    return s.to_json()


def convert_memory(build: Path, t0: float, dt: int) -> dict[str, object]:
    out: dict[str, object] = {}
    names = ["total_kb", "free_kb", "buffers_kb", "cached_kb",
             "swap_total_kb", "swap_free_kb"]
    s = Series(names, ["mean"] * 6, t0, dt)
    for epoch, lines in read_records(build / "reduced_proc_meminfo.log"):
        if lines and len(numbers(lines[0])) == 6:
            s.add(epoch, numbers(lines[0]))
    out["meminfo"] = s.to_json()
    out["pressure"] = convert_pressure(build / "reduced_proc_pressure" / "memory.log", t0, dt)
    return {k: v for k, v in out.items() if v}


def convert_disk(build: Path, t0: float, dt: int) -> dict[str, object]:
    out: dict[str, object] = {}
    free: dict[str, Series] = {}
    for epoch, lines in read_records(build / "monitor_disk.log"):
        for line in lines:
            device, _, value = line.rpartition(":")
            if not device or not value.strip().isdigit():
                continue
            free.setdefault(device, Series(["free_bytes"], ["min"], t0, dt)).add(
                epoch, [float(value)]
            )
    out["free"] = {dev: s.to_json(0) for dev, s in free.items() if s.buckets}

    # reduced_proc_diskstats.log: three values of unconfirmed meaning
    s = Series(["v0", "v1", "v2"], ["mean"] * 3, t0, dt)
    for epoch, lines in read_records(build / "reduced_proc_diskstats.log"):
        if lines and len(numbers(lines[0])) == 3:
            s.add(epoch, numbers(lines[0]))
    out["diskstats"] = s.to_json()
    out["pressure"] = convert_pressure(build / "reduced_proc_pressure" / "io.log", t0, dt)
    return {k: v for k, v in out.items() if v}


def convert_net(build: Path, t0: float, dt: int) -> dict[str, object]:
    ifaces: dict[str, Series] = {}
    for epoch, lines in read_records(build / "reduced_proc_net.log"):
        for line in lines:
            name, _, rest = line.partition(":")
            values = numbers(rest) if rest.strip() else []
            if name == "lo" or len(values) != 4:
                continue
            ifaces.setdefault(name, Series(["v0", "v1", "v2", "v3"], ["last"] * 4, t0, dt)).add(
                epoch, values
            )
    return {name: s.to_json(0) for name, s in ifaces.items() if s.buckets}


CONVERTERS = {
    "cpu": convert_cpu,
    "memory": convert_memory,
    "disk": convert_disk,
    "net": convert_net,
}


def build_start(build: Path) -> float:
    path = build / "build_stats"
    if path.is_file():
        match = re.search(r"^Build Started:\s*([0-9.]+)", path.read_text(errors="replace"), re.M)
        if match:
            return float(match.group(1))
    for name in ("reduced_proc_stat.log", "monitor_disk.log"):
        records = read_records(build / name)
        if records:
            return float(records[0][0])
    raise ValueError(f"Cannot determine build start time for {build}")


def write_json(path: Path, data: dict[str, object], compress: bool) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if compress:
        path = path.with_suffix(".json.gz")
        path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    else:
        path.write_text(text + "\n", encoding="utf-8")


def write_index(output: Path, build: str, info: dict[str, object]) -> None:
    (output / "index.json").write_text(
        json.dumps({"build": build, **info}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Yocto buildstats host monitor logs to time-series JSON."
    )
    parser.add_argument("--input", type=Path, required=True,
                        help="buildstats build directory (e.g. tmp/buildstats/20261027134621)")
    parser.add_argument("--output", type=Path, required=True,
                        help="output directory for cpu.json, memory.json, ... and index.json")
    parser.add_argument("--only", default=",".join(CONVERTERS),
                        help="comma-separated metrics to write: " + ",".join(CONVERTERS))
    parser.add_argument("--interval", type=int, default=10,
                        help="bucket size in seconds (default: 10)")
    parser.add_argument("--gzip", action="store_true", help="write .json.gz files")
    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    args = parser.parse_args()
    build = args.input.resolve()
    metrics = [m.strip() for m in args.only.split(",") if m.strip()]
    unknown = [m for m in metrics if m not in CONVERTERS]
    if unknown or args.interval < 1 or not build.is_dir():
        parser.exit(2, f"{parser.prog}: error: invalid input, --only or --interval\n")

    try:
        t0 = build_start(build)
    except ValueError as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")

    out_dir = args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for metric in metrics:
        data = CONVERTERS[metric](build, t0, args.interval)
        if not data:
            print(f"skip {metric}: no data")
            continue
        data = {"build": build.name, "buildStart": t0, "interval": args.interval, **data}
        path = out_dir / f"{metric}.json"
        write_json(path, data, args.gzip)
        final = path.with_suffix(".json.gz") if args.gzip else path
        files[metric] = final.name
        print(f"Created {final} ({final.stat().st_size} bytes)")

    write_index(out_dir, build.name, {"buildStart": t0, "files": files})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

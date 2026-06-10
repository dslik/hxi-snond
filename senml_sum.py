#!/usr/bin/env python3
"""
senml_sum.py — Sum N directories of SenML time-series files.

For every N-tuple of series (one from each input directory) whose time ranges
share a common overlap, the values are summed at the union of all sample times
within that overlap (using linear interpolation where a series lacks a sample)
and written as one new SenML file to the output directory.

The output file's base name (bn) is derived from the output directory's name,
which must be a UUID.  The output filename follows the same convention as the
inputs:  <output-dir-uuid>-<first-sample-timestamp>.json

If two distinct output series happen to share the same first-sample timestamp
a collision-index suffix is appended to keep filenames unique.

Usage
-----
    python senml_sum.py <input_dir1> <input_dir2> [<input_dirN> ...] <output_dir>

At least two input directories are required.
"""

from __future__ import annotations
import bisect
import itertools
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── UUID validation ───────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def extract_dir_uuid(directory: Path) -> str:
    """
    Return the UUID that forms *directory*'s name, or raise ValueError.
    The directory name itself is expected to be a bare UUID string.
    """
    name = directory.name
    if not _UUID_RE.match(name):
        raise ValueError(f"Output directory name is not a UUID: {name!r}")
    return name


# ── SenML I/O ─────────────────────────────────────────────────────────────────

def load_senml(path: Path) -> tuple[str | None, list[tuple[float, float]]]:
    """
    Parse a SenML JSON pack file.

    Returns
    -------
    base_name : str | None
        The ``bn`` field from the first record, or None if absent.
    samples : list of (epoch_seconds, value) tuples
        Sorted chronologically.

    Raises
    ------
    ValueError
        If any timestamp appears more than once within the file.
    """
    with open(path) as f:
        records = json.load(f)

    base_name: str | None = None
    samples: list[tuple[float, float]] = []

    for rec in records:
        if "bn" in rec:
            base_name = rec["bn"]
        if "t" not in rec or "v" not in rec:
            continue
        epoch = datetime.fromisoformat(
            rec["t"].replace("Z", "+00:00")
        ).timestamp()
        samples.append((epoch, float(rec["v"])))

    samples.sort()

    # Reject duplicate timestamps — ambiguous and would silently corrupt lerp_at
    for i in range(1, len(samples)):
        if samples[i][0] == samples[i - 1][0]:
            t_str = (
                datetime.fromtimestamp(samples[i][0], tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            raise ValueError(f"Duplicate timestamp {t_str}")

    return base_name, samples


def load_directory(
    directory: Path,
) -> list[tuple[Path, str | None, list[tuple[float, float]]]]:
    """
    Load every *.json SenML file from *directory*.

    Returns a list of (file_path, base_name, samples) — one entry per file
    that parses successfully and contains at least one valid sample.
    Files that fail to parse are skipped with a warning on stderr.

    Raises
    ------
    FileNotFoundError
        If the directory contains no *.json files at all.
    """
    json_files = sorted(directory.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No *.json files found in {directory}")

    entries: list[tuple[Path, str | None, list[tuple[float, float]]]] = []
    for path in json_files:
        try:
            bn, samples = load_senml(path)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  Warning: skipping {path.name}: {exc}", file=sys.stderr)
            continue
        if not samples:
            print(f"  Warning: no valid samples in {path.name}, skipping.",
                  file=sys.stderr)
            continue
        entries.append((path, bn, samples))

    return entries


def save_senml(
    path: Path,
    base_name: str,
    samples: list[tuple[float, float]],
) -> None:
    """
    Write a SenML JSON pack to *path*.

    The ``bn`` field appears only in the first record.
    Values are rounded to 9 decimal places to suppress floating-point noise.
    """
    records: list[dict] = []
    for i, (t, v) in enumerate(samples):
        t_str = (
            datetime.fromtimestamp(t, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        rec: dict = {"v": round(v, 9), "t": t_str}
        if i == 0:
            rec = {"bn": base_name, **rec}
        records.append(rec)

    with open(path, "w") as f:
        json.dump(records, f, indent=2)


# ── Interpolation ─────────────────────────────────────────────────────────────

def lerp_at(t: float, samples: list[tuple[float, float]]) -> float | None:
    """
    Linearly interpolate *samples* at time *t*.

    Returns the exact sample value when *t* coincides with a sample time.
    Returns None when *t* is strictly outside [samples[0].t, samples[-1].t]
    — extrapolation is never performed.
    """
    times = [s[0] for s in samples]

    if t < times[0] or t > times[-1]:
        return None

    idx = bisect.bisect_left(times, t)

    if times[idx] == t:                   # exact hit — no interpolation needed
        return samples[idx][1]

    t0, v0 = samples[idx - 1]            # left bracket
    t1, v1 = samples[idx]                # right bracket
    return v0 + (t - t0) / (t1 - t0) * (v1 - v0)


# ── Core algorithm ────────────────────────────────────────────────────────────

def sum_n_series(
    all_samples: list[list[tuple[float, float]]],
) -> list[tuple[float, float]]:
    """
    Sum N time series over their common overlapping time range.

    The output is evaluated at every sample time that appears in *any* of the
    N series within [t_lo, t_hi], where t_lo = max of all start times and
    t_hi = min of all end times.  Where a series has no sample at a given
    time its value is linearly interpolated from its two neighbours.

    Parameters
    ----------
    all_samples : list of N sorted [(epoch_s, value)] lists

    Returns
    -------
    Sorted list of (epoch_s, summed_value), or [] if the N series share no
    common time range (t_lo >= t_hi).
    """
    t_lo = max(s[0][0]  for s in all_samples)
    t_hi = min(s[-1][0] for s in all_samples)

    if t_lo >= t_hi:
        return []

    # Union of all series' sample times clipped to the overlap window
    times_in_overlap = sorted(
        set().union(*(
            {t for t, _ in s if t_lo <= t <= t_hi}
            for s in all_samples
        ))
    )

    result: list[tuple[float, float]] = []
    for t in times_in_overlap:
        values = [lerp_at(t, s) for s in all_samples]
        if all(v is not None for v in values):
            result.append((t, sum(values)))  # type: ignore[arg-type]

    return result


# ── Output filename ───────────────────────────────────────────────────────────

def make_output_path(
    out_dir: Path,
    out_uuid: str,
    first_epoch: float,
    collision_index: int = 0,
) -> Path:
    """
    Build an output file path inside *out_dir*.

    Follows the same ``{uuid}-{timestamp}.json`` naming convention as the
    inputs.  *collision_index* > 0 appends a ``-N`` suffix so that two output
    series which happen to share the same first-sample timestamp remain
    distinct on disk.
    """
    dt  = datetime.fromtimestamp(first_epoch, tz=timezone.utc)
    ts  = dt.strftime("%Y-%m-%dT%H-%M-%S_") + f"{dt.microsecond:06d}Z"
    suffix = f"-{collision_index}" if collision_index else ""
    return out_dir / f"{out_uuid}-{ts}{suffix}.json"


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(
            "Usage: senml_sum.py <input_dir1> <input_dir2> [<input_dirN> ...]"
            " <output_dir>\n\n"
            "For every N-tuple of series (one from each input directory) that\n"
            "share a common time range, sums them and writes one output SenML\n"
            "file to <output_dir>.  N-tuples with no overlap produce no output.\n\n"
            "<output_dir> name must be a UUID — it is used as the bn identifier\n"
            "for all output files."
        )

    *input_paths, output_path = [Path(p) for p in sys.argv[1:]]

    if len(input_paths) < 2:
        sys.exit("At least two input directories are required.")

    for d in input_paths:
        if not d.is_dir():
            sys.exit(f"Not a directory: {d}")

    out_dir = Path(output_path)
    if not out_dir.is_dir():
        sys.exit(f"Not a directory: {out_dir}")

    try:
        out_uuid = extract_dir_uuid(out_dir)
    except ValueError as exc:
        sys.exit(str(exc))

    out_bn = f"urn:uuid:{out_uuid}"

    # ── Load all series from every input directory ────────────────────────────
    print(f"Loading {len(input_paths)} input director{'ies' if len(input_paths) != 1 else 'y'}:")
    dir_series: list[list[tuple[Path, str | None, list[tuple[float, float]]]]] = []
    for d in input_paths:
        try:
            entries = load_directory(d)
        except FileNotFoundError as exc:
            sys.exit(str(exc))
        print(f"  {len(entries):>3} series  ← {d}")
        dir_series.append(entries)

    total_combos = 1
    for entries in dir_series:
        total_combos *= len(entries)
    print(f"\nChecking {total_combos} combination(s) "
          f"({' × '.join(str(len(e)) for e in dir_series)}):")

    # ── Evaluate every N-tuple (one series per directory) ────────────────────
    # used_timestamps tracks how many outputs share a given first-sample epoch
    # so we can append a collision-index suffix when needed.
    used_timestamps: dict[float, int] = {}
    n_output = 0

    fmt = lambda ep: (
        datetime.fromtimestamp(ep, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )

    for combo in itertools.product(*dir_series):
        # combo: one (path, bn, samples) tuple per input directory
        source_paths   = [c[0] for c in combo]
        source_samples = [c[2] for c in combo]

        summed = sum_n_series(source_samples)
        if not summed:
            continue                        # no overlap — skip silently

        first_epoch   = summed[0][0]
        collision_idx = used_timestamps.get(first_epoch, 0)
        used_timestamps[first_epoch] = collision_idx + 1

        out_file = make_output_path(out_dir, out_uuid, first_epoch, collision_idx)
        save_senml(out_file, out_bn, summed)
        n_output += 1

        print(
            f"  [{n_output:>4}] {' + '.join(p.name for p in source_paths)}\n"
            f"         → {out_file.name}"
            f"  ({len(summed)} samples,"
            f" {fmt(summed[0][0])} → {fmt(summed[-1][0])})"
        )

    print(
        f"\n{n_output} output file(s) written to {out_dir}."
        if n_output
        else "\nNo overlapping combinations found — nothing written."
    )


if __name__ == "__main__":
    main()

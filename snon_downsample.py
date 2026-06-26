#!/usr/bin/env python3
"""
snon_downsample.py — Resample a SNON series to per-second value fragments.

Usage:
    python snon_downsample.py --snon-path PATH --sensor UUID [--series UUID]

Arguments:
    --snon-path PATH    Path to the SNON root directory (e.g. ./snon/)
    --sensor UUID       UUID of the sensor to process
    --series UUID       UUID of the series to resample.  Required when the
                        sensor has more than one associated series; omit when
                        there is exactly one.

What the tool does:
    1. Lists .json symlinks in sensors/<sensor_uuid>/series/ to find the
       series (or series) associated with the sensor.
    2. If exactly one series exists, selects it automatically.
       If more than one exists, --series must be supplied.
    3. Reads every value file from series/<series_uuid>/values/, collecting
       all (timestamp, value) pairs across files.
    4. Resamples to 1-second intervals over the output window:
           start — first whole-second boundary strictly after the first sample
           end   — last  whole-second boundary strictly before the last sample
    5. For each interval computes the time-weighted (trapezoidal) mean,
       minimum, and maximum.
    6. Writes the result as a new immutable value fragment under
       sensors/<sensor_uuid>/values/<timestamp>.json.

Input value fragment format (produced by hxi-snond):
    [{ "eID": "urn:uuid:<series_uuid>",
       "v":   [<number>, ...],
       "vT":  ["<ISO-8601-UTC>", ...] }]

Output value fragment format:
    [{ "eID":  "urn:uuid:<sensor_uuid>",
       "v":    ["<mean_0>",  ...],
       "vMin": ["<min_0>",   ...],
       "vMax": ["<max_0>",   ...],
       "vT":   ["<YYYY-MM-DDTHH:MM:SSZ/PT1S>", ...] }]

    Values are JSON strings per the SNON 4 specification.
    Every vT entry carries the absolute start timestamp of the interval
    followed by /PT1S.

    The output filename follows the Section 8.6 naming convention:
        <YYYY>-<MM>-<DD>T<HH>-<MM>-<SS>Z.json
    (the ISO 8601 UTC timestamp of the first output interval, with colons
    replaced by hyphens).  The file is created with exclusive open so that
    an existing file is never overwritten (Section 8.6: value files are
    immutable once written).
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Tuple

# ---------------------------------------------------------------------------
# UUID validation
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(s: str) -> bool:
    """Return True if *s* is a canonically-formatted RFC 4122 UUID."""
    return bool(_UUID_RE.match(s))


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_iso8601(ts: str) -> float:
    """ISO 8601 UTC string → Unix epoch (float seconds)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _epoch_to_iso8601(epoch: float) -> str:
    """Unix epoch → ISO 8601 UTC string (whole-second precision, no sub-seconds)."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_filename_ts(epoch: float) -> str:
    """
    Unix epoch → filesystem-safe ISO 8601 timestamp for value file naming.

    Colons are replaced by hyphens per Section 8.6.  The resampler always
    starts on a whole-second boundary, so no sub-second component is needed.

    Example: 1750000000.0 → "2025-06-15T10-40-00Z"
    """
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


# ---------------------------------------------------------------------------
# SNON filesystem navigation
# ---------------------------------------------------------------------------

def discover_series(snon_path: str, sensor_uuid: str) -> List[str]:
    """
    Return the list of series UUIDs linked under sensors/<sensor_uuid>/series/.

    Inspects every entry in that directory whose name ends in '.json'.  The
    UUID is extracted by stripping the extension; entries whose stem is not
    a valid RFC 4122 UUID are silently skipped (they are directory symlinks
    or unrelated files).

    Raises FileNotFoundError if the series directory itself is absent.
    """
    series_dir = os.path.join(snon_path, "sensors", sensor_uuid, "series")
    if not os.path.isdir(series_dir):
        raise FileNotFoundError(
            f"Sensor series directory not found: {series_dir}\n"
            f"  Check that '{sensor_uuid}' is a valid sensor UUID in this SNON tree."
        )

    uuids: List[str] = []
    for entry in sorted(os.listdir(series_dir)):
        if not entry.endswith(".json"):
            continue
        stem = entry[: -len(".json")]
        if _is_valid_uuid(stem):
            uuids.append(stem)

    return uuids


def read_snon_series(
    snon_path: str,
    series_uuid: str,
) -> List[Tuple[float, float]]:
    """
    Read all value files from series/<series_uuid>/values/ and return a
    sorted list of (unix_epoch, value) pairs.

    Each value file is expected to be a JSON array (SNON pack) whose first
    element contains parallel 'v' (numeric values) and 't' (ISO 8601 UTC
    timestamp strings) arrays:

        [{ "eID": "urn:uuid:<series_uuid>",
           "v":   [<number>, ...],
           "vT":  ["<ISO-8601-UTC>", ...] }]

    Files are visited in lexicographic order, which equals chronological
    order given the timestamp-based filename convention of Section 8.6.
    Residual ordering is corrected by the final sort.

    Raises
    ------
    FileNotFoundError
        If the values directory does not exist.
    ValueError
        If no value files are found, or if a fragment's 'v' and 't' arrays
        have different lengths.
    """
    values_dir = os.path.join(snon_path, "series", series_uuid, "values")
    if not os.path.isdir(values_dir):
        raise FileNotFoundError(
            f"Series values directory not found: {values_dir}"
        )

    filenames = sorted(f for f in os.listdir(values_dir) if f.endswith(".json"))
    if not filenames:
        raise ValueError(f"No value files found in: {values_dir}")

    samples: List[Tuple[float, float]] = []

    for filename in filenames:
        filepath = os.path.join(values_dir, filename)
        with open(filepath) as fh:
            pack = json.load(fh)

        if not isinstance(pack, list) or not pack:
            print(
                f"  Warning: skipping {filename} — not a non-empty JSON array.",
                file=sys.stderr,
            )
            continue

        fragment = pack[0]
        v_list = fragment.get("v", [])
        t_list = fragment.get("vT", [])

        if len(v_list) != len(t_list):
            raise ValueError(
                f"'v' and 't' arrays have different lengths in {filepath} "
                f"({len(v_list)} vs {len(t_list)})."
            )

        for t_str, v_val in zip(t_list, v_list):
            samples.append((_parse_iso8601(str(t_str)), float(v_val)))

    if not samples:
        raise ValueError(
            f"No samples could be read from series {series_uuid}. "
            "Check that the value files contain non-empty 'v' and 't' arrays."
        )

    samples.sort(key=lambda x: x[0])
    return samples


# ---------------------------------------------------------------------------
# Linear interpolation (binary search)
# ---------------------------------------------------------------------------

def _interpolate(samples: List[Tuple[float, float]], t: float) -> float:
    """
    Linearly interpolate *samples* at time *t*.

    *samples* must be sorted by time.  *t* is clamped to the sample range
    (prevents crashes from floating-point rounding at interval boundaries).
    """
    if t <= samples[0][0]:
        return samples[0][1]
    if t >= samples[-1][0]:
        return samples[-1][1]

    lo, hi = 0, len(samples) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if samples[mid][0] <= t:
            lo = mid
        else:
            hi = mid

    t0, v0 = samples[lo]
    t1, v1 = samples[hi]
    if t1 == t0:          # duplicate timestamps — return left value
        return v0
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


# ---------------------------------------------------------------------------
# Per-interval statistics
# ---------------------------------------------------------------------------

def _interval_stats(
    samples: List[Tuple[float, float]],
    t_start: float,
    t_end: float,
) -> Tuple[float, float, float]:
    """
    Compute (mean, min, max) for the 1-second interval [t_start, t_end].

    The point set used is:
        • the linearly interpolated value at t_start,
        • every raw sample strictly inside (t_start, t_end), and
        • the linearly interpolated value at t_end.

    The mean is the time-weighted (trapezoidal) average over that set.

    Returns (mean, v_min, v_max).
    """
    v_start = _interpolate(samples, t_start)
    v_end   = _interpolate(samples, t_end)

    interior = [(t, v) for t, v in samples if t_start < t < t_end]
    pts = [(t_start, v_start)] + interior + [(t_end, v_end)]

    values = [v for _, v in pts]
    v_min  = min(values)
    v_max  = max(values)

    area = 0.0
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        area += (v0 + v1) * 0.5 * (t1 - t0)

    mean = area / (t_end - t_start)
    return mean, v_min, v_max


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Format a numeric sensor value as a compact SNON string."""
    return f"{value:.9f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_to_snon(
    sensor_uuid: str,
    samples: List[Tuple[float, float]],
) -> Tuple[list, float]:
    """
    Resample *samples* to 1-second intervals and build a SNON value fragment.

    The output window runs from the first whole-second boundary strictly
    after the earliest sample to the last whole-second boundary strictly
    before the latest sample.

    Parameters
    ----------
    sensor_uuid : str
        Bare UUID (without 'urn:uuid:' prefix) placed in the output eID.
    samples : list of (unix_epoch, value)
        Must be sorted by time and contain at least 2 entries.

    Returns
    -------
    (snon_pack, t_win_start)
        snon_pack     : JSON-serialisable SNON pack (a list containing one
                        value fragment).
        t_win_start   : Unix epoch of the first interval start (used for
                        output file naming).

    Raises
    ------
    ValueError
        If fewer than 2 samples are provided, or if the sample span is too
        short to produce at least one complete 1-second interval.
    """
    if len(samples) < 2:
        raise ValueError("At least 2 samples are required for interpolation.")

    t_first = samples[0][0]
    t_last  = samples[-1][0]

    # First whole-second boundary strictly after the first sample:
    #   floor(t) + 1 is always > t, even when t is exactly an integer.
    t_win_start = int(math.floor(t_first)) + 1

    # Last whole-second boundary strictly before the last sample:
    #   For non-integer t: ceil(t) > t, so ceil(t)-1 < t  ✓
    #   For integer t:     ceil(t) == t, so ceil(t)-1 < t  ✓
    t_win_end = int(math.ceil(t_last)) - 1

    n_intervals = t_win_end - t_win_start
    if n_intervals < 1:
        raise ValueError(
            f"Data spans only {t_last - t_first:.3f} s — need at least "
            "enough data for one complete 1-second output interval."
        )

    v_arr    = []
    vmin_arr = []
    vmax_arr = []
    vt_arr   = []

    for i in range(n_intervals):
        t0 = float(t_win_start + i)
        t1 = t0 + 1.0

        mean, v_min, v_max = _interval_stats(samples, t0, t1)

        v_arr.append(_fmt(mean))
        vmin_arr.append(_fmt(v_min))
        vmax_arr.append(_fmt(v_max))
        vt_arr.append(f"{_epoch_to_iso8601(t0)}/PT1S")

    fragment = {
        "eID":  f"urn:uuid:{sensor_uuid}",
        "v":    v_arr,
        "vMin": vmin_arr,
        "vMax": vmax_arr,
        "vT":   vt_arr,
    }

    return [fragment], float(t_win_start)


# ---------------------------------------------------------------------------
# SNON filesystem output
# ---------------------------------------------------------------------------

def write_snon_value(
    snon_path: str,
    sensor_uuid: str,
    snon_pack: list,
    t_win_start: float,
) -> str:
    """
    Write *snon_pack* to sensors/<sensor_uuid>/values/<timestamp>.json.

    The filename is derived from *t_win_start* using the Section 8.6 naming
    convention: ISO 8601 UTC with colons replaced by hyphens.

    The file is opened with os.O_EXCL so that an existing file is never
    overwritten; value files are immutable once written (Section 8.6).

    Parameters
    ----------
    snon_path    : SNON root directory path.
    sensor_uuid  : Bare sensor UUID (directory name within sensors/).
    snon_pack    : JSON-serialisable SNON pack to write.
    t_win_start  : Unix epoch of the first interval start (drives naming).

    Returns
    -------
    Absolute path of the file that was written.

    Raises
    ------
    FileExistsError
        If a file with the same timestamp name already exists.
    """
    values_dir = os.path.join(snon_path, "sensors", sensor_uuid, "values")
    os.makedirs(values_dir, exist_ok=True)

    filename = f"{_epoch_to_filename_ts(t_win_start)}.json"
    out_path = os.path.join(values_dir, filename)

    # O_EXCL: fail rather than silently overwrite an immutable value file
    try:
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise FileExistsError(
            f"Output file already exists (value files are immutable, "
            f"Section 8.6): {out_path}"
        )

    with os.fdopen(fd, "w") as fh:
        json.dump(snon_pack, fh, indent=2)
        fh.write("\n")

    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snon_downsample.py",
        description=(
            "Resample a SNON series to per-second value fragments and write "
            "the result into the sensor's values/ directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Sensor with one series — series is selected automatically:\n"
            "  python snon_downsample.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --sensor 3f8a1c2d-1234-4e9b-abcd-000000000001\n"
            "\n"
            "  # Sensor with multiple series — series must be specified:\n"
            "  python snon_downsample.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --sensor 3f8a1c2d-1234-4e9b-abcd-000000000001 \\\n"
            "      --series  7a2b3c4d-5678-4f0a-bcde-000000000002\n"
        ),
    )
    parser.add_argument(
        "--snon-path",
        required=True,
        metavar="PATH",
        help="Path to the SNON root directory (e.g. ./snon/).",
    )
    parser.add_argument(
        "--sensor",
        required=True,
        metavar="UUID",
        help="UUID of the sensor to process.",
    )
    parser.add_argument(
        "--series",
        metavar="UUID",
        default=None,
        help=(
            "UUID of the series to resample. "
            "Required when the sensor has more than one associated series; "
            "omit when there is exactly one."
        ),
    )
    args = parser.parse_args()

    snon_path   = args.snon_path
    sensor_uuid = args.sensor.lower()
    series_uuid = args.series.lower() if args.series else None

    # ------------------------------------------------------------------
    # Validate UUIDs supplied on the command line
    # ------------------------------------------------------------------
    if not _is_valid_uuid(sensor_uuid):
        parser.error(f"--sensor: not a valid UUID: {sensor_uuid!r}")
    if series_uuid is not None and not _is_valid_uuid(series_uuid):
        parser.error(f"--series: not a valid UUID: {series_uuid!r}")

    if not os.path.isdir(snon_path):
        print(f"Error: SNON directory not found: {snon_path}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Discover series linked to the sensor
    # ------------------------------------------------------------------
    try:
        available = discover_series(snon_path, sensor_uuid)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not available:
        print(
            f"Error: no series found under "
            f"sensors/{sensor_uuid}/series/.\n"
            "  Ensure hxi-snond has run at least once to create the series entity.",
            file=sys.stderr,
        )
        sys.exit(1)

    if series_uuid is None:
        if len(available) == 1:
            series_uuid = available[0]
            print(f"Auto-selected series: {series_uuid}", file=sys.stderr)
        else:
            series_list = "".join(f"  {u}\n" for u in available)
            print(
                f"Error: sensor {sensor_uuid} has {len(available)} series. "
                "Specify one with --series:\n" + series_list,
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        if series_uuid not in available:
            series_list = ", ".join(available) or "(none)"
            print(
                f"Error: series {series_uuid} is not linked under "
                f"sensors/{sensor_uuid}/series/.\n"
                f"  Available: {series_list}",
                file=sys.stderr,
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # Read series value files
    # ------------------------------------------------------------------
    print(f"Reading series {series_uuid} …", file=sys.stderr)
    try:
        samples = read_snon_series(snon_path, series_uuid)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  Loaded {len(samples):,} samples.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Resample to 1-second intervals
    # ------------------------------------------------------------------
    print("Resampling to 1-second intervals …", file=sys.stderr)
    try:
        snon_pack, t_win_start = resample_to_snon(sensor_uuid, samples)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    n = len(snon_pack[0]["v"])
    print(f"  Produced {n:,} second(s) of resampled data.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Write output value fragment
    # ------------------------------------------------------------------
    try:
        out_path = write_snon_value(snon_path, sensor_uuid, snon_pack, t_win_start)
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
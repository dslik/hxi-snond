#!/usr/bin/env python3
"""
senml_to_snon.py — Resample SenML data to per-second SNON value fragments.

Usage:
    python senml_to_snon.py input.json [output.json]

    If output.json is omitted the result is written to stdout.

Output window:
    • Starts at the first whole-second boundary strictly after the first sample.
    • Ends   at the last  whole-second boundary strictly before the last sample.

For each 1-second interval [t, t+1] within that window the script:
    1. Linearly interpolates the signal at the exact start (t) and end (t+1).
    2. Collects every raw sample that falls strictly inside (t, t+1).
    3. Computes the time-weighted (trapezoidal) average over all those points.
    4. Finds the minimum and maximum over the same point set.

SNON output (Sensor Network Object Notation, Rev 4.0.0):
    A JSON array (SNON pack) containing a single value fragment:

    [
        {
            "eID":  "<sensor URN from SenML 'bn' field>",
            "eC":   "value",
            "v":    ["<avg_0>", "<avg_1>", ...],   // time-weighted mean
            "vMin": ["<min_0>", "<min_1>", ...],   // minimum in interval
            "vMax": ["<max_0>", "<max_1>", ...],   // maximum in interval
            "vT":   [                               // ISO 8601 interval: start/duration
                "<YYYY-MM-DDTHH:MM:SSZ/PT1S>",
                "<YYYY-MM-DDTHH:MM:SSZ/PT1S>",
                ...
            ]
        }
    ]

    Values are JSON strings (not numbers) per the SNON 4 specification.
    Every vT entry uses the full absolute start timestamp + PT1S duration.
"""

import json
import math
import sys
from datetime import datetime, timezone
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_iso8601(ts: str) -> float:
    """ISO 8601 UTC string → Unix epoch (float seconds)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _epoch_to_iso8601(epoch: float) -> str:
    """Unix epoch (whole seconds assumed) → ISO 8601 UTC string (no sub-seconds)."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# SenML parsing (RFC 8428)
# ---------------------------------------------------------------------------

def parse_senml(records: list) -> Tuple[str, List[Tuple[float, float]]]:
    """
    Parse a SenML JSON array.

    Handles base fields (bn, bt, bv) per RFC 8428.  'v' is taken as
    absolute-value + bv (default bv = 0); 't' may be an ISO 8601 string
    (absolute) or a number (offset from bt, default bt = 0).

    Returns
    -------
    (entity_id, samples)
        entity_id : the 'bn' value (used as SNON eID)
        samples   : list of (unix_time, value) sorted by time
    """
    entity_id  = "urn:unknown"
    base_time  = 0.0
    base_value = 0.0
    samples: List[Tuple[float, float]] = []

    for rec in records:
        # Update base fields whenever they appear
        if "bn" in rec:
            entity_id = rec["bn"]
        if "bt" in rec:
            bt = rec["bt"]
            base_time = _parse_iso8601(bt) if isinstance(bt, str) else float(bt)
        if "bv" in rec:
            base_value = float(rec["bv"])

        # Resolve timestamp
        t_raw = rec.get("t", 0)
        if isinstance(t_raw, str):
            abs_time = _parse_iso8601(t_raw)         # absolute ISO 8601
        else:
            abs_time = base_time + float(t_raw)      # relative offset from bt

        # Only include records that carry a numeric value
        if "v" in rec:
            samples.append((abs_time, base_value + float(rec["v"])))

    samples.sort(key=lambda x: x[0])
    return entity_id, samples


# ---------------------------------------------------------------------------
# Linear interpolation (binary search)
# ---------------------------------------------------------------------------

def _interpolate(samples: List[Tuple[float, float]], t: float) -> float:
    """
    Linearly interpolate *samples* at time *t*.

    *samples* must be sorted by time.  *t* is clamped to the sample range
    (should not be needed in practice but prevents crashes from fp rounding).
    """
    if t <= samples[0][0]:
        return samples[0][1]
    if t >= samples[-1][0]:
        return samples[-1][1]

    # Binary search for the bracketing pair
    lo, hi = 0, len(samples) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if samples[mid][0] <= t:
            lo = mid
        else:
            hi = mid

    t0, v0 = samples[lo]
    t1, v1 = samples[hi]
    if t1 == t0:          # duplicate timestamps – return left value
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
    Compute (mean, min, max) for the half-open interval starting at t_start,
    ending at t_end.

    The point set used consists of:
        • the linearly interpolated value at t_start,
        • every raw sample strictly inside (t_start, t_end), and
        • the linearly interpolated value at t_end.

    The mean is the time-weighted (trapezoidal) average over that set.

    Returns
    -------
    (mean, v_min, v_max)
    """
    v_start = _interpolate(samples, t_start)
    v_end   = _interpolate(samples, t_end)

    # Raw samples strictly interior to the interval
    interior = [(t, v) for t, v in samples if t_start < t < t_end]

    # Full ordered point set: interpolated endpoints + interior raws
    pts = [(t_start, v_start)] + interior + [(t_end, v_end)]

    values = [v for _, v in pts]
    v_min = min(values)
    v_max = max(values)

    # Trapezoidal integration for time-weighted average
    area = 0.0
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        area += (v0 + v1) * 0.5 * (t1 - t0)

    mean = area / (t_end - t_start)
    return mean, v_min, v_max


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Format a numeric sensor value as a compact SNON string."""
    # Use up to 9 significant figures, strip trailing zeros after the decimal
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text


def senml_to_snon(senml_records: list) -> list:
    """
    Convert a SenML record list to a SNON pack with 1-second resampled
    value fragments.

    Returns the SNON pack as a Python list (ready for json.dumps).
    """
    entity_id, samples = parse_senml(senml_records)

    if len(samples) < 2:
        raise ValueError("At least 2 samples are required for interpolation.")

    t_first = samples[0][0]
    t_last  = samples[-1][0]

    # Next whole-second boundary strictly after the first sample
    #   floor(t) + 1  always > t  (even when t is exactly an integer)
    t_start = int(math.floor(t_first)) + 1

    # Last whole-second boundary strictly before the last sample
    #   ceil(t) - 1  works for both integer and non-integer t:
    #     non-integer: ceil > t, so ceil-1 < t  ✓
    #     integer:     ceil == t, so ceil-1 == t-1 < t  ✓
    t_end = int(math.ceil(t_last)) - 1

    n_intervals = t_end - t_start
    if n_intervals < 1:
        raise ValueError(
            f"Data spans only {t_last - t_first:.3f} s – need at least "
            "enough data for one complete 1-second output interval."
        )

    # ------------------------------------------------------------------ #
    # Compute per-second statistics                                        #
    # ------------------------------------------------------------------ #
    v_arr    = []   # time-weighted mean
    vmin_arr = []   # minimum
    vmax_arr = []   # maximum
    vt_arr   = []   # ISO 8601 time / duration strings

    for i in range(n_intervals):
        t0 = t_start + i
        t1 = t0 + 1

        mean, v_min, v_max = _interval_stats(samples, float(t0), float(t1))

        v_arr.append(_fmt(mean))
        vmin_arr.append(_fmt(v_min))
        vmax_arr.append(_fmt(v_max))

        # Every entry: absolute start timestamp + duration
        vt_arr.append(f"{_epoch_to_iso8601(t0)}/PT1S")

    # ------------------------------------------------------------------ #
    # Build SNON value fragment                                            #
    # ------------------------------------------------------------------ #
    fragment = {
        "eID":  entity_id,
        "eC":   "value",
        "v":    v_arr,
        "vMin": vmin_arr,
        "vMax": vmax_arr,
        "vT":   vt_arr,
    }

    return [fragment]          # a SNON pack is always a JSON array


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(
            f"Usage: {sys.argv[0]} <senml_file.json> [snon_output.json]\n"
            "  Output goes to stdout when no output file is given.",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path  = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    with open(input_path) as fh:
        senml_records = json.load(fh)

    snon_pack = senml_to_snon(senml_records)
    text = json.dumps(snon_pack, indent=2)

    if output_path:
        with open(output_path, "w") as fh:
            fh.write(text)
            fh.write("\n")
        n = len(snon_pack[0]["v"])
        print(
            f"Wrote {n} second(s) of resampled data → {output_path}",
            file=sys.stderr,
        )
    else:
        print(text)


if __name__ == "__main__":
    main()
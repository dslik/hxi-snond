#!/usr/bin/env python3
"""
snon_tree.py — SNON 4.0 Tree Visualization Tool

Walks the SNON 4.0 filesystem tree and prints a hierarchical view of the
location and device topology.  Root entities — locations and devices that
have no child_of relationship — form the top of the tree.  Location roots
are shown first; device roots not reachable from any location follow as
separate subtrees.

Default output shows locations, devices, and sensors.  The --series flag
additionally shows series entities under each sensor.  The --values flag
additionally shows value file leaf nodes under whichever entity types are
being displayed (sensors, and series when --series is also given).

Usage:
    python snon_tree.py --snon-path PATH [--series] [--values]

Arguments:
    --snon-path PATH    Path to the SNON root directory (e.g. ./snon/)
    --series            Also show series entities under each sensor
    --values            Also show value files under sensors (and series
                        when --series is set)

Exit codes:
    0  Success
    1  Error
"""

import argparse
import json
import os
import re
import shutil
import sys
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# UUID validation  (same pattern as rest of suite)
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_valid_uuid(s: str) -> bool:
    """Return True if *s* is a canonically-formatted RFC 4122 UUID."""
    return bool(_UUID_RE.match(s))


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

#: Short display label for each class directory.
LABELS: Dict[str, str] = {
    "locations": "loc",
    "devices":   "dev",
    "sensors":   "sen",
    "series":    "ser",
}

#: Name of the optional identification tag field for each entity type.
TAG_FIELDS: Dict[str, str] = {
    "locations": "lT",
    "devices":   "dT",
    "sensors":   "sT",
}

# Tree-drawing characters.
_TEE   = "├── "
_ELBOW = "└── "
_PIPE  = "│   "
_BLANK = "    "


# ---------------------------------------------------------------------------
# Fragment I/O
# ---------------------------------------------------------------------------

def _load_fragment(snon_path: str, class_dir: str, entity_uuid: str) -> Optional[dict]:
    """
    Load and return the primary fragment (the one whose eID matches
    entity_uuid) from the canonical pack file.  Returns None if the file
    is missing, unreadable, or contains no matching fragment.
    """
    path = os.path.join(snon_path, class_dir, entity_uuid + ".json")
    try:
        with open(path) as fh:
            pack = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pack, list):
        return None
    target = "urn:uuid:" + entity_uuid
    for item in pack:
        if isinstance(item, dict) and item.get("eID") == target:
            return item
    return None


def _load_value_fragment(
    snon_path: str,
    class_dir: str,
    entity_uuid: str,
    filename: str,
) -> Optional[dict]:
    """
    Load the first fragment from a value file at
    <class_dir>/<entity_uuid>/values/<filename>.  Returns None on error.
    """
    path = os.path.join(snon_path, class_dir, entity_uuid, "values", filename)
    try:
        with open(path) as fh:
            pack = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(pack, list) and pack and isinstance(pack[0], dict):
        return pack[0]
    return None


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _entity_name(frag: dict) -> str:
    """Return the best available display name from eN, or '(unnamed)'."""
    en = frag.get("eN") or {}
    if not isinstance(en, dict):
        return "(unnamed)"
    return en.get("*") or next(iter(en.values()), "(unnamed)")


def _entity_content(class_dir: str, frag: dict) -> str:
    """
    Return the left-hand content of an entity line, without the UUID.
    The UUID is printed separately, right-justified, by _print_rjust.

    Format: [label]  Name  (tag)
    The tag is omitted when absent.
    """
    label     = LABELS.get(class_dir, "???")
    name      = _entity_name(frag)
    tag_field = TAG_FIELDS.get(class_dir)
    tag       = frag.get(tag_field, "") if tag_field else ""

    parts: List[str] = [f"[{label}]", name]
    if tag:
        parts.append(f"({tag})")
    return "  ".join(parts)


def _term_width() -> int:
    """Return the current terminal width, defaulting to 120 if not a TTY."""
    return shutil.get_terminal_size(fallback=(120, 24)).columns


def _print_rjust(left: str, uuid: str) -> None:
    """
    Print *left* followed by *uuid* (36 chars) right-justified to the
    terminal width.  A minimum of two spaces always separates them.
    """
    width    = _term_width()
    uuid_col = width - 37          # column at which the UUID starts
    gap      = uuid_col - len(left)
    if gap >= 2:
        print(left + " " * gap + uuid + " ")
    else:
        print(left + "  " + uuid + " ")  # fallback when line is already wide


def _value_line(frag: dict) -> str:
    """
    Return a single formatted line summarising a value fragment.

    Format: [val]  <vT[0]>  <v[0]>  (min:<vMin[0]> max:<vMax[0]>)  [N values]
    The min/max and count suffix are omitted when not applicable.
    """
    v_list  = frag.get("v")    or []
    vt_list = frag.get("vT")   or []
    vmin    = frag.get("vMin") or []
    vmax    = frag.get("vMax") or []

    ts  = vt_list[0] if vt_list else "(no timestamp)"
    val = v_list[0]  if v_list  else "(no value)"
    n   = len(v_list)

    parts: List[str] = [f"[val]  {ts}  {val}"]
    if vmin and vmax:
        parts.append(f"(min:{vmin[0]} max:{vmax[0]})")
    if n > 1:
        parts.append(f"[{n} values]")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Child discovery via filesystem symlinks  (§8.7)
# ---------------------------------------------------------------------------

def _child_uuids(
    snon_path: str,
    class_dir: str,
    entity_uuid: str,
    subdir: str,
) -> List[str]:
    """
    Return sorted UUIDs found as .json symlinks in
    <snon_path>/<class_dir>/<entity_uuid>/<subdir>/.

    Lexicographic sort of UUID filenames produces a stable, reproducible
    output order.  Only entries that parse as valid UUIDs are returned;
    other files in the directory are silently ignored.
    """
    path = os.path.join(snon_path, class_dir, entity_uuid, subdir)
    if not os.path.isdir(path):
        return []
    uuids: List[str] = []
    for entry in sorted(os.listdir(path)):
        if entry.endswith(".json"):
            stem = entry[: -len(".json")]
            if _is_valid_uuid(stem):
                uuids.append(stem)
    return uuids


def _value_filenames(
    snon_path: str,
    class_dir: str,
    entity_uuid: str,
) -> List[str]:
    """
    Return sorted value filenames from <class_dir>/<entity_uuid>/values/.
    Lexicographic order equals chronological order per §8.6.
    """
    path = os.path.join(snon_path, class_dir, entity_uuid, "values")
    if not os.path.isdir(path):
        return []
    return sorted(f for f in os.listdir(path) if f.endswith(".json"))


# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------

def _find_roots(snon_path: str, class_dir: str) -> List[str]:
    """
    Return sorted UUIDs of all entities in class_dir that carry no
    child_of relationship — these are the natural roots of the tree.

    An entity is considered a root when its eR.child_of field is absent,
    empty, or not present at all.
    """
    dir_path = os.path.join(snon_path, class_dir)
    if not os.path.isdir(dir_path):
        return []

    roots: List[str] = []
    for entry in sorted(os.listdir(dir_path)):
        if not entry.endswith(".json"):
            continue
        stem = entry[: -len(".json")]
        if not _is_valid_uuid(stem):
            continue
        frag = _load_fragment(snon_path, class_dir, stem)
        if frag is None:
            continue
        child_of = (frag.get("eR") or {}).get("child_of") or []
        if not child_of:
            roots.append(stem)
    return roots


# ---------------------------------------------------------------------------
# Recursive tree renderer
# ---------------------------------------------------------------------------

def _print_values(
    snon_path: str,
    class_dir: str,
    entity_uuid: str,
    prefix: str,
    after_siblings: bool,
) -> None:
    """
    Print value file leaf nodes for entity_uuid.

    after_siblings is True when entity children have already been printed
    above these value nodes, so the correct connector (├ vs └) is chosen
    for each file.
    """
    filenames = _value_filenames(snon_path, class_dir, entity_uuid)
    for i, filename in enumerate(filenames):
        is_last = (i == len(filenames) - 1)
        connector = _ELBOW if is_last else _TEE
        frag = _load_value_fragment(snon_path, class_dir, entity_uuid, filename)
        if frag is None:
            print(f"{prefix}{connector}[val]  {filename}  (unreadable)")
        else:
            print(f"{prefix}{connector}{_value_line(frag)}")


def _print_entity(
    snon_path: str,
    class_dir: str,
    entity_uuid: str,
    prefix: str,
    is_last: bool,
    show_series: bool,
    show_values: bool,
    visited: Set[Tuple[str, str]],
) -> None:
    """
    Recursively print an entity and all of its visible descendants.

    prefix      : The indentation string accumulated by the caller.
    is_last     : True when this entity is the last child of its parent,
                  which governs which connector character to use.
    visited     : Mutable set of (class_dir, uuid) pairs already printed;
                  updated in place to detect entities reachable via multiple
                  symlink paths.
    """
    connector = _ELBOW if is_last else _TEE
    extension = _BLANK if is_last else _PIPE

    key = (class_dir, entity_uuid)
    if key in visited:
        # Entity already shown elsewhere in the tree (multiple symlink paths).
        frag    = _load_fragment(snon_path, class_dir, entity_uuid)
        label   = LABELS.get(class_dir, "???")
        name    = _entity_name(frag) if frag else "?"
        content = f"{prefix}{connector}[{label}]  {name}  ↑ already shown"
        _print_rjust(content, entity_uuid)
        return
    visited.add(key)

    frag = _load_fragment(snon_path, class_dir, entity_uuid)
    if frag is None:
        label = LABELS.get(class_dir, "???")
        _print_rjust(f"{prefix}{connector}[{label}]  (unreadable)", entity_uuid)
        return

    uuid    = frag.get("eID", "").replace("urn:uuid:", "")
    content = prefix + connector + _entity_content(class_dir, frag)
    _print_rjust(content, uuid)
    child_prefix = prefix + extension

    # Build the ordered list of entity children to recurse into.
    children: List[Tuple[str, str]] = []  # (class_dir, uuid)

    if class_dir == "locations":
        for u in _child_uuids(snon_path, "locations", entity_uuid, "locations"):
            children.append(("locations", u))
        for u in _child_uuids(snon_path, "locations", entity_uuid, "devices"):
            children.append(("devices", u))

    elif class_dir == "devices":
        for u in _child_uuids(snon_path, "devices", entity_uuid, "devices"):
            children.append(("devices", u))
        for u in _child_uuids(snon_path, "devices", entity_uuid, "sensors"):
            children.append(("sensors", u))

    elif class_dir == "sensors":
        if show_series:
            for u in _child_uuids(snon_path, "sensors", entity_uuid, "series"):
                children.append(("series", u))

    # Decide whether value files will appear after the entity children.
    has_values = show_values and class_dir in ("sensors", "series")
    value_files = _value_filenames(snon_path, class_dir, entity_uuid) if has_values else []

    # Print entity children.
    for i, (child_class, child_uuid) in enumerate(children):
        child_is_last = (i == len(children) - 1) and not value_files
        _print_entity(
            snon_path, child_class, child_uuid,
            child_prefix, child_is_last,
            show_series, show_values, visited,
        )

    # Print value leaf nodes after entity children.
    for i, filename in enumerate(value_files):
        val_is_last = (i == len(value_files) - 1)
        connector_v = _ELBOW if val_is_last else _TEE
        val_frag = _load_value_fragment(snon_path, class_dir, entity_uuid, filename)
        if val_frag is None:
            print(f"{child_prefix}{connector_v}[val]  {filename}  (unreadable)")
        else:
            print(f"{child_prefix}{connector_v}{_value_line(val_frag)}")


# ---------------------------------------------------------------------------
# Top-level tree renderer
# ---------------------------------------------------------------------------

def print_tree(snon_path: str, show_series: bool, show_values: bool) -> None:
    """
    Print the full SNON entity tree to stdout.

    Location roots are enumerated first.  Any device roots not encountered
    during the location traversal (devices unattached to any location) are
    shown as separate top-level entries afterward.
    """
    print(os.path.join(snon_path, ""))  # trailing slash makes it look like a dir

    location_roots = _find_roots(snon_path, "locations")
    device_roots   = _find_roots(snon_path, "devices")

    # All top-level entries: location roots first, then unattached device roots.
    # The visited set filled during location traversal is used to suppress
    # device roots that were already printed as children of a location.
    visited: Set[Tuple[str, str]] = set()

    top_entries: List[Tuple[str, str]] = (
        [("locations", u) for u in location_roots] +
        [("devices",   u) for u in device_roots]
    )

    # Filter device roots: remove any already visited (reached via location).
    # We can only know this after traversal, so collect a two-pass approach:
    # first pass for locations, second for the remaining device roots.
    loc_entries = [("locations", u) for u in location_roots]
    dev_entries = [("devices",   u) for u in device_roots]

    # Print location subtrees.
    for i, (class_dir, uuid) in enumerate(loc_entries):
        # After locations are done we'll append the remaining device roots;
        # for now determine is_last assuming devices may follow.
        is_last = (i == len(loc_entries) - 1) and not dev_entries
        _print_entity(
            snon_path, class_dir, uuid,
            prefix="", is_last=is_last,
            show_series=show_series, show_values=show_values,
            visited=visited,
        )

    # Print device roots not already shown under a location.
    remaining_devs = [u for u in device_roots if ("devices", u) not in visited]
    for i, uuid in enumerate(remaining_devs):
        is_last = (i == len(remaining_devs) - 1)
        _print_entity(
            snon_path, "devices", uuid,
            prefix="", is_last=is_last,
            show_series=show_series, show_values=show_values,
            visited=visited,
        )

    if not location_roots and not device_roots:
        print("(no root entities found)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snon_tree.py",
        description=(
            "Print a visual tree of the SNON 4.0 location and device hierarchy. "
            "Root entities (those with no child_of relationship) form the top "
            "of the tree."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Show the location/device/sensor hierarchy:\n"
            "  python snon_tree.py --snon-path ./snon/\n"
            "\n"
            "  # Also show series entities under each sensor:\n"
            "  python snon_tree.py --snon-path ./snon/ --series\n"
            "\n"
            "  # Show everything including value files:\n"
            "  python snon_tree.py --snon-path ./snon/ --series --values\n"
        ),
    )
    parser.add_argument(
        "--snon-path",
        required=True,
        metavar="PATH",
        help="Path to the SNON root directory (e.g. ./snon/).",
    )
    parser.add_argument(
        "--series",
        action="store_true",
        default=False,
        help="Also show series entities under each sensor.",
    )
    parser.add_argument(
        "--values",
        action="store_true",
        default=False,
        help=(
            "Also show value file leaf nodes under sensors "
            "(and under series when --series is also given)."
        ),
    )

    args = parser.parse_args()

    snon_path = args.snon_path

    if not os.path.isdir(snon_path):
        print(f"Error: SNON directory not found: {snon_path}", file=sys.stderr)
        sys.exit(1)

    try:
        print_tree(snon_path, show_series=args.series, show_values=args.values)
    except OSError as exc:
        print(f"\nFilesystem error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
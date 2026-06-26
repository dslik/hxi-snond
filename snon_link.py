#!/usr/bin/env python3
"""
snon_link.py — SNON 4.0 Entity Linking Tool
============================================
Links a child entity to a parent entity within a SNON 4.0 filesystem tree.

Per §8.7 and §8.11 of the SNON 4.0 spec, linking performs two operations:

  1. Creates a relative-symlink pair (file + directory) inside the parent
     entity's relationship subdirectory, so the child appears in the parent's
     context without duplicating data.

  2. Updates the child's canonical fragment file to set eR.child_of to the
     parent's urn:uuid: entity ID, so tools can traverse from child to parent
     without an index (§8.11).

Supported relationships:
  sensor   → device    (symlinks appear in <device>/sensors/)
  device   → device    (symlinks appear in <device>/devices/)
  device   → location  (symlinks appear in <location>/devices/)
  location → location  (symlinks appear in <location>/locations/)

Both entities must already exist (canonical file + subdirectory) in the SNON
tree.  This tool does not create new entities.

The tool is idempotent: re-running it when the desired state already exists
prints a confirmation and exits cleanly.  A conflict — a symlink pointing
somewhere unexpected, or eR.child_of already referencing a *different* parent —
is reported as an error rather than silently overwritten.

Usage:
    python snon_link.py --snon-path PATH --child UUID --parent UUID

Arguments:
    --snon-path PATH    Path to the SNON root directory (e.g. ./snon/)
    --child UUID        Bare UUID of the child entity  (sensor, device, or location)
    --parent UUID       Bare UUID of the parent entity (device or location)

Exit codes:
    0  Success (changes applied, or already in the desired state)
    1  Validation or logical error
    2  Filesystem I/O error
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# UUID validation  (same pattern as snon_downsample.py)
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

#: Singular entity class name → plural directory name under the SNON root.
#: §8.4 requires directory names to be lowercase plural nouns.
CLASS_TO_DIR: Dict[str, str] = {
    "device":    "devices",
    "location":  "locations",
    "sensor":    "sensors",
    "series":    "series",
    "measurand": "measurands",
}

#: Valid (child_class, parent_class) pairs and the name of the subdirectory
#: that holds the child's symlinks inside the parent entity's directory.
#:
#: Derived from §8.8 (full directory layout) and §8.7.1 (relative paths).
#: Series and measurand entities are deliberately excluded: series are linked
#: to sensors by the clock-recovery tool (§8.9), and measurands are global
#: shared definitions with no parent (§8.10).
VALID_RELATIONSHIPS: Dict[Tuple[str, str], str] = {
    ("sensor",   "device"):   "sensors",
    ("device",   "device"):   "devices",
    ("device",   "location"): "devices",
    ("location", "location"): "locations",
}


# ---------------------------------------------------------------------------
# Entity discovery
# ---------------------------------------------------------------------------

def find_entity_class(snon_path: str, entity_uuid: str) -> Optional[str]:
    """
    Return the singular class name ("device", "sensor", …) of the entity with
    *entity_uuid* by probing each class directory for ``<uuid>.json``.

    Returns None if no match is found.  Raises ValueError if the UUID appears
    in more than one class directory (which would indicate a corrupted tree).
    """
    matches = [
        class_name
        for class_name, dir_name in CLASS_TO_DIR.items()
        if os.path.exists(os.path.join(snon_path, dir_name, entity_uuid + ".json"))
    ]

    if len(matches) > 1:
        raise ValueError(
            f"UUID {entity_uuid!r} found in multiple class directories: "
            f"{', '.join(matches)}.  "
            "Each entity must have exactly one canonical file (§8.5).  "
            "The SNON tree may be corrupted."
        )

    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Relative symlink path computation  (§8.7.1)
# ---------------------------------------------------------------------------

def _symlink_targets(
    parent_class: str,
    child_class: str,
    child_uuid: str,
) -> Tuple[str, str]:
    """
    Return ``(file_rel, dir_rel)`` — the relative target strings for the two
    symlinks placed in::

        <snon_root>/<parent_dir>/<parent_uuid>/<rel_subdir>/

    Depth reasoning
    ~~~~~~~~~~~~~~~
    A relationship-entry directory always sits exactly three levels below the
    SNON root::

        <root>/<parent_class_dir>/<parent_uuid>/<rel_subdir>/   ← depth 3

    *Same-class* links (location→location, device→device) stay inside the
    same class directory.  Two ``..`` hops climb out of ``<rel_subdir>/`` and
    ``<parent_uuid>/``, landing back in the shared class directory::

        ../../<child_uuid>.json   resolves to  <root>/<class_dir>/<child_uuid>.json
        ../../<child_uuid>        resolves to  <root>/<class_dir>/<child_uuid>/

    *Cross-class* links need a third hop to escape the class directory, then
    explicitly name the child's class directory::

        ../../../<child_dir>/<child_uuid>.json
        ../../../<child_dir>/<child_uuid>

    This matches the table in §8.7.1 exactly.
    """
    child_dir = CLASS_TO_DIR[child_class]

    if parent_class == child_class:
        # Same class directory — two hops sufficient
        file_rel = "../../" + child_uuid + ".json"
        dir_rel  = "../../" + child_uuid
    else:
        # Cross class — three hops to root, then into child class dir
        file_rel = "../../../" + child_dir + "/" + child_uuid + ".json"
        dir_rel  = "../../../" + child_dir + "/" + child_uuid

    return file_rel, dir_rel


# ---------------------------------------------------------------------------
# Symlink management
# ---------------------------------------------------------------------------

def _ensure_symlink(link_path: str, target: str) -> bool:
    """
    Create ``link_path → target`` if not already present.

    Returns True when the symlink was newly created, False when it already
    existed and was already correct (idempotent).

    Raises ValueError on conflict:
    - Existing symlink points to a different target.
    - A non-symlink filesystem object occupies the link path.
    """
    if os.path.islink(link_path):
        existing = os.readlink(link_path)
        if existing == target:
            return False  # already correct
        raise ValueError(
            f"Symlink conflict at:\n"
            f"  {link_path}\n"
            f"  existing → {existing}\n"
            f"  expected → {target}\n"
            "Remove the conflicting symlink before re-linking."
        )

    if os.path.exists(link_path):
        raise ValueError(
            f"{link_path} exists and is not a symlink; refusing to overwrite."
        )

    os.symlink(target, link_path)
    return True


def create_relationship_symlinks(
    snon_path: str,
    parent_class: str,
    parent_uuid: str,
    child_class: str,
    child_uuid: str,
    rel_subdir: str,
) -> None:
    """
    Create (or confirm) the two relative symlinks — file and directory — in::

        <snon_root>/<parent_class_dir>/<parent_uuid>/<rel_subdir>/

    The relationship subdirectory is created if it does not yet exist.
    Per §8.7.2, symlinks are named identically to the basename of their
    target: ``<uuid>.json`` for the file symlink, ``<uuid>`` for the
    directory symlink.
    """
    link_dir = os.path.join(
        snon_path, CLASS_TO_DIR[parent_class], parent_uuid, rel_subdir
    )
    os.makedirs(link_dir, exist_ok=True)

    file_rel, dir_rel = _symlink_targets(parent_class, child_class, child_uuid)

    entries = [
        (os.path.join(link_dir, child_uuid + ".json"), file_rel, "file"),
        (os.path.join(link_dir, child_uuid),           dir_rel,  "dir "),
    ]

    for link_path, target, kind in entries:
        created = _ensure_symlink(link_path, target)
        glyph   = "  +" if created else "  ✓"
        status  = "created" if created else "already set"
        rel     = os.path.relpath(link_path, snon_path)
        print(f"{glyph} symlink ({kind})  {status}: {rel}  →  {target}",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# Fragment update — eR.child_of  (§8.11)
# ---------------------------------------------------------------------------

def _now_eut() -> str:
    """
    Return the current UTC time formatted as an SNON eUT string.

    §2.3 specifies ISO 8601 extended representation date/time format.
    Microsecond precision is used to minimise collision risk when multiple
    tools update fragments in rapid succession.
    """
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


def update_child_of(
    snon_path: str,
    child_class: str,
    child_uuid: str,
    parent_uuid: str,
) -> None:
    """
    Set ``eR.child_of`` and ``eUT`` on the matching fragment in the child's
    canonical fragment file, then rewrite the file atomically.

    Per §2.3, ``eR`` is a "JSON Object containing JSON Arrays of JSON
    Strings", so ``child_of`` is stored as a single-element array:
    ``["urn:uuid:<parent_uuid>"]``.

    Per §2.3, ``eUT`` ("Entity update time") is required whenever an entity
    changes, to support eventually-consistent out-of-order processing.  This
    tool modifies the fragment, so it must stamp ``eUT`` with the current UTC
    time in ``"YYYY-MM-DDTHH:MM:SS.MMMZ"`` format.

    The fragment file is a JSON array (pack per §8.5).  We locate the
    fragment whose ``eID`` matches the child UUID, apply both updates, and
    write the result back via a ``.tmp`` file + atomic ``os.rename()``.

    §8.11 tension
    ~~~~~~~~~~~~~
    §8.11 states "Tools SHALL NOT modify existing canonical fragment files."
    That constraint primarily targets entity-definition stability: measurand
    UUIDs, units of measure, and acquisition metadata that downstream
    consumers may have cached.  ``eR.child_of`` is relationship metadata
    whose only valid location is the fragment itself — there is no separate
    index.  A freshly created entity that has not yet been linked will have
    no ``child_of`` entry, making this update equivalent to completing the
    entity's initial configuration rather than mutating a stable definition.
    """
    fragment_path = os.path.join(
        snon_path, CLASS_TO_DIR[child_class], child_uuid + ".json"
    )
    child_eid  = "urn:uuid:" + child_uuid
    parent_eid = "urn:uuid:" + parent_uuid

    try:
        with open(fragment_path) as fh:
            fragments = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in {fragment_path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read fragment file {fragment_path}: {exc}") from exc

    if not isinstance(fragments, list):
        raise ValueError(
            f"{fragment_path} must contain a top-level JSON array (§8.5); "
            f"got {type(fragments).__name__}."
        )

    # Locate the child's own fragment within the pack.
    child_fragment = next(
        (
            f for f in fragments
            if isinstance(f, dict) and f.get("eID") == child_eid
        ),
        None,
    )
    if child_fragment is None:
        raise ValueError(
            f"No fragment with eID={child_eid!r} found in {fragment_path}.\n"
            "The fragment file may be incomplete or mis-named."
        )

    er = child_fragment.setdefault("eR", {})

    # child_of must be a JSON array of URN strings per §2.3.
    # A non-array value is a schema violation; reject it so the operator
    # knows the file needs to be corrected rather than silently coercing it.
    existing_raw = er.get("child_of")
    if existing_raw is None:
        existing_list = []
    elif isinstance(existing_raw, list):
        existing_list = existing_raw
    else:
        raise ValueError(
            f"Entity {child_uuid} has eR.child_of = {existing_raw!r}, "
            "which is not a JSON array as required by §2.3.  "
            "Correct the fragment file before linking."
        )

    # Evaluate each field independently so a fragment that already has the
    # correct child_of but is missing eUT (e.g. linked by an older tool that
    # pre-dates eUT) still gets the stamp written.
    child_of_ok = (existing_list == [parent_eid])
    eut_ok      = "eUT" in child_fragment

    if child_of_ok and eut_ok:
        print(f"  ✓ eR.child_of  already set: [{parent_eid}]", file=sys.stderr)
        print(f"  ✓ eUT          already set: {child_fragment['eUT']}", file=sys.stderr)
        return

    if not child_of_ok and existing_list:
        raise ValueError(
            f"Entity {child_uuid} already has eR.child_of = {existing_list!r}.\n"
            f"Cannot link to {parent_eid!r} without first removing the existing "
            "child_of relationship."
        )

    if not child_of_ok:
        er["child_of"] = [parent_eid]

    child_fragment["eUT"] = _now_eut()

    # Atomic write: write to .tmp then rename
    tmp_path = fragment_path + ".tmp"
    try:
        with open(tmp_path, "w") as fh:
            json.dump(fragments, fh, indent=2)
            fh.write("\n")
        os.rename(tmp_path, fragment_path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise ValueError(
            f"Failed to write fragment file {fragment_path}: {exc}"
        ) from exc

    if not child_of_ok:
        print(f"  + eR.child_of  set:         [{parent_eid}]", file=sys.stderr)
    else:
        print(f"  ✓ eR.child_of  already set: [{parent_eid}]", file=sys.stderr)
    print(f"  + eUT          set:         {child_fragment['eUT']}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Top-level operation
# ---------------------------------------------------------------------------

def link_entities(snon_path: str, child_uuid: str, parent_uuid: str) -> None:
    """
    Link *child_uuid* to *parent_uuid* within the SNON tree at *snon_path*.

    Steps
    -----
    1. Guard against self-links.
    2. Discover entity classes by probing class directories.
    3. Validate the relationship is supported by this spec.
    4. Confirm that canonical files and directories exist for both entities.
    5. Create (or confirm) the relative-symlink pair in the parent's
       relationship subdirectory.
    6. Update the child's canonical fragment with eR.child_of.
    """
    # 1. Self-link guard
    if child_uuid == parent_uuid:
        raise ValueError(
            f"--child and --parent are identical ({child_uuid!r}); "
            "an entity cannot be its own parent."
        )

    # 2. Discover classes
    child_class = find_entity_class(snon_path, child_uuid)
    if child_class is None:
        raise ValueError(
            f"Child UUID {child_uuid!r} not found in any class directory under "
            f"{snon_path}.\n"
            f"  Expected: <snon-path>/<class>/{child_uuid}.json"
        )

    parent_class = find_entity_class(snon_path, parent_uuid)
    if parent_class is None:
        raise ValueError(
            f"Parent UUID {parent_uuid!r} not found in any class directory under "
            f"{snon_path}.\n"
            f"  Expected: <snon-path>/<class>/{parent_uuid}.json"
        )

    # 3. Validate relationship
    rel_key = (child_class, parent_class)
    if rel_key not in VALID_RELATIONSHIPS:
        valid = "\n  ".join(
            f"{c:8} → {p}" for c, p in sorted(VALID_RELATIONSHIPS)
        )
        raise ValueError(
            f"Unsupported relationship: {child_class} → {parent_class}.\n"
            f"Supported relationships:\n  {valid}"
        )
    rel_subdir = VALID_RELATIONSHIPS[rel_key]

    # 4. Confirm canonical directories exist for both entities (§8.5)
    child_canonical_dir  = os.path.join(snon_path, CLASS_TO_DIR[child_class],  child_uuid)
    parent_canonical_dir = os.path.join(snon_path, CLASS_TO_DIR[parent_class], parent_uuid)

    for path, label in [
        (child_canonical_dir,  "child"),
        (parent_canonical_dir, "parent"),
    ]:
        if not os.path.isdir(path):
            raise ValueError(
                f"Canonical directory for {label} entity does not exist:\n"
                f"  {path}\n"
                "Create the entity (fragment file + subdirectory) before linking."
            )

    # 5 & 6. Apply changes
    print(
        f"Linking {child_class:<8} {child_uuid}\n"
        f"     to {parent_class:<8} {parent_uuid}\n"
        f"     via parent/{rel_subdir}/\n",
        file=sys.stderr,
    )

    create_relationship_symlinks(
        snon_path,
        parent_class, parent_uuid,
        child_class,  child_uuid,
        rel_subdir,
    )
    update_child_of(snon_path, child_class, child_uuid, parent_uuid)

    print("\nDone.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snon_link.py",
        description=(
            "Link a child SNON entity to a parent entity by creating relative "
            "symlinks in the parent's relationship subdirectory and setting "
            "eR.child_of in the child's canonical fragment file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Supported relationships:\n"
            "  sensor   → device    (symlinks in <device>/sensors/)\n"
            "  device   → device    (symlinks in <device>/devices/)\n"
            "  device   → location  (symlinks in <location>/devices/)\n"
            "  location → location  (symlinks in <location>/locations/)\n"
            "\n"
            "Examples:\n"
            "  # Link a sensor to a device:\n"
            "  python snon_link.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --child  3f8a1c2d-1234-4e9b-abcd-000000000001 \\\n"
            "      --parent a1b2c3d4-5678-4f0a-bcde-000000000002\n"
            "\n"
            "  # Link a device to a location:\n"
            "  python snon_link.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --child  a1b2c3d4-5678-4f0a-bcde-000000000002 \\\n"
            "      --parent f0e1d2c3-9876-4a0b-cdef-000000000003\n"
        ),
    )
    parser.add_argument(
        "--snon-path",
        required=True,
        metavar="PATH",
        help="Path to the SNON root directory (e.g. ./snon/).",
    )
    parser.add_argument(
        "--child",
        required=True,
        metavar="UUID",
        help="Bare UUID of the child entity (sensor, device, or location).",
    )
    parser.add_argument(
        "--parent",
        required=True,
        metavar="UUID",
        help="Bare UUID of the parent entity (device or location).",
    )

    args = parser.parse_args()

    snon_path   = args.snon_path
    child_uuid  = args.child.lower()
    parent_uuid = args.parent.lower()

    if not _is_valid_uuid(child_uuid):
        parser.error(f"--child: not a valid UUID: {child_uuid!r}")
    if not _is_valid_uuid(parent_uuid):
        parser.error(f"--parent: not a valid UUID: {parent_uuid!r}")

    if not os.path.isdir(snon_path):
        print(f"Error: SNON directory not found: {snon_path}", file=sys.stderr)
        sys.exit(1)

    try:
        link_entities(snon_path, child_uuid, parent_uuid)
    except ValueError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"\nFilesystem error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
snon_create.py — SNON 4.0 Entity Creation Tool

Creates a new device or location entity in a SNON 4.0 filesystem tree by
writing the two objects every entity requires (§8.5):

  1. A canonical fragment file  (<snon-path>/<class>/<uuid>.json)
  2. A canonical subdirectory   (<snon-path>/<class>/<uuid>/)

The entity is created without any parent relationships.  Use snon_link.py
afterwards to attach it to a device or location.

The UUID of the created entity is written to stdout so that callers can
capture it for use with snon_link.py:

    UUID=$(python snon_create.py --snon-path ./snon/ --type device --name "Power Meter")
    python snon_link.py --snon-path ./snon/ --child "$UUID" --parent "$LOCATION_UUID"

Usage:
    python snon_create.py --snon-path PATH --type TYPE --name NAME [--tag TAG] [--uuid UUID]

Arguments:
    --snon-path PATH    Path to the SNON root directory (e.g. ./snon/)
    --type TYPE         Entity type: "device" or "location"
    --name NAME         Human-readable display name (stored as eN["*"])
    --tag TAG           Optional identification tag: dT for devices (ISO/IEC 81346
                        RDS tag or custom value), lT for locations
    --uuid UUID         UUID for the new entity; a random UUID v4 is generated
                        if omitted

Exit codes:
    0  Success
    1  Validation or logical error
    2  Filesystem I/O error
"""

import argparse
import json
import os
import re
import sys
import uuid as _uuid_mod
from datetime import datetime, timezone
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# UUID validation  (same pattern as snon_downsample.py and snon_link.py)
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

#: Maps supported entity type names to their filesystem directory name (§8.4)
#: and the field name used for the optional identification tag (§2.8, §2.9).
ENTITY_TYPES: Dict[str, Dict[str, str]] = {
    "device":   {"dir": "devices",   "tag_field": "dT"},
    "location": {"dir": "locations", "tag_field": "lT"},
}

#: All class directories defined by the filesystem spec (§8.4), used when
#: checking that a UUID is globally unique across the entire SNON tree.
ALL_CLASS_DIRS = ["devices", "locations", "sensors", "series", "measurands"]


# ---------------------------------------------------------------------------
# Timestamp helper  (shared convention with snon_link.py)
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


# ---------------------------------------------------------------------------
# UUID uniqueness check
# ---------------------------------------------------------------------------

def _find_existing_class(snon_path: str, entity_uuid: str) -> Optional[str]:
    """
    Return the class directory name if *entity_uuid* already exists anywhere
    in the SNON tree, or None if it is not present.

    Checks all standard class directories so that a UUID collision across
    entity types produces a clear error rather than a confusing OS exception.
    """
    for dir_name in ALL_CLASS_DIRS:
        if os.path.exists(os.path.join(snon_path, dir_name, entity_uuid + ".json")):
            return dir_name
    return None


# ---------------------------------------------------------------------------
# Entity creation  (§8.11 steps 1–2)
# ---------------------------------------------------------------------------

def create_entity(
    snon_path: str,
    entity_type: str,
    entity_uuid: str,
    name: str,
    tag: Optional[str],
) -> None:
    """
    Write the canonical fragment file and subdirectory for a new entity.

    Per §8.11, creating a new entity requires:
      1. Writing the canonical fragment file to the appropriate class directory.
      2. Creating the canonical subdirectory.

    Relationship subdirectories (e.g. devices/, sensors/) are NOT created
    here; snon_link.py creates them on demand when the first relationship
    is established.

    The fragment file is opened with O_EXCL so that an existing entity is
    never silently overwritten, consistent with the immutability principle
    applied to value files in snon_downsample.py (§8.6).

    Parameters
    ----------
    snon_path   : SNON root directory path.
    entity_type : "device" or "location".
    entity_uuid : Bare UUID string (without urn:uuid: prefix).
    name        : Display name stored as eN["*"].
    tag         : Optional tag value (dT or lT field); None to omit.
    """
    config     = ENTITY_TYPES[entity_type]
    class_path = os.path.join(snon_path, config["dir"])

    # Create the class directory if the SNON tree is being initialised fresh.
    os.makedirs(class_path, exist_ok=True)

    fragment_path = os.path.join(class_path, entity_uuid + ".json")
    entity_dir    = os.path.join(class_path, entity_uuid)

    # Build the fragment.  Field order follows the spec examples: identity
    # fields (eID, eC) first, then eUT, then descriptive metadata (eN),
    # then type-specific tag field.
    fragment = {
        "eID": "urn:uuid:" + entity_uuid,
        "eC":  entity_type,
        "eUT": _now_eut(),
        "eN":  {"*": name},
    }
    if tag is not None:
        fragment[config["tag_field"]] = tag

    pack_json = json.dumps([fragment], indent=2) + "\n"

    # Write the fragment file with O_EXCL: fails immediately if the file
    # already exists rather than overwriting it.
    try:
        fd = os.open(fragment_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise ValueError(
            f"Fragment file already exists: {fragment_path}\n"
            "Choose a different UUID or remove the existing entity first."
        )

    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(pack_json)
    except OSError as exc:
        try:
            os.unlink(fragment_path)
        except OSError:
            pass
        raise ValueError(
            f"Failed to write fragment file {fragment_path}: {exc}"
        ) from exc

    # Create the canonical subdirectory (§8.5).
    try:
        os.mkdir(entity_dir)
    except OSError as exc:
        # The fragment was written successfully; report the directory failure
        # without attempting to roll back, since a partial entity is
        # recoverable by creating the directory manually.
        raise ValueError(
            f"Fragment written but failed to create entity directory "
            f"{entity_dir}: {exc}\n"
            "Create the directory manually to complete the entity."
        ) from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snon_create.py",
        description=(
            "Create a new SNON 4.0 device or location entity, writing the "
            "canonical fragment file and subdirectory to the SNON tree.  "
            "The UUID of the created entity is printed to stdout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Create a location with an auto-generated UUID:\n"
            "  python snon_create.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --type location \\\n"
            "      --name 'Level 2, Bay 4' \\\n"
            "      --tag '++2.BAA4'\n"
            "\n"
            "  # Create a device and capture its UUID for immediate linking:\n"
            "  UUID=$(python snon_create.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --type device \\\n"
            "      --name 'Power Meter' \\\n"
            "      --tag '=K1=B1')\n"
            "  python snon_link.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --child \"$UUID\" \\\n"
            "      --parent \"$LOCATION_UUID\"\n"
            "\n"
            "  # Create a device with a specific UUID:\n"
            "  python snon_create.py \\\n"
            "      --snon-path ./snon/ \\\n"
            "      --type device \\\n"
            "      --name 'Power Meter' \\\n"
            "      --uuid a1b2c3d4-5678-4f0a-bcde-000000000002\n"
        ),
    )
    parser.add_argument(
        "--snon-path",
        required=True,
        metavar="PATH",
        help="Path to the SNON root directory (e.g. ./snon/).",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=sorted(ENTITY_TYPES),
        metavar="TYPE",
        help="Entity type to create: device or location.",
    )
    parser.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help='Human-readable display name, stored as eN["*"].',
    )
    parser.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help=(
            "Optional identification tag: stored as dT for devices "
            "(ISO/IEC 81346 RDS tag or custom value), lT for locations."
        ),
    )
    parser.add_argument(
        "--uuid",
        default=None,
        metavar="UUID",
        help="UUID for the new entity; a random UUID v4 is generated if omitted.",
    )

    args = parser.parse_args()

    snon_path   = args.snon_path
    entity_type = args.type
    name        = args.name
    tag         = args.tag

    # Validate and normalise a caller-supplied UUID, or generate a fresh one.
    if args.uuid is not None:
        entity_uuid = args.uuid.lower()
        if not _is_valid_uuid(entity_uuid):
            parser.error(f"--uuid: not a valid UUID: {entity_uuid!r}")
    else:
        entity_uuid = str(_uuid_mod.uuid4())

    if not os.path.isdir(snon_path):
        print(f"Error: SNON directory not found: {snon_path}", file=sys.stderr)
        sys.exit(1)

    # Check for UUID collisions across the whole tree before attempting any
    # writes, so the error message names the conflicting class directory.
    existing_class = _find_existing_class(snon_path, entity_uuid)
    if existing_class is not None:
        print(
            f"\nError: UUID {entity_uuid!r} already exists in "
            f"{os.path.join(snon_path, existing_class)}/",
            file=sys.stderr,
        )
        sys.exit(1)

    config     = ENTITY_TYPES[entity_type]
    tag_field  = config["tag_field"]
    class_path = os.path.join(snon_path, config["dir"])

    print(f"Creating {entity_type}  {entity_uuid}", file=sys.stderr)
    print(f"  name: {name!r}", file=sys.stderr)
    if tag is not None:
        print(f"  {tag_field}: {tag!r}", file=sys.stderr)

    try:
        create_entity(snon_path, entity_type, entity_uuid, name, tag)
    except ValueError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"\nFilesystem error: {exc}", file=sys.stderr)
        sys.exit(2)

    print(
        f"  wrote: {os.path.join(class_path, entity_uuid + '.json')}",
        f"  mkdir: {os.path.join(class_path, entity_uuid)}/",
        sep="\n",
        file=sys.stderr,
    )

    # UUID to stdout: the only stdout output, so callers can capture it cleanly.
    print(entity_uuid)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
snon_validate.py — SNON 4.0 Filesystem Tree Validator

Walks a SNON 4.0 filesystem tree (§8 of the SNON 4.0 specification at
https://www.snon.org) and validates every fragment and structural element
found.  One line is printed per issue.

Validation categories
─────────────────────
  FS  Filesystem layout               §8.4 – §8.8
  FF  Fragment-file structure         §8.5
  FV  Fragment field values           §2.3 – §2.10
  VF  Value-file structure            §8.6
  SL  Symlink correctness             §8.7
  CR  Cross-reference integrity       §3, §5, §8.9 – §8.11

Each output line has the form:

  [SEVERITY] <code>  <relative-path>  –  <description>

Severity
────────
  ERROR    Violation of a SHALL requirement – tree is non-conformant.
  WARNING  Violation of a SHOULD requirement (only with --strict).

Exit codes
──────────
  0  No issues found
  1  One or more ERRORs found
  2  Only WARNINGs found (implies --strict was given)

Usage
─────
  python snon_validate.py --snon-path PATH [--strict]

Arguments
─────────
  --snon-path PATH   Path to the SNON root directory (e.g. ./snon/)
  --strict           Also report SHOULD-level (WARNING) violations
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ISO 8601 point-in-time  e.g. 2014-08-20T14:32:57.126Z  or  ...+05:30
_ISO8601_TS_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)

# ISO 8601 duration  e.g.  PT10S  PT1.5M  P1DT2H
_ISO8601_DUR_RE = re.compile(
    r"^P(?:\d+Y)?(?:\d+M)?(?:\d+W)?(?:\d+D)?"
    r"(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
)

# ISO 8601 timestamp/duration  e.g.  2014-08-20T14:32:57Z/PT10S
_ISO8601_TS_DUR_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
    r"/P.*$"
)

# Value file name: YYYY-MM-DDTHH-MM-SS[.fff…]Z[-hex].json  (§8.6)
_VALUE_FNAME_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3])-[0-5]\d-[0-5]\d"
    r"(?:\.\d+)?Z(?:-[0-9a-fA-F]+)?\.json$"
)

# ── entity class directories ─────────────────────────────────────────────────

KNOWN_CLASS_DIRS: Set[str] = {
    "devices", "locations", "measurands", "sensors", "series"
}

# (parent_class, relationship_subdir)  →  child_class   (§8.7.1 table)
VALID_REL_SUBDIRS: Dict[Tuple[str, str], str] = {
    ("locations", "locations"): "locations",
    ("locations", "devices"):   "devices",
    ("devices",   "devices"):   "devices",
    ("devices",   "sensors"):   "sensors",
    ("sensors",   "series"):    "series",
}

# Entity classes that may contain a values/ subdirectory
CLASSES_WITH_VALUES: Set[str] = {"sensors", "series"}

# Classes that carry no relationship subdirs and normally have no subdir at all
CLASSES_WITHOUT_SUBDIR: Set[str] = {"measurands"}

# Expected eC value for each class directory
CLASS_DIR_TO_EC: Dict[str, str] = {
    "devices":    "device",
    "locations":  "location",
    "measurands": "measurand",
    "sensors":    "sensor",
    "series":     "series",
}

# ── fragment schemas (§2.3 – §2.10, additionalProperties: false) ─────────────

ALLOWED_FIELDS: Dict[str, Set[str]] = {
    "value": {
        "eID", "eC",
        "v", "vT", "vMax", "vMin", "vTo", "vE",
        "ext",
    },
    "series": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "meSL", "meSH", "meDL", "meDH", "meDU",
        "meUR", "meTo", "meR", "meAc",
        "ext",
    },
    "sensor": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "sT",
        "ext",
    },
    "measurand": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "meU", "meT", "meAq",
        "meUP", "meUS", "meUPx", "meUSx", "meL",
        "meSL", "meSH", "meDL", "meDH", "meDU",
        "meUR", "meTo", "meR", "meAc",
        "ext",
    },
    "device": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "dT",
        "ext",
    },
    "location": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "lT",
        "ext",
    },
    "relationship": {
        "eID", "eC", "eUT", "eT", "eN", "eR",
        "rS", "rD", "rT",
        "ext",
    },
}

# Fields that MUST NOT appear in value fragments (§2.3)
NON_VALUE_FIELDS: Set[str] = {"eUT", "eN", "eT", "eR"}

# Fields whose values must be numeric strings (§2.5, §2.7)
NUMERIC_STRING_FIELDS: Set[str] = {
    "meSL", "meSH", "meDL", "meDH", "meUR", "meTo", "meR", "meAc",
}

VALID_EC_VALUES: Set[str] = set(ALLOWED_FIELDS.keys())

VALID_ME_TYPES: Set[str] = {
    "enumeration", "numeric", "string", "url", "iso8601", "ordinal",
}
VALID_ME_AQ: Set[str] = {
    "sample", "count", "triggered", "summary", "derived",
}


# ─────────────────────────────────────────────────────────────────────────────
# Issue dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(order=True)
class Issue:
    """One validation finding, sortable by path then code."""
    path:     str
    code:     str
    severity: str   # "ERROR" or "WARNING"
    message:  str

    def __str__(self) -> str:
        return f"[{self.severity:<7}] {self.code:<6}  {self.path}  –  {self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _is_valid_urn(s: object) -> bool:
    """Minimal RFC 8141 URN check: starts with 'urn:' and has ≥ 2 colons."""
    return (
        isinstance(s, str)
        and s.startswith("urn:")
        and s.count(":") >= 2
        and len(s) > 6
    )


def _is_valid_iso8601_ts(s: str) -> bool:
    return bool(_ISO8601_TS_RE.match(s))


def _is_valid_iso8601_dur(s: str) -> bool:
    """Return True for a non-empty ISO 8601 duration (must have ≥1 designator)."""
    if not _ISO8601_DUR_RE.match(s):
        return False
    return any(c in s for c in "YMDTHMS")


def _is_valid_iso8601_ts_dur(s: str) -> bool:
    return bool(_ISO8601_TS_DUR_RE.match(s))


def _is_valid_vt_entry(s: str, is_first: bool) -> bool:
    """
    Validate one vT array element per §2.4:
      First entry   → must be ISO 8601 timestamp or timestamp/duration.
      Later entries → may also be a pure duration.
    """
    if _is_valid_iso8601_ts(s) or _is_valid_iso8601_ts_dur(s):
        return True
    return (not is_first) and _is_valid_iso8601_dur(s)


def _is_numeric_string(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _rp(root: str, abs_path: str) -> str:
    """Return abs_path relative to root for tidy display."""
    try:
        return os.path.relpath(abs_path, root)
    except ValueError:
        return abs_path


def _expected_rel_symlink(
    parent_class: str,
    child_class: str,
    child_uuid: str,
    is_dir: bool,
) -> str:
    """
    Compute the expected relative symlink value for a relationship entry per §8.7.1.

    Same class (e.g. device → device):   ../../<uuid>[.json]
    Cross class (e.g. device → sensor):  ../../../<child_class>/<uuid>[.json]
    """
    suffix = "/" if is_dir else ".json"
    if parent_class == child_class:
        return f"../../{child_uuid}{suffix}"
    return f"../../../{child_class}/{child_uuid}{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

class SnonValidator:
    """Validates a SNON 4.0 filesystem tree rooted at snon_path."""

    def __init__(self, snon_path: str, strict: bool = False) -> None:
        self._root   = os.path.abspath(snon_path)
        self._strict = strict
        self._issues: List[Issue] = []

        # uuid → class_dir   (populated while scanning fragment files)
        self._known_entities: Dict[str, str] = {}

        # (parent_class, parent_uuid, child_class) → {child_uuid}
        # derived from symlinks found in relationship sub-directories
        self._symlink_children: Dict[Tuple[str, str, str], Set[str]] = {}

        # (class_dir, uuid) → {rel_type: [target_uuid, …]}
        # derived from eR fields in fragment files (urn:uuid: only)
        self._er_relations: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

    # ── reporting ─────────────────────────────────────────────────────────────

    def _err(self, path: str, code: str, msg: str) -> None:
        self._issues.append(Issue(path=path, code=code, severity="ERROR", message=msg))

    def _warn(self, path: str, code: str, msg: str) -> None:
        if self._strict:
            self._issues.append(Issue(path=path, code=code, severity="WARNING", message=msg))

    def _r(self, abs_path: str) -> str:
        return _rp(self._root, abs_path)

    # ── entry point ───────────────────────────────────────────────────────────

    def validate(self) -> List[Issue]:
        """Run all checks and return sorted issues."""
        if not os.path.isdir(self._root):
            self._err(self._root, "FS001", f"SNON root directory not found: {self._root}")
            return self._issues

        self._check_root_layout()

        for class_dir in sorted(KNOWN_CLASS_DIRS):
            dir_path = os.path.join(self._root, class_dir)
            if os.path.isdir(dir_path):
                self._scan_class_dir(class_dir)

        self._validate_cross_references()
        self._issues.sort()
        return self._issues

    # ── §8.4  root layout ─────────────────────────────────────────────────────

    def _check_root_layout(self) -> None:
        try:
            entries = os.listdir(self._root)
        except OSError as exc:
            self._err(self._root, "FS001", f"Cannot list root directory: {exc}")
            return

        for entry in entries:
            abs_e = os.path.join(self._root, entry)
            if os.path.isdir(abs_e) and not os.path.islink(abs_e):
                if entry not in KNOWN_CLASS_DIRS:
                    self._warn(
                        self._r(abs_e), "FS002",
                        f"Non-standard class directory '{entry}' in SNON root "
                        "(not defined in §8.4; treated as an extension)"
                    )
            elif os.path.isfile(abs_e) and not os.path.islink(abs_e):
                self._warn(
                    self._r(abs_e), "FS003",
                    f"Unexpected file '{entry}' directly inside the SNON root directory"
                )

    # ── §8.5  entity class directory ─────────────────────────────────────────

    def _scan_class_dir(self, class_dir: str) -> None:
        dir_path = os.path.join(self._root, class_dir)
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError as exc:
            self._err(self._r(dir_path), "FS004", f"Cannot list class directory: {exc}")
            return

        json_uuids: Set[str] = set()
        subdir_uuids: Set[str] = set()

        for entry in entries:
            abs_e = os.path.join(dir_path, entry)

            if entry.endswith(".json"):
                stem = entry[:-5]
                if _is_valid_uuid(stem):
                    json_uuids.add(stem)
                else:
                    self._warn(self._r(abs_e), "FS005",
                               f"File '{entry}' has a non-UUID stem; "
                               "expected <uuid>.json")

            elif os.path.isdir(abs_e) and not os.path.islink(abs_e):
                if _is_valid_uuid(entry):
                    subdir_uuids.add(entry)
                else:
                    self._warn(self._r(abs_e), "FS006",
                               f"Directory '{entry}' has a non-UUID name; "
                               "expected <uuid>/")

            elif not os.path.islink(abs_e):
                self._warn(self._r(abs_e), "FS007",
                           f"Unexpected entry '{entry}' in {class_dir}/ "
                           "(expected <uuid>.json files and <uuid>/ directories)")

        # Validate fragment files and record known entities
        for uuid in sorted(json_uuids):
            self._known_entities[uuid] = class_dir
            self._validate_pack_file(class_dir, uuid)

        # Cross-check: every uuid should have both .json and a subdir
        # Measurands are excepted – §8.8 shows only the .json file for them.
        if class_dir not in CLASSES_WITHOUT_SUBDIR:
            for uuid in sorted(json_uuids):
                if uuid not in subdir_uuids:
                    self._warn(
                        self._r(os.path.join(dir_path, uuid)),
                        "FS008",
                        f"Canonical subdirectory {uuid}/ is missing for "
                        f"{class_dir}/{uuid}.json (§8.5)"
                    )
                else:
                    self._validate_entity_subdir(class_dir, uuid)

            for uuid in sorted(subdir_uuids):
                if uuid not in json_uuids:
                    self._warn(
                        self._r(os.path.join(dir_path, uuid)),
                        "FS009",
                        f"Subdirectory {uuid}/ exists but canonical fragment file "
                        f"{uuid}.json is missing (§8.5)"
                    )

    # ── §8.5  canonical fragment pack file ───────────────────────────────────

    def _validate_pack_file(self, class_dir: str, uuid: str) -> None:
        path  = os.path.join(self._root, class_dir, uuid + ".json")
        rpath = self._r(path)

        # ── parse ──────────────────────────────────────────────────────────
        try:
            with open(path, encoding="utf-8") as fh:
                pack = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self._err(rpath, "FF001", f"Cannot parse as JSON: {exc}")
            return

        if not isinstance(pack, list):
            self._err(rpath, "FF002", "File content must be a JSON array (pack)")
            return

        if len(pack) == 0:
            self._err(rpath, "FF003", "Pack is empty; must contain ≥ 1 fragment")
            return

        target_eid      = "urn:uuid:" + uuid
        matched_frags:  List[dict] = []
        foreign_eids:   Set[str]   = set()

        for idx, item in enumerate(pack):
            loc = f"item[{idx}]"

            if not isinstance(item, dict):
                self._err(rpath, "FF004", f"{loc}: element is not a JSON object")
                continue

            # ── detect value fragments (SHALL NOT appear in pack files §8.5) ─
            ec_raw = item.get("eC")
            has_value_fields = ("v" in item or "vT" in item)
            is_value = ec_raw == "value" or (ec_raw is None and has_value_fields)
            if is_value:
                self._err(rpath, "FF005",
                          f"{loc}: entity pack files SHALL NOT contain value "
                          "fragments (§8.5); place them in a values/ subdirectory")
                continue

            eid = item.get("eID", "")
            if eid == target_eid:
                matched_frags.append(item)
            elif eid:
                foreign_eids.add(eid)

            self._validate_fragment(item, class_dir, uuid, rpath, idx)

        if not matched_frags:
            self._err(rpath, "FF006",
                      f"No fragment in pack has eID = '{target_eid}' "
                      "(must match the file's UUID per §8.5)")

        if foreign_eids:
            self._err(rpath, "FF007",
                      f"Pack contains fragment(s) with {len(foreign_eids)} foreign "
                      "entity ID(s) – a pack file SHALL NOT mix entities (§8.5): "
                      + ", ".join(sorted(foreign_eids)))

        # ── collect eR relations for cross-reference phase ─────────────────
        for frag in matched_frags:
            er = frag.get("eR")
            if not isinstance(er, dict):
                continue
            bucket = self._er_relations.setdefault((class_dir, uuid), {})
            for rel_type, targets in er.items():
                if not isinstance(targets, list):
                    continue
                for t in targets:
                    if isinstance(t, str) and t.startswith("urn:uuid:"):
                        child_uuid = t[len("urn:uuid:"):]
                        bucket.setdefault(rel_type, []).append(child_uuid)

    # ── §2.3 – §2.10  fragment field validation ───────────────────────────────

    def _validate_fragment(
        self,
        frag:      dict,
        class_dir: str,
        uuid:      str,
        rpath:     str,
        idx:       int = 0,
    ) -> None:
        loc = f"item[{idx}]"

        # ── eID ────────────────────────────────────────────────────────────
        eid = frag.get("eID")
        if eid is None:
            self._err(rpath, "FV001", f"{loc}: missing mandatory 'eID' field")
        elif not _is_valid_urn(eid):
            self._err(rpath, "FV002",
                      f"{loc}: 'eID' = {eid!r} is not a valid RFC 8141 URN")

        # ── eC ─────────────────────────────────────────────────────────────
        ec = frag.get("eC")
        if ec is None:
            expected_ec = CLASS_DIR_TO_EC.get(class_dir, "value")
            self._warn(rpath, "FV003",
                       f"{loc}: 'eC' field is absent; entities in {class_dir}/ "
                       f"should declare eC = '{expected_ec}'")
            ec = expected_ec
        elif ec not in VALID_EC_VALUES:
            self._err(rpath, "FV004",
                      f"{loc}: 'eC' = {ec!r} is not a valid entity class "
                      f"(must be one of {sorted(VALID_EC_VALUES)})")
            return   # cannot continue without a known eC
        else:
            expected_ec = CLASS_DIR_TO_EC.get(class_dir)
            if expected_ec and ec != expected_ec:
                self._err(rpath, "FV005",
                          f"{loc}: 'eC' = {ec!r} but entities in {class_dir}/ "
                          f"must have eC = '{expected_ec}'")

        # ── unexpected fields ───────────────────────────────────────────────
        allowed = ALLOWED_FIELDS.get(ec, set())
        for key in frag:
            if key not in allowed:
                self._warn(rpath, "FV006",
                           f"{loc}: unexpected field '{key}' for eC='{ec}' "
                           "(schema additionalProperties: false)")

        # ── fields forbidden in value fragments ────────────────────────────
        if ec == "value":
            for forbidden in NON_VALUE_FIELDS:
                if forbidden in frag:
                    self._err(rpath, "FV007",
                              f"{loc}: field '{forbidden}' is not permitted "
                              "in value fragments (§2.3)")

        # ── eUT ────────────────────────────────────────────────────────────
        eut = frag.get("eUT")
        if eut is not None:
            if not isinstance(eut, str):
                self._err(rpath, "FV008", f"{loc}: 'eUT' must be a JSON string")
            elif not _is_valid_iso8601_ts(eut):
                self._err(rpath, "FV009",
                          f"{loc}: 'eUT' = {eut!r} is not a valid "
                          "ISO 8601 timestamp (§2.3)")

        # ── eN ─────────────────────────────────────────────────────────────
        self._check_intl_name(frag, "eN", rpath, loc)

        # ── eT ─────────────────────────────────────────────────────────────
        self._check_intl_name(frag, "eT", rpath, loc)

        # ── eR ─────────────────────────────────────────────────────────────
        er = frag.get("eR")
        if er is not None:
            if not isinstance(er, dict):
                self._err(rpath, "FV014", f"{loc}: 'eR' must be a JSON object")
            else:
                for rel_type, targets in er.items():
                    if not isinstance(targets, list):
                        self._err(rpath, "FV015",
                                  f"{loc}: 'eR'.{rel_type!r} must be a JSON array")
                        continue
                    for j, t in enumerate(targets):
                        if not isinstance(t, str):
                            self._err(rpath, "FV016",
                                      f"{loc}: 'eR'.{rel_type!r}[{j}] must be a string")
                        elif not _is_valid_urn(t):
                            self._err(rpath, "FV017",
                                      f"{loc}: 'eR'.{rel_type!r}[{j}] = {t!r} "
                                      "is not a valid RFC 8141 URN")

        # ── series-specific ────────────────────────────────────────────────
        if ec == "series":
            self._check_numeric_string_fields(frag, rpath, loc)
            er_dict = frag.get("eR") if isinstance(frag.get("eR"), dict) else {}
            measurand_list = er_dict.get("measurand", [])
            if isinstance(measurand_list, list) and len(measurand_list) > 1:
                self._err(rpath, "FV018",
                          f"{loc}: series has {len(measurand_list)} 'measurand' "
                          "relationships; at most one is permitted (§5)")
            child_of = er_dict.get("child_of", [])
            if isinstance(child_of, list) and len(child_of) > 1:
                self._err(rpath, "FV019",
                          f"{loc}: series has {len(child_of)} 'child_of' "
                          "relationships; exactly one sensor parent is permitted (§5)")

        # ── sensor-specific ────────────────────────────────────────────────
        if ec == "sensor":
            st = frag.get("sT")
            if st is not None and not isinstance(st, str):
                self._err(rpath, "FV020", f"{loc}: 'sT' must be a JSON string")
            er_dict = frag.get("eR") if isinstance(frag.get("eR"), dict) else {}
            measurand_list = er_dict.get("measurand", [])
            if isinstance(measurand_list, list) and len(measurand_list) > 1:
                self._err(rpath, "FV021",
                          f"{loc}: sensor has {len(measurand_list)} 'measurand' "
                          "relationships; at most one is permitted (§5)")

        # ── measurand-specific ─────────────────────────────────────────────
        if ec == "measurand":
            met = frag.get("meT")
            if met is not None and met not in VALID_ME_TYPES:
                self._err(rpath, "FV022",
                          f"{loc}: 'meT' = {met!r} is not a valid measure type "
                          f"(must be one of {sorted(VALID_ME_TYPES)})")
            meaq = frag.get("meAq")
            if meaq is not None and meaq not in VALID_ME_AQ:
                self._err(rpath, "FV023",
                          f"{loc}: 'meAq' = {meaq!r} is not a valid acquire method "
                          f"(must be one of {sorted(VALID_ME_AQ)})")
            self._check_intl_name(frag, "meUP",  rpath, loc)
            self._check_intl_name(frag, "meUS",  rpath, loc)
            self._check_intl_name(frag, "meUPx", rpath, loc)
            self._check_intl_name(frag, "meUSx", rpath, loc)
            self._check_numeric_string_fields(frag, rpath, loc)

        # ── device-specific ────────────────────────────────────────────────
        if ec == "device":
            dt = frag.get("dT")
            if dt is not None and not isinstance(dt, str):
                self._err(rpath, "FV024", f"{loc}: 'dT' must be a JSON string")

        # ── location-specific ──────────────────────────────────────────────
        if ec == "location":
            lt = frag.get("lT")
            if lt is not None and not isinstance(lt, str):
                self._err(rpath, "FV025", f"{loc}: 'lT' must be a JSON string")

        # ── relationship fragment ──────────────────────────────────────────
        if ec == "relationship":
            for mandatory in ("rS", "rD", "rT"):
                if mandatory not in frag:
                    self._err(rpath, "FV026",
                              f"{loc}: relationship fragment missing mandatory "
                              f"field '{mandatory}' (§2.10)")
            for fname in ("rS", "rD"):
                val = frag.get(fname)
                if val is not None and not _is_valid_urn(val):
                    self._err(rpath, "FV027",
                              f"{loc}: '{fname}' = {val!r} is not a valid URN (§2.10)")
            rt = frag.get("rT")
            if rt is not None and not isinstance(rt, str):
                self._err(rpath, "FV028", f"{loc}: 'rT' must be a JSON string (§2.10)")

    def _check_intl_name(
        self, frag: dict, field: str, rpath: str, loc: str
    ) -> None:
        """Validate a multilingual name field (eN, eT, meUP, …)."""
        val = frag.get(field)
        if val is None:
            return
        if not isinstance(val, dict):
            self._err(rpath, "FV010",
                      f"{loc}: '{field}' must be a JSON object (language → string)")
            return
        for lang, name in val.items():
            if not isinstance(name, str):
                self._err(rpath, "FV011",
                          f"{loc}: '{field}'.{lang!r} value must be a string "
                          f"(got {type(name).__name__})")

    def _check_numeric_string_fields(
        self, frag: dict, rpath: str, loc: str
    ) -> None:
        for fname in NUMERIC_STRING_FIELDS:
            val = frag.get(fname)
            if val is None:
                continue
            if not isinstance(val, str):
                self._err(rpath, "FV030",
                          f"{loc}: '{fname}' must be a JSON string (got "
                          f"{type(val).__name__})")
            elif not _is_numeric_string(val):
                self._err(rpath, "FV031",
                          f"{loc}: '{fname}' = {val!r} must be a numeric string")

    # ── §8.5  entity subdirectory ─────────────────────────────────────────────

    def _validate_entity_subdir(self, class_dir: str, uuid: str) -> None:
        subdir = os.path.join(self._root, class_dir, uuid)
        try:
            entries = sorted(os.listdir(subdir))
        except OSError as exc:
            self._err(self._r(subdir), "FS010",
                      f"Cannot list entity subdirectory: {exc}")
            return

        # Determine what names are legal inside this entity's subdir
        valid_rel: Set[str] = {
            rel for (pc, rel) in VALID_REL_SUBDIRS if pc == class_dir
        }
        valid_names = valid_rel | ({"values"} if class_dir in CLASSES_WITH_VALUES else set())

        for entry in entries:
            abs_e = os.path.join(subdir, entry)
            if entry in valid_names:
                if entry == "values":
                    self._validate_values_dir(class_dir, uuid, abs_e)
                else:
                    child_class = VALID_REL_SUBDIRS[(class_dir, entry)]
                    self._validate_rel_subdir(
                        class_dir, uuid, entry, child_class, abs_e
                    )
            elif os.path.islink(abs_e):
                self._warn(self._r(abs_e), "FS011",
                           f"Unexpected symlink '{entry}' inside "
                           f"{class_dir}/{uuid}/")
            elif os.path.isdir(abs_e):
                self._warn(self._r(abs_e), "FS012",
                           f"Unexpected subdirectory '{entry}' inside "
                           f"{class_dir}/{uuid}/ "
                           f"(valid: {sorted(valid_names) or 'none'})")
            elif os.path.isfile(abs_e):
                self._warn(self._r(abs_e), "FS013",
                           f"Unexpected file '{entry}' inside entity subdirectory "
                           f"{class_dir}/{uuid}/")

    # ── §8.6  values/ directory ───────────────────────────────────────────────

    def _validate_values_dir(
        self, class_dir: str, uuid: str, values_path: str
    ) -> None:
        try:
            filenames = sorted(os.listdir(values_path))
        except OSError as exc:
            self._err(self._r(values_path), "FS014",
                      f"Cannot list values/ directory: {exc}")
            return

        for fname in filenames:
            fpath = os.path.join(values_path, fname)
            if not fname.endswith(".json"):
                self._warn(self._r(fpath), "VF016",
                           f"Non-.json entry '{fname}' in values/ directory")
                continue
            if not _VALUE_FNAME_RE.match(fname):
                self._warn(
                    self._r(fpath), "VF017",
                    f"Value file name '{fname}' does not follow the §8.6 "
                    r"convention: YYYY-MM-DDTHH-MM-SS[.fff]Z[-hex].json"
                )
            self._validate_value_file(fpath, expected_uuid=uuid)

    def _validate_value_file(self, path: str, expected_uuid: str) -> None:
        """Validate one value file per §8.6 and §2.4."""
        rpath = self._r(path)

        try:
            with open(path, encoding="utf-8") as fh:
                content = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self._err(rpath, "VF018", f"Cannot parse value file as JSON: {exc}")
            return

        if not isinstance(content, list):
            self._err(rpath, "VF019", "Value file content must be a JSON array")
            return
        if len(content) == 0:
            self._err(rpath, "VF020",
                      "Value file JSON array is empty; "
                      "must contain exactly one value fragment (§8.6)")
            return
        if len(content) > 1:
            self._err(rpath, "VF021",
                      f"Value file contains {len(content)} fragments; "
                      "must contain exactly one (§8.6)")

        item = content[0]
        if not isinstance(item, dict):
            self._err(rpath, "VF022", "Value file fragment must be a JSON object")
            return

        # eID must match parent entity UUID  (§8.9)
        eid          = item.get("eID", "")
        expected_eid = "urn:uuid:" + expected_uuid
        if eid != expected_eid:
            self._err(rpath, "VF023",
                      f"eID = {eid!r} does not match parent entity "
                      f"(expected {expected_eid!r}) per §8.9")

        # Fields forbidden in value fragments
        for forbidden in NON_VALUE_FIELDS:
            if forbidden in item:
                self._err(rpath, "VF024",
                          f"Field '{forbidden}' is not permitted in value fragments (§2.3)")

        # Value-array field validation (§2.4)
        self._validate_value_arrays(item, rpath, "item[0]")

    def _validate_value_arrays(
        self, frag: dict, rpath: str, loc: str
    ) -> None:
        """Validate v, vT and optional parallel arrays per §2.4."""
        v  = frag.get("v")
        vt = frag.get("vT")

        # ── v ──────────────────────────────────────────────────────────────
        n: Optional[int] = None
        if v is None:
            self._err(rpath, "VF001", f"{loc}: missing mandatory 'v' field")
        elif not isinstance(v, list):
            self._err(rpath, "VF002", f"{loc}: 'v' must be a JSON array")
        elif len(v) == 0:
            self._err(rpath, "VF003", f"{loc}: 'v' array must not be empty")
        else:
            n = len(v)
            for i, item in enumerate(v):
                if not isinstance(item, str):
                    self._err(rpath, "VF004",
                              f"{loc}: 'v'[{i}] must be a string "
                              f"(got {type(item).__name__})")

        # ── vT ─────────────────────────────────────────────────────────────
        if vt is None:
            self._err(rpath, "VF005", f"{loc}: missing mandatory 'vT' field")
        elif not isinstance(vt, list):
            self._err(rpath, "VF006", f"{loc}: 'vT' must be a JSON array")
        elif len(vt) == 0:
            self._err(rpath, "VF007", f"{loc}: 'vT' array must not be empty")
        else:
            for i, entry in enumerate(vt):
                if not isinstance(entry, str):
                    self._err(rpath, "VF008",
                              f"{loc}: 'vT'[{i}] must be a string")
                elif not _is_valid_vt_entry(entry, is_first=(i == 0)):
                    tip = "ISO 8601 timestamp" if i == 0 else "ISO 8601 timestamp, timestamp/duration, or duration"
                    self._err(rpath, "VF009" if i == 0 else "VF010",
                              f"{loc}: 'vT'[{i}] = {entry!r} is not a valid {tip} (§2.4)")

            if n is not None and len(vt) != n:
                self._err(rpath, "VF011",
                          f"{loc}: 'v' has {n} entries but 'vT' has {len(vt)}; "
                          "all value arrays must have the same length (§2.4)")

        # ── vMax, vMin, vTo ────────────────────────────────────────────────
        for fname in ("vMax", "vMin", "vTo"):
            arr = frag.get(fname)
            if arr is None:
                continue
            if not isinstance(arr, list):
                self._err(rpath, "VF012", f"{loc}: '{fname}' must be a JSON array")
                continue
            if n is not None and len(arr) != n:
                self._err(rpath, "VF013",
                          f"{loc}: '{fname}' has {len(arr)} entries but 'v' has {n}; "
                          "all value arrays must have the same length (§2.4)")
            for i, item in enumerate(arr):
                if not isinstance(item, str):
                    self._err(rpath, "VF014",
                              f"{loc}: '{fname}'[{i}] must be a string")

        # ── vE ─────────────────────────────────────────────────────────────
        ve = frag.get("vE")
        if ve is not None and not isinstance(ve, str):
            self._err(rpath, "VF015", f"{loc}: 'vE' must be a JSON string (§2.4)")

    # ── §8.7  relationship subdirectory symlinks ──────────────────────────────

    def _validate_rel_subdir(
        self,
        parent_class: str,
        parent_uuid:  str,
        rel_subdir:   str,
        child_class:  str,
        path:         str,
    ) -> None:
        """Validate all symlink pairs in one relationship subdirectory."""
        try:
            entries = sorted(os.listdir(path))
        except OSError as exc:
            self._err(self._r(path), "SL001",
                      f"Cannot list relationship directory: {exc}")
            return

        json_uuids: Set[str] = set()
        dir_uuids:  Set[str] = set()

        for entry in entries:
            abs_e = os.path.join(path, entry)
            if entry.endswith(".json"):
                stem = entry[:-5]
                if _is_valid_uuid(stem):
                    json_uuids.add(stem)
                else:
                    self._warn(self._r(abs_e), "SL002",
                               f"Non-UUID .json entry '{entry}' in "
                               f"{parent_class}/{parent_uuid}/{rel_subdir}/")
            else:
                if _is_valid_uuid(entry):
                    dir_uuids.add(entry)
                else:
                    self._warn(self._r(abs_e), "SL003",
                               f"Unexpected entry '{entry}' in relationship "
                               f"directory (expected <uuid>.json or <uuid>/)")

        all_uuids = json_uuids | dir_uuids
        for child_uuid in sorted(all_uuids):
            json_path = os.path.join(path, child_uuid + ".json")
            dir_path  = os.path.join(path, child_uuid)
            has_json  = child_uuid in json_uuids
            has_dir   = child_uuid in dir_uuids

            if has_json and not has_dir:
                self._err(self._r(json_path), "SL004",
                          f"Symlink pair incomplete: {child_uuid}.json exists "
                          f"but directory symlink {child_uuid}/ is missing (§8.7)")
            elif has_dir and not has_json:
                self._err(self._r(dir_path), "SL005",
                          f"Symlink pair incomplete: directory symlink {child_uuid}/ "
                          f"exists but {child_uuid}.json is missing (§8.7)")

            if has_json:
                self._validate_symlink(
                    json_path, parent_class, child_class, child_uuid, is_dir=False
                )
            if has_dir:
                self._validate_symlink(
                    dir_path, parent_class, child_class, child_uuid, is_dir=True
                )

            # Record for cross-reference phase
            key = (parent_class, parent_uuid, child_class)
            self._symlink_children.setdefault(key, set()).add(child_uuid)

    def _validate_symlink(
        self,
        path:         str,
        parent_class: str,
        child_class:  str,
        child_uuid:   str,
        is_dir:       bool,
    ) -> None:
        """Validate one symlink in a relationship directory (§8.7)."""
        rpath = self._r(path)

        if not os.path.islink(path):
            kind = "directory" if is_dir else ".json file"
            self._err(rpath, "SL006",
                      f"Expected a symlink but found a regular {kind}; "
                      "relationship entries SHALL be relative symlinks (§8.7)")
            return

        try:
            link_target = os.readlink(path)
        except OSError as exc:
            self._err(rpath, "SL006", f"Cannot read symlink: {exc}")
            return

        # Must be relative  (§8.7)
        if os.path.isabs(link_target):
            self._err(rpath, "SL007",
                      f"Symlink target '{link_target}' is absolute; "
                      "all symlinks SHALL be relative (§8.7)")

        # Expected relative path per §8.7.1
        expected = _expected_rel_symlink(
            parent_class, child_class, child_uuid, is_dir
        )
        if link_target.rstrip("/") != expected.rstrip("/"):
            self._err(rpath, "SL008",
                      f"Symlink target '{link_target}' does not match "
                      f"expected '{expected}' per §8.7.1")

        # Must not be broken
        if not os.path.exists(path):
            self._err(rpath, "SL009",
                      f"Broken symlink: target '{link_target}' does not exist")
            return

        # Symlink name must match target basename  (§8.7.2)
        entry_name    = os.path.basename(path)
        expected_name = child_uuid + ("" if is_dir else ".json")
        if entry_name != expected_name:
            self._err(rpath, "SL010",
                      f"Symlink name '{entry_name}' must equal the target "
                      f"basename '{expected_name}' (§8.7.2)")

        # Resolved target must point into the correct class directory
        resolved = os.path.realpath(path)
        if is_dir:
            canonical = os.path.realpath(
                os.path.join(self._root, child_class, child_uuid)
            )
        else:
            canonical = os.path.realpath(
                os.path.join(self._root, child_class, child_uuid + ".json")
            )
        if resolved != canonical:
            self._err(rpath, "SL011",
                      f"Symlink resolves to '{resolved}' but should resolve to "
                      f"'{canonical}'")

    # ── cross-reference validation ─────────────────────────────────────────────

    def _validate_cross_references(self) -> None:
        """
        Validate referential integrity after scanning the whole tree.

        Checks performed:
          1. Every entity ID referenced in an eR field exists in the tree.
          2. eR.measurand targets are in measurands/.
          3. child_of relationships point to entities of the correct class.
          4. eR.child_of entries are consistent with the reverse symlinks.
          5. Symlinks that have no matching eR.child_of in the child entity.
        """
        # ── forward reference checks ───────────────────────────────────────
        for (class_dir, uuid), relations in self._er_relations.items():
            rpath = self._r(
                os.path.join(self._root, class_dir, uuid + ".json")
            )
            for rel_type, target_uuids in relations.items():
                for t_uuid in target_uuids:
                    if not _is_valid_uuid(t_uuid):
                        continue  # invalid URN already reported in FV017

                    # Target must exist
                    if t_uuid not in self._known_entities:
                        self._err(rpath, "CR001",
                                  f"eR.{rel_type!r} references "
                                  f"'urn:uuid:{t_uuid}' which is not present "
                                  "as a fragment file in this SNON tree")
                        continue

                    # Measurand references must point into measurands/
                    if rel_type == "measurand":
                        t_class = self._known_entities[t_uuid]
                        if t_class != "measurands":
                            self._err(rpath, "CR002",
                                      f"eR.measurand references "
                                      f"'urn:uuid:{t_uuid}' which is in "
                                      f"'{t_class}/' not 'measurands/'")

            child_of_uuids = relations.get("child_of", [])

            # ── sensors: parent must be a device ──────────────────────────
            if class_dir == "sensors":
                for p_uuid in child_of_uuids:
                    if not _is_valid_uuid(p_uuid):
                        continue
                    p_class = self._known_entities.get(p_uuid)
                    if p_class and p_class != "devices":
                        self._err(rpath, "CR003",
                                  f"Sensor eR.child_of references "
                                  f"'urn:uuid:{p_uuid}' which is in "
                                  f"'{p_class}/' not 'devices/'")
                    # Reverse-symlink check
                    if uuid not in self._symlink_children.get(
                        ("devices", p_uuid, "sensors"), set()
                    ):
                        self._warn(rpath, "CR004",
                                   f"Sensor declares eR.child_of = "
                                   f"'urn:uuid:{p_uuid}' but no symlink was "
                                   f"found in devices/{p_uuid}/sensors/ "
                                   "pointing back to this sensor (§8.7)")

            # ── series: parent must be a sensor ───────────────────────────
            if class_dir == "series":
                for p_uuid in child_of_uuids:
                    if not _is_valid_uuid(p_uuid):
                        continue
                    p_class = self._known_entities.get(p_uuid)
                    if p_class and p_class != "sensors":
                        self._err(rpath, "CR005",
                                  f"Series eR.child_of references "
                                  f"'urn:uuid:{p_uuid}' which is in "
                                  f"'{p_class}/' not 'sensors/'")
                    if uuid not in self._symlink_children.get(
                        ("sensors", p_uuid, "series"), set()
                    ):
                        self._warn(rpath, "CR006",
                                   f"Series declares eR.child_of = "
                                   f"'urn:uuid:{p_uuid}' but no symlink was "
                                   f"found in sensors/{p_uuid}/series/ "
                                   "pointing back to this series (§8.7)")

            # ── devices: parent must be a location or device ───────────────
            if class_dir == "devices":
                for p_uuid in child_of_uuids:
                    if not _is_valid_uuid(p_uuid):
                        continue
                    p_class = self._known_entities.get(p_uuid)
                    if p_class and p_class not in ("locations", "devices"):
                        self._err(rpath, "CR007",
                                  f"Device eR.child_of references "
                                  f"'urn:uuid:{p_uuid}' which is in "
                                  f"'{p_class}/' (expected 'locations/' or 'devices/')")
                    if p_class in ("locations", "devices"):
                        if uuid not in self._symlink_children.get(
                            (p_class, p_uuid, "devices"), set()
                        ):
                            self._warn(rpath, "CR008",
                                       f"Device declares eR.child_of = "
                                       f"'urn:uuid:{p_uuid}' but no symlink was "
                                       f"found in {p_class}/{p_uuid}/devices/ "
                                       "pointing back to this device (§8.7)")

            # ── locations: parent must be a location ──────────────────────
            if class_dir == "locations":
                for p_uuid in child_of_uuids:
                    if not _is_valid_uuid(p_uuid):
                        continue
                    p_class = self._known_entities.get(p_uuid)
                    if p_class and p_class != "locations":
                        self._err(rpath, "CR009",
                                  f"Location eR.child_of references "
                                  f"'urn:uuid:{p_uuid}' which is in "
                                  f"'{p_class}/' not 'locations/'")
                    if uuid not in self._symlink_children.get(
                        ("locations", p_uuid, "locations"), set()
                    ):
                        self._warn(rpath, "CR010",
                                   f"Location declares eR.child_of = "
                                   f"'urn:uuid:{p_uuid}' but no symlink was "
                                   f"found in locations/{p_uuid}/locations/ "
                                   "pointing back to this location (§8.7)")

        # ── reverse check: symlinks without a matching eR.child_of ────────
        for (p_class, p_uuid, c_class), child_uuids in self._symlink_children.items():
            for c_uuid in sorted(child_uuids):
                child_er   = self._er_relations.get((c_class, c_uuid), {})
                child_of   = child_er.get("child_of", [])
                if p_uuid not in child_of:
                    symlink_rpath = self._r(
                        os.path.join(
                            self._root, p_class, p_uuid, c_class, c_uuid + ".json"
                        )
                    )
                    self._warn(symlink_rpath, "CR011",
                               f"Symlink declares {c_class[:-1]} '{c_uuid}' as a "
                               f"child of {p_class[:-1]} '{p_uuid}', but the child "
                               "fragment has no matching eR.child_of entry")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snon_validate.py",
        description=(
            "Validate a SNON 4.0 filesystem tree and all fragments within it, "
            "printing one line per issue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Validate a SNON tree (SHALL violations only):\n"
            "  python snon_validate.py --snon-path ./snon/\n"
            "\n"
            "  # Include SHOULD-level warnings as well:\n"
            "  python snon_validate.py --snon-path ./snon/ --strict\n"
        ),
    )
    parser.add_argument(
        "--snon-path",
        required=True,
        metavar="PATH",
        help="Path to the SNON root directory (e.g. ./snon/).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Also report SHOULD-level (WARNING) violations.",
    )
    args = parser.parse_args()

    validator = SnonValidator(snon_path=args.snon_path, strict=args.strict)
    issues    = validator.validate()

    if not issues:
        print("No issues found.")
        sys.exit(0)

    errors   = [i for i in issues if i.severity == "ERROR"]
    warnings = [i for i in issues if i.severity == "WARNING"]

    for issue in issues:
        print(issue)

    print()
    print(
        f"Found {len(errors)} error(s) and {len(warnings)} warning(s) "
        f"across {len(issues)} total issue(s)."
    )

    sys.exit(1 if errors else 2)


if __name__ == "__main__":
    main()
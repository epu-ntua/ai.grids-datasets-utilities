#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
00_xiidm_sanity.py

Sanity/verification pass for an IIDM (XIIdm) XML file.
Outputs:
  - <out>.json : machine-readable report
  - <out>.txt  : human-readable summary + sample tables

Checks performed:
  - XML parse + IIDM namespace/schema version detection
  - element counts (all tags) + namespace declaration counts
  - reference resolution checks:
      * voltageLevelId / voltageLevelId1/2 -> voltageLevel ids
      * substationId -> substation ids
      * terminalRef connectableId (if present) -> known connectable ids
  - contextual containment checks:
      * selected equipment should appear under a voltageLevel in the XML nesting
  - numeric parsing checks for common electrical fields (no NaN/inf, parseable floats)

Designed for large files (streaming iterparse).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

try:
    from .common import (
        iso_now,
        localname,
        ns_uri,
        sha256_file,
    )
    from .common import (
        safe_float_strict as safe_float,
    )
    from .common import (
        safe_int_strict as safe_int,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import (  # type: ignore
        iso_now,
        localname,
        ns_uri,
        sha256_file,
    )
    from common import (
        safe_float_strict as safe_float,
    )
    from common import (
        safe_int_strict as safe_int,
    )


# ---------- helpers ----------


def short(s: str, n: int = 120) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 3] + "..."


def tsv(rows: List[Dict[str, Any]], cols: List[str], max_rows: int = 15) -> str:
    out = []
    out.append("\t".join(cols))
    for r in rows[:max_rows]:
        out.append("\t".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
    return "\n".join(out)


# ---------- main sanity pass ----------


def run_sanity(
    xiidm_path: str,
    out_prefix: str,
    samples_per_tag: int = 15,
    max_issue_examples: int = 50,
) -> None:
    if not os.path.exists(xiidm_path):
        raise FileNotFoundError(xiidm_path)

    file_size = os.path.getsize(xiidm_path)
    file_hash = sha256_file(xiidm_path)

    # Streaming parser
    # start-ns yields (prefix, uri)
    context = ET.iterparse(xiidm_path, events=("start", "end", "start-ns"))

    # Counts / meta
    ns_decl_counts = collections.Counter()  # uri -> count
    tag_counts = collections.Counter()  # local tag -> count

    iidm_ns: Optional[str] = None
    iidm_schema_version: Optional[str] = None
    network_meta: Dict[str, Any] = {}

    # ID sets
    substation_ids = set()
    voltagelevel_ids = set()

    # Connectables: things that may be referenced by terminalRef.connectableId
    connectable_ids = set()

    # Reference list we will validate later
    refs_substation: List[
        Tuple[str, str, str, str]
    ] = []  # (tag, elem_id, attr, ref_id)
    refs_voltagelevel: List[Tuple[str, str, str, str]] = []
    terminalref_connectable: List[
        Tuple[str, str, str]
    ] = []  # (terminalRef_id_or_empty, attr, connectableId)

    # Numeric parsing errors
    numeric_errors: List[Dict[str, Any]] = []

    # Contextual containment checks: ensure these appear under a voltageLevel
    # (in NODE_BREAKER networks, almost everything is nested there)
    require_voltagelevel_context = {
        "busbarSection",
        "switch",
        "load",
        "generator",
        "line",
        "twoWindingsTransformer",
        "danglingLine",
        "shunt",
        "staticVarCompensator",
        "ratioTapChanger",
        "phaseTapChanger",
    }
    no_voltagelevel_context: List[Dict[str, Any]] = []

    # For samples: store first N rows
    samples: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)

    # Maintain nesting context
    # stack entries: (tag_local, id_or_none)
    stack: List[Tuple[str, Optional[str]]] = []
    current_substation: Optional[str] = None
    current_voltagelevel: Optional[str] = None

    # Common numeric attributes (not exhaustive; just the ones most likely to matter)
    numeric_attr_names = {
        # voltages/limits
        "nominalV",
        "lowVoltageLimit",
        "highVoltageLimit",
        "ratedU1",
        "ratedU2",
        # series parameters
        "r",
        "x",
        # shunt parameters
        "g",
        "b",
        "g1",
        "g2",
        "b1",
        "b2",
        "bMax",
        "bMin",
        # power setpoints / limits
        "p",
        "q",
        "p0",
        "q0",
        "minP",
        "maxP",
        "reactivePowerSetpoint",
        # regulation/voltage
        "voltageSetpoint",
    }
    int_attr_names = {
        "node",
        "node1",
        "node2",
        "tapPosition",
    }  # tapPosition is sometimes an int

    # For better sample output, capture certain fields + context
    sample_specs = {
        "substation": ["id", "country", "tso"],
        "voltageLevel": [
            "id",
            "nominalV",
            "topologyKind",
            "lowVoltageLimit",
            "highVoltageLimit",
            "fictitious",
        ],
        "busbarSection": ["id", "name", "node"],
        "switch": [
            "id",
            "name",
            "kind",
            "open",
            "retained",
            "fictitious",
            "node1",
            "node2",
        ],
        "load": ["id", "loadType", "node"],
        "generator": [
            "id",
            "energySource",
            "minP",
            "maxP",
            "voltageRegulatorOn",
            "node",
        ],
        "staticVarCompensator": [
            "id",
            "node",
            "p",
            "q",
            "bMin",
            "bMax",
            "regulationMode",
            "voltageSetpoint",
        ],
        "line": [
            "id",
            "r",
            "x",
            "g1",
            "b1",
            "g2",
            "b2",
            "voltageLevelId1",
            "node1",
            "voltageLevelId2",
            "node2",
        ],
        "twoWindingsTransformer": [
            "id",
            "r",
            "x",
            "g",
            "b",
            "ratedU1",
            "ratedU2",
            "voltageLevelId1",
            "node1",
            "voltageLevelId2",
            "node2",
        ],
        "danglingLine": ["id", "name", "pairingKey", "r", "x", "g", "b", "node"],
    }

    def push_context(tag_l: str, elem_id: Optional[str]) -> None:
        nonlocal current_substation, current_voltagelevel
        stack.append((tag_l, elem_id))
        if tag_l == "substation":
            current_substation = elem_id
        elif tag_l == "voltageLevel":
            current_voltagelevel = elem_id

    def pop_context(tag_l: str) -> None:
        nonlocal current_substation, current_voltagelevel
        # pop until matching tag_l (defensive)
        while stack:
            t, _ = stack.pop()
            if t == tag_l:
                break
        # recompute current context
        current_substation = None
        current_voltagelevel = None
        for t, eid in stack:
            if t == "substation":
                current_substation = eid
            elif t == "voltageLevel":
                current_voltagelevel = eid

    # Iterate streaming
    root_seen = False
    root_tag_local = None

    for event, payload in context:
        if event == "start-ns":
            prefix, uri = payload
            ns_decl_counts[uri] += 1
            continue

        elem: ET.Element = payload
        tag_l = localname(elem.tag)

        if event == "start":
            if not root_seen:
                # first real element is the root (network)
                root_seen = True
                root_tag_local = tag_l
                iidm_ns = ns_uri(elem.tag)
                if iidm_ns:
                    iidm_schema_version = iidm_ns.rstrip("/").split("/")[-1]
                # capture some network meta attributes if present
                for k in (
                    "caseDate",
                    "forecastDistance",
                    "id",
                    "minimumValidationLevel",
                    "sourceFormat",
                ):
                    if k in elem.attrib:
                        network_meta[k] = elem.attrib.get(k)

            elem_id = elem.attrib.get("id")
            push_context(tag_l, elem_id)

            # collect IDs of major objects
            if tag_l == "substation" and elem_id:
                substation_ids.add(elem_id)
            if tag_l == "voltageLevel" and elem_id:
                voltagelevel_ids.add(elem_id)

            # collect connectables
            if (
                tag_l
                in {
                    "load",
                    "generator",
                    "line",
                    "twoWindingsTransformer",
                    "danglingLine",
                    "shunt",
                    "staticVarCompensator",
                    "threeWindingsTransformer",
                    "hvdcLine",
                    "battery",
                }
                and elem_id
            ):
                connectable_ids.add(elem_id)

            # collect explicit references in attributes (if present)
            if elem_id is None:
                elem_id = ""  # keep stable for reporting

            for attr_name, ref_set, ref_list in (
                ("substationId", substation_ids, refs_substation),
            ):
                if attr_name in elem.attrib:
                    ref_list.append((tag_l, elem_id, attr_name, elem.attrib[attr_name]))

            # voltage level references can appear in many forms
            for attr_name in ("voltageLevelId", "voltageLevelId1", "voltageLevelId2"):
                if attr_name in elem.attrib:
                    refs_voltagelevel.append(
                        (tag_l, elem_id, attr_name, elem.attrib[attr_name])
                    )

            # terminalRef connectableId (common in IIDM)
            if tag_l == "terminalRef":
                # typical attribute is connectableId; sometimes "id" exists too
                cid = elem.attrib.get("connectableId")
                if cid is not None:
                    terminalref_connectable.append(
                        (elem.attrib.get("id", ""), "connectableId", cid)
                    )

            # numeric parsing checks on attributes
            for k, v in elem.attrib.items():
                if k in numeric_attr_names:
                    try:
                        _ = safe_float(v)
                    except Exception as e:
                        numeric_errors.append(
                            {
                                "tag": tag_l,
                                "id": elem.attrib.get("id", ""),
                                "attr": k,
                                "value": short(v, 200),
                                "error": str(e),
                            }
                        )
                elif k in int_attr_names:
                    try:
                        _ = safe_int(v)
                    except Exception as e:
                        numeric_errors.append(
                            {
                                "tag": tag_l,
                                "id": elem.attrib.get("id", ""),
                                "attr": k,
                                "value": short(v, 200),
                                "error": str(e),
                            }
                        )

            # contextual requirement: must be inside voltageLevel
            if tag_l in require_voltagelevel_context and current_voltagelevel is None:
                no_voltagelevel_context.append(
                    {
                        "tag": tag_l,
                        "id": elem.attrib.get("id", ""),
                        "substation_ctx": current_substation,
                        "voltageLevel_ctx": current_voltagelevel,
                    }
                )

            # build samples (first N) with context augmentation
            if tag_l in sample_specs and len(samples[tag_l]) < samples_per_tag:
                row = {c: elem.attrib.get(c) for c in sample_specs[tag_l]}
                # add derived context fields matching your inspection output style
                if tag_l not in ("substation", "voltageLevel"):
                    row["substationId"] = elem.attrib.get(
                        "substationId", current_substation
                    )
                    row["voltageLevelId"] = elem.attrib.get(
                        "voltageLevelId", current_voltagelevel
                    )
                if tag_l == "line":
                    # if file stores voltageLevelId1/2 explicitly, keep them; else derive from context not meaningful
                    pass
                samples[tag_l].append(row)

        elif event == "end":
            # count on end for a stable count of completed elements
            tag_counts[tag_l] += 1

            # pop context if we are closing these containers
            if tag_l in ("substation", "voltageLevel"):
                pop_context(tag_l)
            else:
                # regular pop of current element
                if stack:
                    stack.pop()
                    # recompute current_substation/voltagelevel from stack (cheap enough)
                    current_substation = None
                    current_voltagelevel = None
                    for t, eid in stack:
                        if t == "substation":
                            current_substation = eid
                        elif t == "voltageLevel":
                            current_voltagelevel = eid

            # keep memory bounded
            elem.clear()

    # ---------- post checks ----------

    unresolved_substation = []
    for tag_l, elem_id, attr, ref in refs_substation:
        if ref not in substation_ids:
            unresolved_substation.append((tag_l, elem_id, attr, ref))

    unresolved_voltagelevel = []
    for tag_l, elem_id, attr, ref in refs_voltagelevel:
        if ref not in voltagelevel_ids:
            unresolved_voltagelevel.append((tag_l, elem_id, attr, ref))

    unresolved_terminalref = []
    for term_id, attr, cid in terminalref_connectable:
        if cid not in connectable_ids:
            unresolved_terminalref.append((term_id, attr, cid))

    # top tags
    top_tags = tag_counts.most_common(40)

    # namespaces (top)
    top_ns = ns_decl_counts.most_common(50)

    # assemble report
    report: Dict[str, Any] = {
        "run": {
            "timestamp": iso_now(),
            "script": os.path.basename(__file__),
        },
        "file": {
            "path": xiidm_path,
            "size_bytes": file_size,
            "sha256": file_hash,
        },
        "iidm": {
            "root_tag": root_tag_local,
            "namespace": iidm_ns,
            "schema_version_from_namespace": iidm_schema_version,
            "network_meta": network_meta,
        },
        "counts": {
            "tags_total_unique": len(tag_counts),
            "tags_top40": [{"tag": t, "count": c} for t, c in top_tags],
            "namespaces_top": [{"uri": u, "count": c} for u, c in top_ns],
        },
        "ids": {
            "substations": len(substation_ids),
            "voltageLevels": len(voltagelevel_ids),
            "connectables": len(connectable_ids),
        },
        "checks": {
            "reference_resolution": {
                "substationId": {
                    "refs_total": len(refs_substation),
                    "unresolved_count": len(unresolved_substation),
                    "unresolved_examples": unresolved_substation[:max_issue_examples],
                },
                "voltageLevelId": {
                    "refs_total": len(refs_voltagelevel),
                    "unresolved_count": len(unresolved_voltagelevel),
                    "unresolved_examples": unresolved_voltagelevel[:max_issue_examples],
                },
                "terminalRef.connectableId": {
                    "refs_total": len(terminalref_connectable),
                    "unresolved_count": len(unresolved_terminalref),
                    "unresolved_examples": unresolved_terminalref[:max_issue_examples],
                },
            },
            "contextual_containment": {
                "requires_voltageLevel_context": sorted(
                    list(require_voltagelevel_context)
                ),
                "violations_count": len(no_voltagelevel_context),
                "violation_examples": no_voltagelevel_context[:max_issue_examples],
            },
            "numeric_parsing": {
                "errors_count": len(numeric_errors),
                "error_examples": numeric_errors[:max_issue_examples],
            },
        },
        "samples": samples,
    }

    # write JSON
    json_path = out_prefix + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # write TXT summary
    txt_path = out_prefix + ".txt"
    lines = []
    lines.append(f"File: {xiidm_path}")
    lines.append(f"Size: {file_size} bytes")
    lines.append(f"SHA256: {file_hash}")
    lines.append("")
    lines.append("=== IIDM ===")
    lines.append(f"Root tag: {root_tag_local}")
    lines.append(f"IIDM namespace: {iidm_ns}")
    lines.append(f"IIDM schema version (from namespace): {iidm_schema_version}")
    lines.append("")
    lines.append("=== NETWORK META (root attributes) ===")
    if network_meta:
        for k in sorted(network_meta.keys()):
            lines.append(f"{k}: {network_meta[k]}")
    else:
        lines.append("(none captured)")
    lines.append("")
    lines.append("=== NAMESPACES (top) ===")
    for uri, c in top_ns[:15]:
        lines.append(f"{c:>8}  {uri}")
    lines.append("")
    lines.append("=== ELEMENT COUNTS (top 40) ===")
    for t, c in top_tags:
        lines.append(f"{c:>8}  {t}")
    lines.append("")
    lines.append("=== CHECKS ===")
    lines.append(
        f"substationId refs: {len(refs_substation)} | unresolved: {len(unresolved_substation)}"
    )
    lines.append(
        f"voltageLevelId refs: {len(refs_voltagelevel)} | unresolved: {len(unresolved_voltagelevel)}"
    )
    lines.append(
        f"terminalRef.connectableId refs: {len(terminalref_connectable)} | unresolved: {len(unresolved_terminalref)}"
    )
    lines.append(
        f"context violations (missing voltageLevel context): {len(no_voltagelevel_context)}"
    )
    lines.append(f"numeric parse errors: {len(numeric_errors)}")
    lines.append("")

    def dump_unresolved(title: str, rows: List[Tuple[Any, ...]]) -> None:
        lines.append(title)
        if not rows:
            lines.append("  (none)")
            lines.append("")
            return
        for r in rows[:max_issue_examples]:
            lines.append("  " + " | ".join(str(x) for x in r))
        if len(rows) > max_issue_examples:
            lines.append(f"  ... ({len(rows) - max_issue_examples} more)")
        lines.append("")

    dump_unresolved(
        "Unresolved substationId examples (tag | id | attr | ref):",
        unresolved_substation,
    )
    dump_unresolved(
        "Unresolved voltageLevelId examples (tag | id | attr | ref):",
        unresolved_voltagelevel,
    )
    dump_unresolved(
        "Unresolved terminalRef.connectableId examples (terminalRefId | attr | connectableId):",
        unresolved_terminalref,
    )

    if numeric_errors:
        lines.append("Numeric parse error examples (tag | id | attr | value | error):")
        for e in numeric_errors[:max_issue_examples]:
            lines.append(
                f"  {e['tag']} | {e['id']} | {e['attr']} | {e['value']} | {e['error']}"
            )
        if len(numeric_errors) > max_issue_examples:
            lines.append(f"  ... ({len(numeric_errors) - max_issue_examples} more)")
        lines.append("")

    # samples section
    lines.append("=== SAMPLES (first N per tag) ===")
    for tag_l, rows in samples.items():
        cols = list(rows[0].keys()) if rows else sample_specs.get(tag_l, ["id"])
        lines.append(
            f"--- {tag_l} samples (first {min(samples_per_tag, len(rows))}) [TSV] ---"
        )
        lines.append(tsv(rows, cols, max_rows=samples_per_tag) if rows else "(none)")
        lines.append("")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Sanity-check an IIDM (XIIdm) XML file and write <out>.json / <out>.txt reports.",
    )
    ap.add_argument("xiidm", help="Path to .xiidm (IIDM XML) file")
    ap.add_argument(
        "--out", default="00_sanity", help="Output prefix (default: 00_sanity)"
    )
    ap.add_argument(
        "--samples", type=int, default=15, help="Samples per tag (default: 15)"
    )
    ap.add_argument(
        "--max-issues",
        type=int,
        default=50,
        help="Max issue examples stored/printed (default: 50)",
    )
    args = ap.parse_args()

    run_sanity(
        xiidm_path=args.xiidm,
        out_prefix=args.out,
        samples_per_tag=args.samples,
        max_issue_examples=args.max_issues,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
03_boundary_dangling.py

Materialize IIDM danglingLines into explicit boundary buses + branches, while conserving
dangling-line shunt/admittance and optional p0/q0 in a deterministic way.

Input:
  - projected JSON from 02_project_equipment.py

Output:
  - <out>.json    : projected model with:
      * buses appended with one synthetic "boundary bus" per danglingLine
      * branches appended with one synthetic branch per danglingLine (series r/x)
      * shunts appended with one synthetic shunt per danglingLine (g/b) placed on INTERNAL bus
      * loads appended with one synthetic load per danglingLine if p0/q0 exist (placed on BOUNDARY bus)
      * danglingLine_materialization[] mapping table
  - <report>.json : verification report (counts, endpoint checks, conservation totals, examples)

Design choice (explicit):
  - The danglingLine's (g,b) shunt is attached to the INTERNAL bus as a bus shunt element.
  - The optional (p0,q0) is attached to the BOUNDARY bus as a load-like element.
  - The danglingLine's series (r,x) becomes a branch between INTERNAL bus and BOUNDARY bus.

Typical use:
  python 03_boundary_dangling.py --in 02_projected.json --out 03_projected_boundary.json --report 03_boundary_report.json
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from typing import Any, Dict, List, Optional

try:
    from .common import ensure_list, iso_now, sha256_file
    from .common import safe_float_opt as safe_float
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import ensure_list, iso_now, sha256_file  # type: ignore
    from common import safe_float_opt as safe_float


# -------------------- utilities --------------------


def get_attr(d: Dict[str, Any], key: str) -> Any:
    # stage02 stores danglingLine details under "attrib" (strings)
    a = d.get("attrib") or {}
    if key in d:
        return d.get(key)
    return a.get(key)


def bus_index(buses: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for b in buses:
        bid = b.get("bus_id")
        if isinstance(bid, int):
            out[bid] = b
    return out


# -------------------- core --------------------


def materialize_dangling(
    in_path: str,
    out_path: str,
    report_path: str,
    boundary_bus_id_start: Optional[int] = None,
    max_report_examples: int = 50,
    preserve_original_dangling_list: bool = True,
) -> None:
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)

    in_size = os.path.getsize(in_path)
    in_hash = sha256_file(in_path)

    with open(in_path, "r", encoding="utf-8") as f:
        projected = json.load(f)

    # Work on a deep copy to keep input immutable
    out = copy.deepcopy(projected)

    buses: List[Dict[str, Any]] = ensure_list(out.get("buses"))
    loads: List[Dict[str, Any]] = ensure_list(out.get("loads"))
    shunts: List[Dict[str, Any]] = ensure_list(out.get("shunts"))
    branches: List[Dict[str, Any]] = ensure_list(out.get("branches"))
    dangling_lines: List[Dict[str, Any]] = ensure_list(out.get("danglingLines"))

    bus_by_id = bus_index(buses)
    existing_bus_ids = sorted(bus_by_id.keys())
    max_bus_id = existing_bus_ids[-1] if existing_bus_ids else 0

    next_bus_id = (
        boundary_bus_id_start if boundary_bus_id_start is not None else (max_bus_id + 1)
    )

    # Deterministic order: sort by danglingLine id (string)
    dangling_sorted = sorted(dangling_lines, key=lambda d: str(d.get("id", "")))

    # Totals (for conservation checks)
    orig_g_sum = 0.0
    orig_b_sum = 0.0
    orig_g_count = 0
    orig_b_count = 0

    new_g_sum = 0.0
    new_b_sum = 0.0
    new_g_count = 0
    new_b_count = 0

    # Verification
    skipped = 0
    skipped_examples: List[Dict[str, Any]] = []
    bad_endpoint_refs = 0
    bad_endpoint_examples: List[Dict[str, Any]] = []

    created_boundary_buses = 0
    created_boundary_branches = 0
    created_boundary_shunts = 0
    created_boundary_loads = 0

    materialization: List[Dict[str, Any]] = []

    # Helper: get internal bus metadata to copy nominalV/substation/voltageLevel
    def internal_bus_meta(
        internal_bus_id: int, fallback: Dict[str, Any]
    ) -> Dict[str, Any]:
        b = bus_by_id.get(internal_bus_id)
        if not b:
            return {
                "voltageLevelId": fallback.get("voltageLevelId"),
                "substationId": fallback.get("substationId"),
                "nominalV": None,
            }
        return {
            "voltageLevelId": b.get("voltageLevelId") or fallback.get("voltageLevelId"),
            "substationId": b.get("substationId") or fallback.get("substationId"),
            "nominalV": b.get("nominalV"),
        }

    for dl in dangling_sorted:
        dl_id = str(dl.get("id", ""))
        internal_bus = dl.get("bus_id")

        if internal_bus is None or not isinstance(internal_bus, int):
            skipped += 1
            if len(skipped_examples) < max_report_examples:
                skipped_examples.append(
                    {
                        "id": dl_id,
                        "reason": "INTERNAL_BUS_MISSING",
                        "bus_id": internal_bus,
                        "voltageLevelId": dl.get("voltageLevelId"),
                        "node": dl.get("node"),
                    }
                )
            continue

        if internal_bus not in bus_by_id:
            bad_endpoint_refs += 1
            if len(bad_endpoint_examples) < max_report_examples:
                bad_endpoint_examples.append(
                    {
                        "id": dl_id,
                        "reason": "INTERNAL_BUS_NOT_IN_BUSES",
                        "bus_id": internal_bus,
                    }
                )
            # still skip; cannot attach reliably
            skipped += 1
            continue

        # Pull electrical parameters from attrib (strings usually)
        r = get_attr(dl, "r")
        x = get_attr(dl, "x")
        g = get_attr(dl, "g")
        b = get_attr(dl, "b")
        pairing_key = get_attr(dl, "pairingKey")
        name = get_attr(dl, "name")

        # Optional boundary injection
        p0 = get_attr(dl, "p0")
        q0 = get_attr(dl, "q0")

        # Update original totals (if parseable)
        g_f = safe_float(g)
        b_f = safe_float(b)
        if g_f is not None:
            orig_g_sum += g_f
            orig_g_count += 1
        if b_f is not None:
            orig_b_sum += b_f
            orig_b_count += 1

        # Create boundary bus (synthetic)
        meta = internal_bus_meta(internal_bus, dl)
        boundary_bus = {
            "bus_id": next_bus_id,
            "is_boundary": True,
            "boundary_of": dl_id,
            "pairingKey": pairing_key,
            "name": name,
            "voltageLevelId": meta.get("voltageLevelId"),
            "substationId": meta.get("substationId"),
            "nominalV": meta.get("nominalV"),
            "nodes": [],  # synthetic; no node-breaker nodes
        }
        boundary_bus_id = next_bus_id
        next_bus_id += 1

        buses.append(boundary_bus)
        bus_by_id[boundary_bus_id] = boundary_bus
        created_boundary_buses += 1

        # Create branch representing the series part of danglingLine
        # Use deterministic synthetic id to avoid collisions with existing connectables.
        branch_id = f"__DL_BRANCH__{dl_id}"
        branch = {
            "type": "danglingLine",
            "id": branch_id,
            "source_danglingLineId": dl_id,
            "fbus": internal_bus,
            "tbus": boundary_bus_id,
            "attrib": {
                "id": branch_id,
                "sourceDanglingLineId": dl_id,
                "r": r,
                "x": x,
                "g": g,
                "b": b,
                "pairingKey": pairing_key,
                "name": name,
            },
        }
        branches.append(branch)
        created_boundary_branches += 1

        # Create shunt on INTERNAL bus for the (g,b)
        # If g/b absent or unparsable, still store raw values for traceability.
        shunt_id = f"__DL_SHUNT__{dl_id}"
        shunt = {
            "type": "shunt",
            "id": shunt_id,
            "bus_id": internal_bus,
            "voltageLevelId": meta.get("voltageLevelId"),
            "substationId": meta.get("substationId"),
            "node": None,
            "attrib": {
                "id": shunt_id,
                "origin": "danglingLine",
                "danglingLineId": dl_id,
                "g": g,
                "b": b,
                "pairingKey": pairing_key,
                "name": name,
            },
            "model": {
                "modelType": "constantAdmittance",
                "note": "Created by 03_boundary_dangling.py from danglingLine (g,b).",
            },
            "model_steps": None,
        }
        shunts.append(shunt)
        created_boundary_shunts += 1

        # Update new totals (if parseable)
        if g_f is not None:
            new_g_sum += g_f
            new_g_count += 1
        if b_f is not None:
            new_b_sum += b_f
            new_b_count += 1

        # Create optional load on BOUNDARY bus for p0/q0
        if p0 is not None or q0 is not None:
            # store even if unparsable, for traceability
            load_id = f"__DL_LOAD__{dl_id}"
            boundary_load = {
                "type": "load",
                "id": load_id,
                "bus_id": boundary_bus_id,
                "voltageLevelId": meta.get("voltageLevelId"),
                "substationId": meta.get("substationId"),
                "node": None,
                "attrib": {
                    "id": load_id,
                    "origin": "danglingLine",
                    "danglingLineId": dl_id,
                    "p0": p0,
                    "q0": q0,
                    "pairingKey": pairing_key,
                    "name": name,
                    "note": "Created by 03_boundary_dangling.py from danglingLine p0/q0 (if present).",
                },
            }
            loads.append(boundary_load)
            created_boundary_loads += 1

        # Record mapping
        materialization.append(
            {
                "danglingLineId": dl_id,
                "internal_bus_id": internal_bus,
                "boundary_bus_id": boundary_bus_id,
                "created_branch_id": branch_id,
                "created_shunt_id": shunt_id,
                "created_load_id": (
                    f"__DL_LOAD__{dl_id}"
                    if (p0 is not None or q0 is not None)
                    else None
                ),
                "pairingKey": pairing_key,
            }
        )

        # Mark original danglingLine as materialized (for audit)
        dl["materialized"] = True
        dl["boundary_bus_id"] = boundary_bus_id
        dl["created_branch_id"] = branch_id

    # If desired, you can remove danglingLines to avoid double handling downstream.
    # Default keeps them but marks them materialized and provides mapping.
    if not preserve_original_dangling_list:
        out["danglingLines"] = []

    out["buses"] = buses
    out["loads"] = loads
    out["shunts"] = shunts
    out["branches"] = branches
    out["danglingLines"] = (
        dangling_sorted
        if preserve_original_dangling_list
        else out.get("danglingLines", [])
    )
    out["danglingLine_materialization"] = materialization

    # Update meta
    meta = out.get("meta", {})
    meta.setdefault("transforms", [])
    meta["transforms"].append(
        {
            "step": "03_boundary_dangling",
            "created_at": iso_now(),
            "input": {"path": in_path, "size_bytes": in_size, "sha256": in_hash},
            "created": {
                "boundary_buses": created_boundary_buses,
                "boundary_branches": created_boundary_branches,
                "dangling_shunts": created_boundary_shunts,
                "dangling_loads": created_boundary_loads,
            },
            "policy": {
                "shunt_placement": "internal_bus",
                "p0q0_placement": "boundary_bus",
                "series_branch": "internal_to_boundary",
            },
        }
    )
    out["meta"] = meta

    # Conservation checks (numeric-only; ignores unparsable)
    g_conserved = (orig_g_count == new_g_count) and (
        abs(orig_g_sum - new_g_sum) <= 1e-9 * max(1.0, abs(orig_g_sum))
    )
    b_conserved = (orig_b_count == new_b_count) and (
        abs(orig_b_sum - new_b_sum) <= 1e-9 * max(1.0, abs(orig_b_sum))
    )

    report = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "input": {"path": in_path, "size_bytes": in_size, "sha256": in_hash},
        },
        "stats_before": {
            "buses": len(ensure_list(projected.get("buses"))),
            "branches": len(ensure_list(projected.get("branches"))),
            "shunts": len(ensure_list(projected.get("shunts"))),
            "loads": len(ensure_list(projected.get("loads"))),
            "danglingLines": len(ensure_list(projected.get("danglingLines"))),
        },
        "stats_after": {
            "buses": len(buses),
            "branches": len(branches),
            "shunts": len(shunts),
            "loads": len(loads),
            "danglingLines": len(out.get("danglingLines", [])),
            "danglingLine_materialization": len(materialization),
        },
        "created": {
            "boundary_buses": created_boundary_buses,
            "boundary_branches": created_boundary_branches,
            "dangling_shunts": created_boundary_shunts,
            "dangling_loads": created_boundary_loads,
            "skipped_danglingLines": skipped,
        },
        "checks": {
            "endpoint_integrity": {
                "bad_internal_bus_refs": bad_endpoint_refs,
                "bad_internal_bus_examples": bad_endpoint_examples[
                    :max_report_examples
                ],
                "skipped_examples": skipped_examples[:max_report_examples],
            },
            "conservation_g_b": {
                "orig": {
                    "g_sum": orig_g_sum,
                    "g_count": orig_g_count,
                    "b_sum": orig_b_sum,
                    "b_count": orig_b_count,
                },
                "new": {
                    "g_sum": new_g_sum,
                    "g_count": new_g_count,
                    "b_sum": new_b_sum,
                    "b_count": new_b_count,
                },
                "g_conserved_numeric": g_conserved,
                "b_conserved_numeric": b_conserved,
                "note": "Only counts danglingLines where g/b parse as floats; raw values always preserved in output.",
            },
        },
        "examples": {
            "materialization_first": materialization[
                : min(max_report_examples, len(materialization))
            ],
            "new_boundary_buses_first": [b for b in buses if b.get("is_boundary")][
                : min(10, created_boundary_buses)
            ],
            "new_boundary_branches_first": [
                br for br in branches if br.get("type") == "danglingLine"
            ][: min(10, created_boundary_branches)],
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "danglingLines_in": report["stats_before"]["danglingLines"],
                "boundary_buses_created": created_boundary_buses,
                "branches_created": created_boundary_branches,
                "shunts_created": created_boundary_shunts,
                "loads_created": created_boundary_loads,
                "skipped": skipped,
                "g_conserved_numeric": g_conserved,
                "b_conserved_numeric": b_conserved,
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Create boundary buses/branches for danglingLines in projected JSON.",
    )
    ap.add_argument(
        "--in",
        dest="inp",
        required=True,
        help="Input projected JSON (from 02_project_equipment.py)",
    )
    ap.add_argument(
        "--out",
        default="03_projected_boundary.json",
        help="Output JSON (default: 03_projected_boundary.json)",
    )
    ap.add_argument(
        "--report",
        default="03_boundary_report.json",
        help="Report JSON (default: 03_boundary_report.json)",
    )
    ap.add_argument(
        "--boundary-bus-id-start",
        type=int,
        default=None,
        help="Optional explicit first boundary bus id. Default is max(existing)+1.",
    )
    ap.add_argument(
        "--max-report-examples",
        type=int,
        default=50,
        help="Max examples stored in report (default: 50)",
    )
    ap.add_argument(
        "--drop-original-dangling",
        action="store_true",
        help="Remove danglingLines list from output (keeps only materialization mapping).",
    )
    args = ap.parse_args()

    materialize_dangling(
        in_path=args.inp,
        out_path=args.out,
        report_path=args.report,
        boundary_bus_id_start=args.boundary_bus_id_start,
        max_report_examples=args.max_report_examples,
        preserve_original_dangling_list=(not args.drop_original_dangling),
    )


if __name__ == "__main__":
    main()

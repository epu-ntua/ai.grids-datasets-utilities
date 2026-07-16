#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_project_equipment.py

Project IIDM (XIIdm) equipment onto buses produced by 01_build_busmap.py.

Inputs:
  - XIIdm file (.xiidm)
  - busmap JSON from 01_build_busmap.py (node_to_bus mapping)

Outputs:
  - <out>.json    : projected equipment model (buses, loads, generators, shunts, SVCs,
                    branches (lines + 2W transformers), danglingLines) + warnings
  - <report>.json : verification metrics + samples (mapping coverage, 2-terminal completeness, etc.)

Design goals:
  - Faithful projection: keep original element attributes (default) without unit conversions.
  - Deterministic IDs: preserve IIDM ids; bus ids come from busmap.
  - Verification after projection: no silent drops, explicit warnings for unmapped/malformed items.

Typical use:
  python 02_project_equipment.py rte-01Jan21-0000.xiidm --busmap 01_busmap.json --out 02_projected.json --report 02_projected_report.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

try:
    from .common import (
        iso_now,
        localname,
        ns_uri,
        sha256_file,
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
        safe_int_strict as safe_int,
    )


# -------------------- utilities --------------------


def parse_bool(s: Optional[str], default: Optional[bool] = None) -> Optional[bool]:
    if s is None:
        return default
    v = s.strip().lower()
    if v in ("true", "1", "yes", "y"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    return default


def pick_attrib(elem: ET.Element, keep_all: bool, keys: List[str]) -> Dict[str, Any]:
    if keep_all:
        return dict(elem.attrib)
    return {k: elem.attrib.get(k) for k in keys if k in elem.attrib}


def warn_add(
    warnings: List[Dict[str, Any]],
    counts: collections.Counter,
    code: str,
    detail: Dict[str, Any],
    max_warnings: int,
) -> None:
    counts[code] += 1
    if len(warnings) < max_warnings:
        d = {"code": code}
        d.update(detail)
        warnings.append(d)


# -------------------- core --------------------


def project_equipment(
    xiidm_path: str,
    busmap_path: str,
    out_path: str,
    report_path: str,
    keep_all_attribs: bool = True,
    max_warnings: int = 5000,
    max_report_examples: int = 50,
) -> None:
    if not os.path.exists(xiidm_path):
        raise FileNotFoundError(xiidm_path)
    if not os.path.exists(busmap_path):
        raise FileNotFoundError(busmap_path)

    xiidm_size = os.path.getsize(xiidm_path)
    xiidm_hash = sha256_file(xiidm_path)
    busmap_size = os.path.getsize(busmap_path)
    busmap_hash = sha256_file(busmap_path)

    with open(busmap_path, "r", encoding="utf-8") as f:
        busmap = json.load(f)

    node_to_bus_raw: Dict[str, Dict[str, int]] = busmap.get("node_to_bus", {})
    vl_meta: Dict[str, Dict[str, Any]] = busmap.get("voltageLevel", {})

    # Build fast mapping (vl_id, node_int) -> bus_id
    node_to_bus: Dict[str, Dict[int, int]] = {}
    for vl_id, m in node_to_bus_raw.items():
        mm: Dict[int, int] = {}
        for ns, bid in m.items():
            try:
                mm[int(ns)] = int(bid)
            except Exception:
                continue
        node_to_bus[vl_id] = mm

    def get_bus(vl_id: Optional[str], node: Optional[int]) -> Optional[int]:
        if vl_id is None or node is None:
            return None
        mm = node_to_bus.get(vl_id)
        if mm is None:
            return None
        return mm.get(node)

    # Invert mapping to build bus table (bus_id -> {vl_id, nodes[]})
    bus_nodes: Dict[int, List[int]] = collections.defaultdict(list)
    bus_vl: Dict[int, str] = {}
    for vl_id, m in node_to_bus.items():
        for n, bid in m.items():
            bus_nodes[bid].append(n)
            prev = bus_vl.get(bid)
            if prev is None:
                bus_vl[bid] = vl_id
            elif prev != vl_id:
                # Should never happen: bus ids assigned per component per VL; keep first, warn later
                pass

    # Build bus objects
    buses: List[Dict[str, Any]] = []
    bus_conflict_vl = 0
    for bid in sorted(bus_nodes.keys()):
        vl_id = bus_vl.get(bid)
        if vl_id is None:
            bus_conflict_vl += 1
            continue
        meta = vl_meta.get(vl_id, {})
        buses.append(
            {
                "bus_id": bid,
                "voltageLevelId": vl_id,
                "substationId": meta.get("substationId"),
                "nominalV": meta.get("nominalV"),
                "topologyKind": meta.get("topologyKind"),
                "nodes": sorted(bus_nodes[bid]),
            }
        )

    # Containers
    loads: List[Dict[str, Any]] = []
    generators: List[Dict[str, Any]] = []
    shunts: List[Dict[str, Any]] = []
    svcs: List[Dict[str, Any]] = []
    branches: List[Dict[str, Any]] = []  # lines + 2W transformers
    dangling: List[Dict[str, Any]] = []

    warnings: List[Dict[str, Any]] = []
    warn_counts: collections.Counter = collections.Counter()

    # Root meta
    iidm_namespace: Optional[str] = None
    iidm_schema_version: Optional[str] = None
    network_meta: Dict[str, Any] = {}
    root_seen = False
    root_tag_local: Optional[str] = None

    # Context
    current_substation: Optional[str] = None
    current_voltagelevel: Optional[str] = None

    # In-progress nested parsing
    current_shunt: Optional[Dict[str, Any]] = None
    current_transformer: Optional[Dict[str, Any]] = None

    # Tap changer contexts
    current_ratio_tc: Optional[Dict[str, Any]] = None
    current_phase_tc: Optional[Dict[str, Any]] = None
    ratio_tc_steps = 0
    phase_tc_steps = 0

    # Keys for compact mode
    KEYS_LOAD = ["id", "name", "loadType", "node", "p0", "q0", "fictitious"]
    KEYS_GEN = [
        "id",
        "name",
        "energySource",
        "node",
        "minP",
        "maxP",
        "targetP",
        "targetQ",
        "voltageRegulatorOn",
        "fictitious",
    ]
    KEYS_SVC = [
        "id",
        "name",
        "node",
        "p",
        "q",
        "bMin",
        "bMax",
        "regulationMode",
        "voltageSetpoint",
        "reactivePowerSetpoint",
        "fictitious",
    ]
    KEYS_LINE = [
        "id",
        "name",
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
        "selectedOperationalLimitsGroupId1",
        "selectedOperationalLimitsGroupId2",
        "fictitious",
    ]
    KEYS_TWTR = [
        "id",
        "name",
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
        "selectedOperationalLimitsGroupId1",
        "selectedOperationalLimitsGroupId2",
        "fictitious",
    ]
    KEYS_DANGLING = [
        "id",
        "name",
        "pairingKey",
        "r",
        "x",
        "g",
        "b",
        "node",
        "p0",
        "q0",
        "fictitious",
    ]
    KEYS_SHUNT = ["id", "name", "node", "fictitious"]

    # Stream parse
    context = ET.iterparse(xiidm_path, events=("start", "end"))
    for event, elem in context:
        tag_l = localname(elem.tag)

        if event == "start":
            if not root_seen:
                root_seen = True
                root_tag_local = tag_l
                iidm_namespace = ns_uri(elem.tag)
                if iidm_namespace:
                    iidm_schema_version = iidm_namespace.rstrip("/").split("/")[-1]
                for k in (
                    "caseDate",
                    "forecastDistance",
                    "id",
                    "minimumValidationLevel",
                    "sourceFormat",
                ):
                    if k in elem.attrib:
                        network_meta[k] = elem.attrib.get(k)

            if tag_l == "substation":
                current_substation = elem.attrib.get("id")

            elif tag_l == "voltageLevel":
                current_voltagelevel = elem.attrib.get("id")

            elif tag_l == "load":
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                node = None
                try:
                    node = safe_int(ns) if ns is not None else None
                except Exception:
                    pass
                bid = get_bus(vl_id, node)
                if bid is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "LOAD_UNMAPPED_BUS",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId": vl_id,
                            "node": ns,
                            "substation_ctx": current_substation,
                        },
                        max_warnings,
                    )
                loads.append(
                    {
                        "type": "load",
                        "id": elem.attrib.get("id", ""),
                        "bus_id": bid,
                        "voltageLevelId": vl_id,
                        "substationId": elem.attrib.get(
                            "substationId", current_substation
                        ),
                        "node": node,
                        "attrib": pick_attrib(elem, keep_all_attribs, KEYS_LOAD),
                    }
                )

            elif tag_l == "generator":
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                node = None
                try:
                    node = safe_int(ns) if ns is not None else None
                except Exception:
                    pass
                bid = get_bus(vl_id, node)
                if bid is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "GEN_UNMAPPED_BUS",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId": vl_id,
                            "node": ns,
                            "substation_ctx": current_substation,
                        },
                        max_warnings,
                    )
                generators.append(
                    {
                        "type": "generator",
                        "id": elem.attrib.get("id", ""),
                        "bus_id": bid,
                        "voltageLevelId": vl_id,
                        "substationId": elem.attrib.get(
                            "substationId", current_substation
                        ),
                        "node": node,
                        "attrib": pick_attrib(elem, keep_all_attribs, KEYS_GEN),
                    }
                )

            elif tag_l == "staticVarCompensator":
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                node = None
                try:
                    node = safe_int(ns) if ns is not None else None
                except Exception:
                    pass
                bid = get_bus(vl_id, node)
                if bid is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "SVC_UNMAPPED_BUS",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId": vl_id,
                            "node": ns,
                            "substation_ctx": current_substation,
                        },
                        max_warnings,
                    )
                svcs.append(
                    {
                        "type": "staticVarCompensator",
                        "id": elem.attrib.get("id", ""),
                        "bus_id": bid,
                        "voltageLevelId": vl_id,
                        "substationId": elem.attrib.get(
                            "substationId", current_substation
                        ),
                        "node": node,
                        "attrib": pick_attrib(elem, keep_all_attribs, KEYS_SVC),
                    }
                )

            elif tag_l == "shunt":
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                node = None
                try:
                    node = safe_int(ns) if ns is not None else None
                except Exception:
                    pass
                bid = get_bus(vl_id, node)
                if bid is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "SHUNT_UNMAPPED_BUS",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId": vl_id,
                            "node": ns,
                            "substation_ctx": current_substation,
                        },
                        max_warnings,
                    )
                current_shunt = {
                    "type": "shunt",
                    "id": elem.attrib.get("id", ""),
                    "bus_id": bid,
                    "voltageLevelId": vl_id,
                    "substationId": elem.attrib.get("substationId", current_substation),
                    "node": node,
                    "attrib": pick_attrib(elem, keep_all_attribs, KEYS_SHUNT),
                    "model": None,  # filled by child model tags if any
                    "model_steps": None,  # optional
                }

            elif tag_l == "shuntLinearModel" and current_shunt is not None:
                # Store model attributes; do not interpret here.
                current_shunt["model"] = {
                    "modelType": "shuntLinearModel",
                    "attrib": dict(elem.attrib)
                    if keep_all_attribs
                    else dict(elem.attrib),
                }

            elif tag_l == "line":
                # 2-terminal connectable
                vl1 = elem.attrib.get("voltageLevelId1")
                vl2 = elem.attrib.get("voltageLevelId2")
                n1s = elem.attrib.get("node1")
                n2s = elem.attrib.get("node2")
                n1 = n2 = None
                try:
                    n1 = safe_int(n1s) if n1s is not None else None
                except Exception:
                    pass
                try:
                    n2 = safe_int(n2s) if n2s is not None else None
                except Exception:
                    pass
                b1 = get_bus(vl1, n1)
                b2 = get_bus(vl2, n2)

                if b1 is None or b2 is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "LINE_UNMAPPED_ENDPOINT",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId1": vl1,
                            "node1": n1s,
                            "bus1": b1,
                            "voltageLevelId2": vl2,
                            "node2": n2s,
                            "bus2": b2,
                        },
                        max_warnings,
                    )

                branches.append(
                    {
                        "type": "line",
                        "id": elem.attrib.get("id", ""),
                        "fbus": b1,
                        "tbus": b2,
                        "voltageLevelId1": vl1,
                        "node1": n1,
                        "voltageLevelId2": vl2,
                        "node2": n2,
                        "attrib": pick_attrib(elem, keep_all_attribs, KEYS_LINE),
                    }
                )

            elif tag_l == "twoWindingsTransformer":
                vl1 = elem.attrib.get("voltageLevelId1")
                vl2 = elem.attrib.get("voltageLevelId2")
                n1s = elem.attrib.get("node1")
                n2s = elem.attrib.get("node2")
                n1 = n2 = None
                try:
                    n1 = safe_int(n1s) if n1s is not None else None
                except Exception:
                    pass
                try:
                    n2 = safe_int(n2s) if n2s is not None else None
                except Exception:
                    pass
                b1 = get_bus(vl1, n1)
                b2 = get_bus(vl2, n2)

                if b1 is None or b2 is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "TWTR_UNMAPPED_ENDPOINT",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId1": vl1,
                            "node1": n1s,
                            "bus1": b1,
                            "voltageLevelId2": vl2,
                            "node2": n2s,
                            "bus2": b2,
                        },
                        max_warnings,
                    )

                current_transformer = {
                    "type": "twoWindingsTransformer",
                    "id": elem.attrib.get("id", ""),
                    "fbus": b1,
                    "tbus": b2,
                    "voltageLevelId1": vl1,
                    "node1": n1,
                    "voltageLevelId2": vl2,
                    "node2": n2,
                    "attrib": pick_attrib(elem, keep_all_attribs, KEYS_TWTR),
                    "ratioTapChanger": None,
                    "phaseTapChanger": None,
                }
                current_ratio_tc = None
                current_phase_tc = None
                ratio_tc_steps = 0
                phase_tc_steps = 0

            elif tag_l == "ratioTapChanger" and current_transformer is not None:
                current_ratio_tc = {
                    "attrib": dict(elem.attrib)
                    if keep_all_attribs
                    else dict(elem.attrib),
                    "steps_total": 0,
                    "selected_step": None,
                }
                ratio_tc_steps = 0

            elif tag_l == "phaseTapChanger" and current_transformer is not None:
                current_phase_tc = {
                    "attrib": dict(elem.attrib)
                    if keep_all_attribs
                    else dict(elem.attrib),
                    "steps_total": 0,
                    "selected_step": None,
                }
                phase_tc_steps = 0

            elif tag_l == "step":
                # "step" exists in many places; only attach if in a tap changer context.
                if current_ratio_tc is not None:
                    ratio_tc_steps += 1
                    pos_s = elem.attrib.get("position")
                    tap_pos = current_ratio_tc["attrib"].get(
                        "tapPosition"
                    ) or current_ratio_tc["attrib"].get("tapPositionValue")
                    if pos_s is not None and tap_pos is not None:
                        try:
                            if (
                                int(pos_s) == int(tap_pos)
                                and current_ratio_tc["selected_step"] is None
                            ):
                                current_ratio_tc["selected_step"] = dict(elem.attrib)
                        except Exception:
                            pass
                elif current_phase_tc is not None:
                    phase_tc_steps += 1
                    pos_s = elem.attrib.get("position")
                    tap_pos = current_phase_tc["attrib"].get(
                        "tapPosition"
                    ) or current_phase_tc["attrib"].get("tapPositionValue")
                    if pos_s is not None and tap_pos is not None:
                        try:
                            if (
                                int(pos_s) == int(tap_pos)
                                and current_phase_tc["selected_step"] is None
                            ):
                                current_phase_tc["selected_step"] = dict(elem.attrib)
                        except Exception:
                            pass

            elif tag_l == "danglingLine":
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                node = None
                try:
                    node = safe_int(ns) if ns is not None else None
                except Exception:
                    pass
                bid = get_bus(vl_id, node)
                if bid is None:
                    warn_add(
                        warnings,
                        warn_counts,
                        "DANGLING_UNMAPPED_BUS",
                        {
                            "id": elem.attrib.get("id", ""),
                            "voltageLevelId": vl_id,
                            "node": ns,
                            "substation_ctx": current_substation,
                        },
                        max_warnings,
                    )
                dangling.append(
                    {
                        "type": "danglingLine",
                        "id": elem.attrib.get("id", ""),
                        "bus_id": bid,
                        "voltageLevelId": vl_id,
                        "substationId": elem.attrib.get(
                            "substationId", current_substation
                        ),
                        "node": node,
                        "attrib": pick_attrib(elem, keep_all_attribs, KEYS_DANGLING),
                    }
                )

        elif event == "end":
            if (
                tag_l == "ratioTapChanger"
                and current_transformer is not None
                and current_ratio_tc is not None
            ):
                current_ratio_tc["steps_total"] = ratio_tc_steps
                current_transformer["ratioTapChanger"] = current_ratio_tc
                current_ratio_tc = None
                ratio_tc_steps = 0

            elif (
                tag_l == "phaseTapChanger"
                and current_transformer is not None
                and current_phase_tc is not None
            ):
                current_phase_tc["steps_total"] = phase_tc_steps
                current_transformer["phaseTapChanger"] = current_phase_tc
                current_phase_tc = None
                phase_tc_steps = 0

            elif tag_l == "twoWindingsTransformer" and current_transformer is not None:
                branches.append(current_transformer)
                current_transformer = None
                current_ratio_tc = None
                current_phase_tc = None
                ratio_tc_steps = 0
                phase_tc_steps = 0

            elif tag_l == "shunt" and current_shunt is not None:
                shunts.append(current_shunt)
                current_shunt = None

            elif tag_l == "voltageLevel":
                current_voltagelevel = None

            elif tag_l == "substation":
                current_substation = None

            elem.clear()

    # -------------------- verification metrics --------------------

    def count_unmapped_single(items: List[Dict[str, Any]], key: str = "bus_id") -> int:
        return sum(1 for it in items if it.get(key) is None)

    def count_unmapped_branch(items: List[Dict[str, Any]]) -> int:
        return sum(
            1 for it in items if it.get("fbus") is None or it.get("tbus") is None
        )

    unmapped_loads = count_unmapped_single(loads)
    unmapped_gens = count_unmapped_single(generators)
    unmapped_shunts = count_unmapped_single(shunts)
    unmapped_svcs = count_unmapped_single(svcs)
    unmapped_dangling = count_unmapped_single(dangling)
    unmapped_branches = count_unmapped_branch(branches)

    # Ensure all bus ids referenced exist (sanity)
    bus_ids_set = set(b["bus_id"] for b in buses)
    bad_bus_refs = 0
    bad_bus_ref_examples: List[Dict[str, Any]] = []

    def check_bus_ref(obj: Dict[str, Any], fields: List[str], kind: str) -> None:
        nonlocal bad_bus_refs
        for f in fields:
            bid = obj.get(f)
            if bid is None:
                continue
            if bid not in bus_ids_set:
                bad_bus_refs += 1
                if len(bad_bus_ref_examples) < max_report_examples:
                    bad_bus_ref_examples.append(
                        {
                            "kind": kind,
                            "id": obj.get("id", ""),
                            "field": f,
                            "bus_id": bid,
                        }
                    )

    for it in loads:
        check_bus_ref(it, ["bus_id"], "load")
    for it in generators:
        check_bus_ref(it, ["bus_id"], "generator")
    for it in shunts:
        check_bus_ref(it, ["bus_id"], "shunt")
    for it in svcs:
        check_bus_ref(it, ["bus_id"], "svc")
    for it in dangling:
        check_bus_ref(it, ["bus_id"], "danglingLine")
    for it in branches:
        check_bus_ref(it, ["fbus", "tbus"], it.get("type", "branch"))

    # counts by branch type
    branch_type_counts = collections.Counter(b.get("type", "branch") for b in branches)

    # -------------------- write outputs --------------------

    projected = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "source_xiidm": {
                "path": xiidm_path,
                "size_bytes": xiidm_size,
                "sha256": xiidm_hash,
                "iidm_namespace": iidm_namespace,
                "iidm_schema_version": iidm_schema_version,
                "network_meta": network_meta,
                "root_tag": root_tag_local,
            },
            "source_busmap": {
                "path": busmap_path,
                "size_bytes": busmap_size,
                "sha256": busmap_hash,
            },
            "keep_all_attribs": keep_all_attribs,
        },
        "buses": buses,
        "loads": loads,
        "generators": generators,
        "shunts": shunts,
        "staticVarCompensators": svcs,
        "branches": branches,
        "danglingLines": dangling,
        "warnings": warnings,
        "warning_counts": dict(warn_counts),
        "stats": {
            "buses": len(buses),
            "loads": len(loads),
            "generators": len(generators),
            "shunts": len(shunts),
            "staticVarCompensators": len(svcs),
            "branches": len(branches),
            "branches_by_type": dict(branch_type_counts),
            "danglingLines": len(dangling),
            "unmapped": {
                "loads": unmapped_loads,
                "generators": unmapped_gens,
                "shunts": unmapped_shunts,
                "staticVarCompensators": unmapped_svcs,
                "branches": unmapped_branches,
                "danglingLines": unmapped_dangling,
            },
        },
    }

    report = {
        "meta": projected["meta"],
        "stats": projected["stats"],
        "checks": {
            "bus_id_conflicts_in_busmap_inversion": bus_conflict_vl,
            "bad_bus_references": {
                "count": bad_bus_refs,
                "examples": bad_bus_ref_examples,
            },
            "warnings": {
                "total_emitted": sum(warn_counts.values()),
                "stored": len(warnings),
                "counts_by_code": dict(warn_counts),
                "examples": warnings[:max_report_examples],
            },
        },
        "samples": {
            "buses": buses[: min(10, len(buses))],
            "loads": loads[: min(10, len(loads))],
            "generators": generators[: min(10, len(generators))],
            "branches": branches[: min(10, len(branches))],
            "danglingLines": dangling[: min(10, len(dangling))],
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(projected, f, ensure_ascii=False, indent=2)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # stdout summary (compact, deterministic)
    print(
        json.dumps(
            {
                "buses": projected["stats"]["buses"],
                "gens": projected["stats"]["generators"],
                "branches": projected["stats"]["branches"],
                "dangling": projected["stats"]["danglingLines"],
                "warnings": sum(warn_counts.values()),
                "unmapped": projected["stats"]["unmapped"],
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Project IIDM equipment onto buses using busmap JSON.",
    )
    ap.add_argument("xiidm", help="Path to .xiidm (IIDM XML) file")
    ap.add_argument(
        "--busmap", required=True, help="Path to busmap JSON from 01_build_busmap.py"
    )
    ap.add_argument(
        "--out",
        default="02_projected.json",
        help="Projected output JSON (default: 02_projected.json)",
    )
    ap.add_argument(
        "--report",
        default="02_projected_report.json",
        help="Verification report JSON (default: 02_projected_report.json)",
    )
    ap.add_argument(
        "--compact-attribs",
        action="store_true",
        help="Store only a selected subset of attributes instead of all element attributes.",
    )
    ap.add_argument(
        "--max-warnings",
        type=int,
        default=5000,
        help="Max warnings stored in output (default: 5000)",
    )
    ap.add_argument(
        "--max-report-examples",
        type=int,
        default=50,
        help="Max examples stored in report (default: 50)",
    )
    args = ap.parse_args()

    project_equipment(
        xiidm_path=args.xiidm,
        busmap_path=args.busmap,
        out_path=args.out,
        report_path=args.report,
        keep_all_attribs=(not args.compact_attribs),
        max_warnings=args.max_warnings,
        max_report_examples=args.max_report_examples,
    )


if __name__ == "__main__":
    main()

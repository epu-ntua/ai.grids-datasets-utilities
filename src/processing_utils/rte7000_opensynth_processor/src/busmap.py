#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01_build_busmap.py

Build "bus view" mapping for an IIDM (XIIdm) NODE_BREAKER network by collapsing
nodes connected through CLOSED switches (open=false).

Outputs:
  - <out>.json        : busmap (node -> bus_id) + voltageLevel metadata + stats
  - <report>.json     : verification report (closed-switch invariants, counts, samples)

Key invariant (must hold):
  For every CLOSED switch within a voltageLevel, both endpoints map to the same bus_id.

Notes:
  - This script does NOT merge nodes through lines/transformers/etc. Only switches.
  - Nodes referenced by any equipment (loads/gens/lines/transformers/danglingLine/etc.)
    are included as singleton buses if not connected by closed switches.

Typical use:
  python 01_build_busmap.py rte-01Jan21-0000.xiidm --out 01_busmap.json --report 01_busmap_report.json
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


# -------------------- utilities --------------------


def parse_bool(s: Optional[str], default: bool = False) -> Tuple[bool, bool]:
    """
    Returns (value, used_default)
    Accepts: true/false, 1/0, yes/no (case-insensitive).
    """
    if s is None:
        return default, True
    v = s.strip().lower()
    if v in ("true", "1", "yes", "y"):
        return True, False
    if v in ("false", "0", "no", "n"):
        return False, False
    # unknown -> default but mark as defaulted
    return default, True


# -------------------- Union-Find --------------------


class UnionFind:
    __slots__ = ("parent", "size")

    def __init__(self) -> None:
        self.parent: Dict[int, int] = {}
        self.size: Dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.size[x] = 1

    def find(self, x: int) -> int:
        # iterative path compression
        p = self.parent.get(x)
        if p is None:
            self.add(x)
            return x
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        # compress x
        cur = x
        while self.parent[cur] != p:
            nxt = self.parent[cur]
            self.parent[cur] = p
            cur = nxt
        return p

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        # union by size
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        del self.size[rb]

    def nodes(self) -> List[int]:
        return list(self.parent.keys())


# -------------------- core --------------------


def build_busmap(
    xiidm_path: str,
    out_path: str,
    report_path: str,
    bus_id_start: int = 1,
    max_report_examples: int = 50,
) -> None:
    if not os.path.exists(xiidm_path):
        raise FileNotFoundError(xiidm_path)

    file_size = os.path.getsize(xiidm_path)
    file_hash = sha256_file(xiidm_path)

    # Per voltage level union-finds + metadata
    ufs: Dict[str, UnionFind] = {}
    vl_meta: Dict[str, Dict[str, Any]] = {}

    # Switch edge stats
    closed_switch_edges = 0
    open_switch_edges = 0
    open_attr_defaulted = 0
    switch_kind_counts = collections.Counter()

    # For verification after bus assignment: store closed switch endpoints
    # (vl_id, node1, node2, switch_id)
    closed_switch_samples: List[Tuple[str, int, int, str]] = []

    # Track nodes observed via non-switch equipment (coverage)
    nodes_seen_total = 0

    # Root / namespace info
    iidm_namespace: Optional[str] = None
    iidm_schema_version: Optional[str] = None
    network_meta: Dict[str, Any] = {}
    root_seen = False

    current_substation: Optional[str] = None
    current_voltagelevel: Optional[str] = None

    def get_uf(vl_id: str) -> UnionFind:
        uf = ufs.get(vl_id)
        if uf is None:
            uf = UnionFind()
            ufs[vl_id] = uf
        return uf

    # Stream parse
    context = ET.iterparse(xiidm_path, events=("start", "end"))
    for event, elem in context:
        tag_l = localname(elem.tag)

        if event == "start":
            if not root_seen:
                root_seen = True
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

            # context tracking
            if tag_l == "substation":
                current_substation = elem.attrib.get("id")
            elif tag_l == "voltageLevel":
                current_voltagelevel = elem.attrib.get("id")
                vl_id = current_voltagelevel
                if vl_id:
                    # store meta
                    meta = vl_meta.get(vl_id, {})
                    meta["id"] = vl_id
                    meta["substationId"] = elem.attrib.get(
                        "substationId", current_substation
                    )
                    if "nominalV" in elem.attrib:
                        try:
                            meta["nominalV"] = safe_float(elem.attrib["nominalV"])
                        except Exception:
                            meta["nominalV"] = elem.attrib["nominalV"]
                    if "topologyKind" in elem.attrib:
                        meta["topologyKind"] = elem.attrib["topologyKind"]
                    if "lowVoltageLimit" in elem.attrib:
                        meta["lowVoltageLimit"] = elem.attrib["lowVoltageLimit"]
                    if "highVoltageLimit" in elem.attrib:
                        meta["highVoltageLimit"] = elem.attrib["highVoltageLimit"]
                    meta["fictitious"] = elem.attrib.get("fictitious")
                    vl_meta[vl_id] = meta
                    # ensure uf exists
                    get_uf(vl_id)

            # handle switch (unions)
            if tag_l == "switch":
                # prefer explicit voltageLevelId if present; else use nesting context
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                if not vl_id:
                    # cannot map; skip but keep parsing
                    continue

                n1s = elem.attrib.get("node1")
                n2s = elem.attrib.get("node2")
                if n1s is None or n2s is None:
                    continue
                try:
                    n1 = safe_int(n1s)
                    n2 = safe_int(n2s)
                except Exception:
                    continue

                kind = elem.attrib.get("kind", "")
                if kind:
                    switch_kind_counts[kind] += 1

                open_val, used_default = parse_bool(
                    elem.attrib.get("open"), default=False
                )
                if used_default and "open" not in elem.attrib:
                    open_attr_defaulted += 1

                uf = get_uf(vl_id)
                uf.add(n1)
                uf.add(n2)

                if open_val:
                    open_switch_edges += 1
                else:
                    closed_switch_edges += 1
                    uf.union(n1, n2)
                    # keep sample for later invariant check (keep all; 60-80k is fine)
                    closed_switch_samples.append(
                        (vl_id, n1, n2, elem.attrib.get("id", ""))
                    )

            # include nodes referenced by equipment as singletons (coverage)
            # nested equipment uses current_voltagelevel; non-nested uses voltageLevelId*
            if tag_l in (
                "busbarSection",
                "load",
                "generator",
                "shunt",
                "staticVarCompensator",
            ):
                vl_id = elem.attrib.get("voltageLevelId") or current_voltagelevel
                if not vl_id:
                    continue
                ns = elem.attrib.get("node")
                if ns is None:
                    continue
                try:
                    n = safe_int(ns)
                except Exception:
                    continue
                get_uf(vl_id).add(n)
                nodes_seen_total += 1

            # lines / transformers / dangling lines are not unions, but we must add their terminal nodes
            if tag_l == "line":
                vl1 = elem.attrib.get("voltageLevelId1")
                vl2 = elem.attrib.get("voltageLevelId2")
                n1s = elem.attrib.get("node1")
                n2s = elem.attrib.get("node2")
                if vl1 and n1s is not None:
                    try:
                        get_uf(vl1).add(safe_int(n1s))
                        nodes_seen_total += 1
                    except Exception:
                        pass
                if vl2 and n2s is not None:
                    try:
                        get_uf(vl2).add(safe_int(n2s))
                        nodes_seen_total += 1
                    except Exception:
                        pass

            if tag_l == "twoWindingsTransformer":
                vl1 = elem.attrib.get("voltageLevelId1")
                vl2 = elem.attrib.get("voltageLevelId2")
                n1s = elem.attrib.get("node1")
                n2s = elem.attrib.get("node2")
                if vl1 and n1s is not None:
                    try:
                        get_uf(vl1).add(safe_int(n1s))
                        nodes_seen_total += 1
                    except Exception:
                        pass
                if vl2 and n2s is not None:
                    try:
                        get_uf(vl2).add(safe_int(n2s))
                        nodes_seen_total += 1
                    except Exception:
                        pass

            if tag_l == "danglingLine":
                vl = elem.attrib.get("voltageLevelId") or current_voltagelevel
                ns = elem.attrib.get("node")
                if vl and ns is not None:
                    try:
                        get_uf(vl).add(safe_int(ns))
                        nodes_seen_total += 1
                    except Exception:
                        pass

        elif event == "end":
            # end context
            if tag_l == "voltageLevel":
                current_voltagelevel = None
            elif tag_l == "substation":
                current_substation = None

            elem.clear()

    # -------------------- assign bus ids deterministically --------------------
    # For each voltageLevel, each connected component becomes one bus.
    # Deterministic ordering: sort components by (voltageLevelId, min_node_in_component)

    comp_entries: List[Tuple[str, int, int]] = []  # (vl_id, comp_min_node, comp_root)
    comp_min_by_root: Dict[Tuple[str, int], int] = {}
    # First pass: compute root + min node per component
    total_nodes = 0
    for vl_id, uf in ufs.items():
        for n in uf.parent.keys():
            total_nodes += 1
            r = uf.find(n)
            key = (vl_id, r)
            cur = comp_min_by_root.get(key)
            if cur is None or n < cur:
                comp_min_by_root[key] = n

    for (vl_id, root), min_node in comp_min_by_root.items():
        comp_entries.append((vl_id, min_node, root))
    comp_entries.sort(key=lambda t: (t[0], t[1]))

    # Assign bus ids
    root_to_bus: Dict[Tuple[str, int], int] = {}
    next_bus = bus_id_start
    for vl_id, _min_node, root in comp_entries:
        root_to_bus[(vl_id, root)] = next_bus
        next_bus += 1

    buses_count = next_bus - bus_id_start

    # Create node_to_bus map
    node_to_bus: Dict[str, Dict[str, int]] = {}
    for vl_id, uf in ufs.items():
        d: Dict[str, int] = {}
        for n in uf.parent.keys():
            r = uf.find(n)
            d[str(n)] = root_to_bus[(vl_id, r)]
        node_to_bus[vl_id] = d

    # -------------------- verification --------------------
    # Invariant: every CLOSED switch endpoint must map to same bus_id
    closed_switch_violations: List[Dict[str, Any]] = []
    for vl_id, n1, n2, sw_id in closed_switch_samples:
        b1 = node_to_bus.get(vl_id, {}).get(str(n1))
        b2 = node_to_bus.get(vl_id, {}).get(str(n2))
        if b1 is None or b2 is None or b1 != b2:
            closed_switch_violations.append(
                {
                    "voltageLevelId": vl_id,
                    "switchId": sw_id,
                    "node1": n1,
                    "node2": n2,
                    "bus1": b1,
                    "bus2": b2,
                }
            )
            if len(closed_switch_violations) >= max_report_examples:
                break

    # Summaries
    nodes_per_vl = {vl_id: len(uf.parent) for vl_id, uf in ufs.items()}
    comps_per_vl = collections.Counter(
        vl_id for (vl_id, _min_node, _root) in comp_entries
    )

    # -------------------- write outputs --------------------

    busmap = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "source_file": xiidm_path,
            "source_size_bytes": file_size,
            "source_sha256": file_hash,
            "iidm_namespace": iidm_namespace,
            "iidm_schema_version": iidm_schema_version,
            "network_meta": network_meta,
        },
        "stats": {
            "voltage_levels": len(ufs),
            "buses": buses_count,
            "mapped_nodes": total_nodes,
            "closed_switch_edges": closed_switch_edges,
            "open_switch_edges": open_switch_edges,
            "open_attr_defaulted_count": open_attr_defaulted,
            "nodes_seen_via_equipment_events": nodes_seen_total,
        },
        "voltageLevel": vl_meta,  # keyed by voltageLevelId
        "node_to_bus": node_to_bus,  # node keys as strings for JSON
    }

    report = {
        "meta": busmap["meta"],
        "stats": busmap["stats"],
        "per_voltageLevel": {
            "nodes": nodes_per_vl,
            "components": dict(comps_per_vl),
        },
        "switch_kind_counts": dict(switch_kind_counts),
        "checks": {
            "closed_switch_invariant": {
                "checked_edges": len(closed_switch_samples),
                "violations_found": len(closed_switch_violations),
                "violation_examples": closed_switch_violations,
            }
        },
        "component_ordering_sample": [
            {
                "voltageLevelId": vl_id,
                "min_node": mn,
                "root": root,
                "bus_id": root_to_bus[(vl_id, root)],
            }
            for (vl_id, mn, root) in comp_entries[: min(30, len(comp_entries))]
        ],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(busmap, f, ensure_ascii=False, indent=2)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # stdout summary (matches your earlier style)
    print(
        json.dumps(
            {
                "voltage_levels": busmap["stats"]["voltage_levels"],
                "buses": busmap["stats"]["buses"],
                "mapped_nodes": busmap["stats"]["mapped_nodes"],
                "closed_switch_edges": busmap["stats"]["closed_switch_edges"],
                "open_switch_edges": busmap["stats"]["open_switch_edges"],
                "closed_switch_invariant_violations": report["checks"][
                    "closed_switch_invariant"
                ]["violations_found"],
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Collapse NODE_BREAKER nodes connected by CLOSED switches into buses (bus view).",
    )
    ap.add_argument("xiidm", help="Path to .xiidm (IIDM XML) file")
    ap.add_argument(
        "--out",
        default="01_busmap.json",
        help="Output busmap JSON (default: 01_busmap.json)",
    )
    ap.add_argument(
        "--report",
        default="01_busmap_report.json",
        help="Verification report JSON (default: 01_busmap_report.json)",
    )
    ap.add_argument(
        "--bus-id-start", type=int, default=1, help="First bus id (default: 1)"
    )
    ap.add_argument(
        "--max-report-examples",
        type=int,
        default=50,
        help="Max violation examples stored (default: 50)",
    )
    args = ap.parse_args()

    build_busmap(
        xiidm_path=args.xiidm,
        out_path=args.out,
        report_path=args.report,
        bus_id_start=args.bus_id_start,
        max_report_examples=args.max_report_examples,
    )


if __name__ == "__main__":
    main()

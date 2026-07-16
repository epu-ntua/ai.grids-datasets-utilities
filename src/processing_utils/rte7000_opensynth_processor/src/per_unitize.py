#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_per_unitize.py

Convert projected JSON (from 03_boundary_dangling.py) from physical units to per-unit
for impedances/admittances, using:
  Zbase(ohm) = (Vbase_kV^2) / baseMVA
  r_pu = r_ohm / Zbase
  x_pu = x_ohm / Zbase
  g_pu = g_S * Zbase
  b_pu = b_S * Zbase

Scope:
  - Buses: attach base_kV, Zbase_ohm, Ybase_S
  - Lines: convert r/x; convert g1/b1 and g2/b2 per-side; compute symmetric totals and
           compute bus-shunt correction terms to preserve asymmetry under MATPOWER's
           symmetric branch-shunt model (stored as bus pu shunt additions).
  - 2W Transformers: convert r/x; convert optional g/b (magnetizing shunt) using ratedU1
                     (preferred) or from-bus nominalV.
  - Shunts created/represented as constant admittance (g,b): convert to pu as bus shunts.
    (shuntLinearModel etc. are preserved but not converted unless explicit g/b exist.)
  - SVC: convert bMin/bMax to pu (if parseable), using bus baseKV.

Inputs:
  - --in 03_projected_boundary.json

Outputs:
  - --out 04_pu.json
  - --report 04_pu_report.json

Typical use:
  python 04_per_unitize.py --in 03_projected_boundary.json --out 04_pu.json --report 04_pu_report.json --base-mva 100
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from .common import ensure_list, iso_now, sha256_file
    from .common import safe_float_opt as safe_float
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import ensure_list, iso_now, sha256_file  # type: ignore
    from common import safe_float_opt as safe_float


# -------------------- utilities --------------------


def warn_add(
    warnings: List[Dict[str, Any]],
    warn_counts: Dict[str, int],
    code: str,
    detail: Dict[str, Any],
    max_warnings: int,
) -> None:
    warn_counts[code] = warn_counts.get(code, 0) + 1
    if len(warnings) < max_warnings:
        d = {"code": code}
        d.update(detail)
        warnings.append(d)


def rel_err(a: float, b: float, eps: float = 1e-12) -> float:
    return abs(a - b) / max(eps, abs(a), abs(b))


# -------------------- per-unit helpers --------------------


def zbase_ohm(v_kv: float, base_mva: float) -> float:
    # Zbase in ohms when V in kV and S in MVA
    return (v_kv * v_kv) / base_mva


def ybase_siemens(v_kv: float, base_mva: float) -> float:
    # Ybase = 1/Zbase in Siemens
    zb = zbase_ohm(v_kv, base_mva)
    return 1.0 / zb if zb != 0.0 else 0.0


def ohm_to_pu(z_ohm: float, v_kv: float, base_mva: float) -> float:
    return z_ohm / zbase_ohm(v_kv, base_mva)


def siemens_to_pu(y_s: float, v_kv: float, base_mva: float) -> float:
    # pu admittance on system base is y * Zbase
    return y_s * zbase_ohm(v_kv, base_mva)


def pu_to_ohm(z_pu: float, v_kv: float, base_mva: float) -> float:
    return z_pu * zbase_ohm(v_kv, base_mva)


def pu_to_siemens(y_pu: float, v_kv: float, base_mva: float) -> float:
    zb = zbase_ohm(v_kv, base_mva)
    return y_pu / zb if zb != 0.0 else 0.0


# -------------------- core --------------------


def per_unitize(
    in_path: str,
    out_path: str,
    report_path: str,
    base_mva: float = 100.0,
    max_warnings: int = 5000,
    max_report_examples: int = 50,
    kv_mismatch_tol: float = 1e-3,  # relative
) -> None:
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)

    in_size = os.path.getsize(in_path)
    in_hash = sha256_file(in_path)

    with open(in_path, "r", encoding="utf-8") as f:
        data_in = json.load(f)

    out = copy.deepcopy(data_in)

    buses: List[Dict[str, Any]] = ensure_list(out.get("buses"))
    branches: List[Dict[str, Any]] = ensure_list(out.get("branches"))
    shunts: List[Dict[str, Any]] = ensure_list(out.get("shunts"))
    svcs: List[Dict[str, Any]] = ensure_list(out.get("staticVarCompensators"))
    loads: List[Dict[str, Any]] = ensure_list(out.get("loads"))
    gens: List[Dict[str, Any]] = ensure_list(out.get("generators"))

    # Warnings (append to existing)
    warnings: List[Dict[str, Any]] = ensure_list(out.get("warnings"))
    warn_counts: Dict[str, int] = dict(out.get("warning_counts") or {})

    # Bus lookup + per-bus shunt accumulators (pu)
    bus_by_id: Dict[int, Dict[str, Any]] = {}
    bus_pu_meta: Dict[int, Dict[str, Any]] = {}
    bus_shunt_asym: Dict[
        int, Tuple[float, float]
    ] = {}  # (gs_pu, bs_pu) from line asym correction
    bus_shunt_const: Dict[
        int, Tuple[float, float]
    ] = {}  # (gs_pu, bs_pu) from explicit shunt-like elements

    for b in buses:
        bid = b.get("bus_id")
        if not isinstance(bid, int):
            continue
        bus_by_id[bid] = b

    def bus_base_kv(bid: int) -> Optional[float]:
        b = bus_by_id.get(bid)
        if not b:
            return None
        # nominalV expected in kV
        v = b.get("nominalV")
        v_f = safe_float(v)
        return v_f

    def add_bus_shunt_asym(bid: int, gs: float, bs: float) -> None:
        g0, b0 = bus_shunt_asym.get(bid, (0.0, 0.0))
        bus_shunt_asym[bid] = (g0 + gs, b0 + bs)

    def add_bus_shunt_const(bid: int, gs: float, bs: float) -> None:
        g0, b0 = bus_shunt_const.get(bid, (0.0, 0.0))
        bus_shunt_const[bid] = (g0 + gs, b0 + bs)

    # Attach bus pu meta (base_kV, Zbase, Ybase)
    for bid, b in bus_by_id.items():
        vkv = bus_base_kv(bid)
        if vkv is None:
            warn_add(
                warnings,
                warn_counts,
                "BUS_MISSING_NOMINALV",
                {"bus_id": bid},
                max_warnings,
            )
            continue
        zb = zbase_ohm(vkv, base_mva)
        yb = ybase_siemens(vkv, base_mva)
        b.setdefault("pu", {})
        b["pu"].update(
            {
                "baseMVA": base_mva,
                "base_kV": vkv,
                "Zbase_ohm": zb,
                "Ybase_S": yb,
            }
        )
        bus_pu_meta[bid] = b["pu"]

    # -------------------- branches conversion --------------------

    branch_roundtrip: List[Dict[str, Any]] = []
    kv_mismatch_warnings = 0

    def record_roundtrip(
        kind: str,
        bid_from: int,
        z_vkv: float,
        orig_r: float,
        orig_x: float,
        r_pu: float,
        x_pu: float,
        branch_id: str,
    ) -> None:
        if len(branch_roundtrip) >= max_report_examples:
            return
        r_rt = pu_to_ohm(r_pu, z_vkv, base_mva)
        x_rt = pu_to_ohm(x_pu, z_vkv, base_mva)
        branch_roundtrip.append(
            {
                "kind": kind,
                "id": branch_id,
                "v_kV_used": z_vkv,
                "r_ohm_orig": orig_r,
                "r_ohm_roundtrip": r_rt,
                "r_abs_err": abs(orig_r - r_rt),
                "r_rel_err": rel_err(orig_r, r_rt),
                "x_ohm_orig": orig_x,
                "x_ohm_roundtrip": x_rt,
                "x_abs_err": abs(orig_x - x_rt),
                "x_rel_err": rel_err(orig_x, x_rt),
            }
        )

    for br in branches:
        br_type = br.get("type", "branch")
        br_id = str(br.get("id", ""))

        fbus = br.get("fbus")
        tbus = br.get("tbus")
        if not isinstance(fbus, int) or not isinstance(tbus, int):
            warn_add(
                warnings,
                warn_counts,
                "BRANCH_MISSING_ENDPOINT",
                {"id": br_id, "type": br_type},
                max_warnings,
            )
            continue

        v_from = bus_base_kv(fbus)
        v_to = bus_base_kv(tbus)

        attrib = br.get("attrib") or {}

        # parse series r/x
        r_ohm = safe_float(attrib.get("r"))
        x_ohm = safe_float(attrib.get("x"))

        br.setdefault("pu", {})
        br["pu"]["baseMVA"] = base_mva

        # Choose conversion base for series impedance
        v_used: Optional[float] = None

        if br_type == "twoWindingsTransformer":
            # Prefer ratedU1, else from-bus nominalV, else to-bus nominalV
            rated_u1 = safe_float(attrib.get("ratedU1"))
            v_used = rated_u1 or v_from or v_to
            if v_used is None:
                warn_add(
                    warnings,
                    warn_counts,
                    "TWTR_NO_BASEKV",
                    {"id": br_id, "fbus": fbus, "tbus": tbus},
                    max_warnings,
                )
            else:
                br["pu"]["base_kV_used_for_series"] = v_used
                if r_ohm is not None:
                    br["pu"]["r_pu"] = ohm_to_pu(r_ohm, v_used, base_mva)
                if x_ohm is not None:
                    br["pu"]["x_pu"] = ohm_to_pu(x_ohm, v_used, base_mva)

                # Optional magnetizing admittance g/b (Siemens)
                g_s = safe_float(attrib.get("g"))
                b_s = safe_float(attrib.get("b"))
                if g_s is not None:
                    br["pu"]["g_pu"] = siemens_to_pu(g_s, v_used, base_mva)
                if b_s is not None:
                    br["pu"]["b_pu"] = siemens_to_pu(b_s, v_used, base_mva)

                if (
                    r_ohm is not None
                    and x_ohm is not None
                    and "r_pu" in br["pu"]
                    and "x_pu" in br["pu"]
                ):
                    record_roundtrip(
                        "twoWindingsTransformer",
                        fbus,
                        v_used,
                        r_ohm,
                        x_ohm,
                        br["pu"]["r_pu"],
                        br["pu"]["x_pu"],
                        br_id,
                    )

        elif br_type in ("line", "danglingLine"):
            # Lines should be same voltage; use from-bus if available else to-bus.
            v_used = v_from or v_to
            if v_used is None:
                warn_add(
                    warnings,
                    warn_counts,
                    "LINE_NO_BASEKV",
                    {"id": br_id, "type": br_type, "fbus": fbus, "tbus": tbus},
                    max_warnings,
                )
                continue

            if v_from is not None and v_to is not None:
                if rel_err(v_from, v_to) > kv_mismatch_tol:
                    kv_mismatch_warnings += 1
                    warn_add(
                        warnings,
                        warn_counts,
                        "LINE_BASEKV_MISMATCH",
                        {
                            "id": br_id,
                            "fbus": fbus,
                            "tbus": tbus,
                            "v_from_kV": v_from,
                            "v_to_kV": v_to,
                        },
                        max_warnings,
                    )

            br["pu"]["base_kV_used_for_series"] = v_used
            if r_ohm is not None:
                br["pu"]["r_pu"] = ohm_to_pu(r_ohm, v_used, base_mva)
            if x_ohm is not None:
                br["pu"]["x_pu"] = ohm_to_pu(x_ohm, v_used, base_mva)

            if (
                r_ohm is not None
                and x_ohm is not None
                and "r_pu" in br["pu"]
                and "x_pu" in br["pu"]
            ):
                record_roundtrip(
                    br_type,
                    fbus,
                    v_used,
                    r_ohm,
                    x_ohm,
                    br["pu"]["r_pu"],
                    br["pu"]["x_pu"],
                    br_id,
                )

            # Line shunts are per-side (Siemens). Convert per-side using each side's baseKV.
            if br_type == "line":
                g1_s = safe_float(attrib.get("g1"))
                b1_s = safe_float(attrib.get("b1"))
                g2_s = safe_float(attrib.get("g2"))
                b2_s = safe_float(attrib.get("b2"))

                if v_from is None or v_to is None:
                    # still convert what we can; missing side => warn
                    if v_from is None:
                        warn_add(
                            warnings,
                            warn_counts,
                            "LINE_NO_BASEKV_FROM_SIDE",
                            {"id": br_id, "fbus": fbus},
                            max_warnings,
                        )
                    if v_to is None:
                        warn_add(
                            warnings,
                            warn_counts,
                            "LINE_NO_BASEKV_TO_SIDE",
                            {"id": br_id, "tbus": tbus},
                            max_warnings,
                        )

                g1_pu = (
                    siemens_to_pu(g1_s, v_from, base_mva)
                    if (g1_s is not None and v_from is not None)
                    else None
                )
                b1_pu = (
                    siemens_to_pu(b1_s, v_from, base_mva)
                    if (b1_s is not None and v_from is not None)
                    else None
                )
                g2_pu = (
                    siemens_to_pu(g2_s, v_to, base_mva)
                    if (g2_s is not None and v_to is not None)
                    else None
                )
                b2_pu = (
                    siemens_to_pu(b2_s, v_to, base_mva)
                    if (b2_s is not None and v_to is not None)
                    else None
                )

                br["pu"]["g1_pu"] = g1_pu
                br["pu"]["b1_pu"] = b1_pu
                br["pu"]["g2_pu"] = g2_pu
                br["pu"]["b2_pu"] = b2_pu

                # Preserve asymmetry under MATPOWER-style symmetric branch shunt:
                # BR_G = g1+g2, BR_B = b1+b2, each split half/half internally.
                # Bus correction adds: (g1 - (g_total/2)) at from bus, (g2 - (g_total/2)) at to bus, similarly for b.
                if g1_pu is not None and g2_pu is not None:
                    g_total = g1_pu + g2_pu
                    g_half = 0.5 * g_total
                    g_corr_from = g1_pu - g_half
                    g_corr_to = g2_pu - g_half
                    br["pu"]["g_total_pu"] = g_total
                    br["pu"]["g_corr_from_bus_pu"] = g_corr_from
                    br["pu"]["g_corr_to_bus_pu"] = g_corr_to
                    add_bus_shunt_asym(fbus, g_corr_from, 0.0)
                    add_bus_shunt_asym(tbus, g_corr_to, 0.0)
                if b1_pu is not None and b2_pu is not None:
                    b_total = b1_pu + b2_pu
                    b_half = 0.5 * b_total
                    b_corr_from = b1_pu - b_half
                    b_corr_to = b2_pu - b_half
                    br["pu"]["b_total_pu"] = b_total
                    br["pu"]["b_corr_from_bus_pu"] = b_corr_from
                    br["pu"]["b_corr_to_bus_pu"] = b_corr_to
                    add_bus_shunt_asym(fbus, 0.0, b_corr_from)
                    add_bus_shunt_asym(tbus, 0.0, b_corr_to)

        else:
            # Unknown branch types: attempt series conversion on from-bus baseKV
            v_used = v_from or v_to
            if v_used is None:
                warn_add(
                    warnings,
                    warn_counts,
                    "BRANCH_UNKNOWN_NO_BASEKV",
                    {"id": br_id, "type": br_type},
                    max_warnings,
                )
                continue
            br["pu"]["base_kV_used_for_series"] = v_used
            if r_ohm is not None:
                br["pu"]["r_pu"] = ohm_to_pu(r_ohm, v_used, base_mva)
            if x_ohm is not None:
                br["pu"]["x_pu"] = ohm_to_pu(x_ohm, v_used, base_mva)

    # -------------------- shunts conversion (constant admittance only) --------------------

    shunt_roundtrip: List[Dict[str, Any]] = []
    converted_shunts = 0
    skipped_shunts = 0

    for sh in shunts:
        sh_id = str(sh.get("id", ""))
        bid = sh.get("bus_id")
        if not isinstance(bid, int):
            warn_add(warnings, warn_counts, "SHUNT_NO_BUS", {"id": sh_id}, max_warnings)
            skipped_shunts += 1
            continue

        vkv = bus_base_kv(bid)
        if vkv is None:
            warn_add(
                warnings,
                warn_counts,
                "SHUNT_NO_BASEKV",
                {"id": sh_id, "bus_id": bid},
                max_warnings,
            )
            skipped_shunts += 1
            continue

        attrib = sh.get("attrib") or {}
        g_s = safe_float(attrib.get("g"))
        b_s = safe_float(attrib.get("b"))

        # Only convert if explicit g/b exist; otherwise preserve raw.
        if g_s is None and b_s is None:
            skipped_shunts += 1
            continue

        sh.setdefault("pu", {})
        sh["pu"]["baseMVA"] = base_mva
        sh["pu"]["base_kV_used"] = vkv
        if g_s is not None:
            g_pu = siemens_to_pu(g_s, vkv, base_mva)
            sh["pu"]["g_pu"] = g_pu
        if b_s is not None:
            b_pu = siemens_to_pu(b_s, vkv, base_mva)
            sh["pu"]["b_pu"] = b_pu

        add_bus_shunt_const(bid, sh["pu"].get("g_pu", 0.0), sh["pu"].get("b_pu", 0.0))
        converted_shunts += 1

        if len(shunt_roundtrip) < max_report_examples:
            rec: Dict[str, Any] = {"id": sh_id, "bus_id": bid, "v_kV_used": vkv}
            if "g_pu" in sh["pu"] and g_s is not None:
                g_rt = pu_to_siemens(sh["pu"]["g_pu"], vkv, base_mva)
                rec.update(
                    {
                        "g_S_orig": g_s,
                        "g_S_roundtrip": g_rt,
                        "g_abs_err": abs(g_s - g_rt),
                        "g_rel_err": rel_err(g_s, g_rt),
                    }
                )
            if "b_pu" in sh["pu"] and b_s is not None:
                b_rt = pu_to_siemens(sh["pu"]["b_pu"], vkv, base_mva)
                rec.update(
                    {
                        "b_S_orig": b_s,
                        "b_S_roundtrip": b_rt,
                        "b_abs_err": abs(b_s - b_rt),
                        "b_rel_err": rel_err(b_s, b_rt),
                    }
                )
            shunt_roundtrip.append(rec)

    # -------------------- SVC conversion (bMin/bMax in Siemens) --------------------

    converted_svcs = 0
    skipped_svcs = 0
    svc_examples: List[Dict[str, Any]] = []

    for svc in svcs:
        svc_id = str(svc.get("id", ""))
        bid = svc.get("bus_id")
        if not isinstance(bid, int):
            warn_add(warnings, warn_counts, "SVC_NO_BUS", {"id": svc_id}, max_warnings)
            skipped_svcs += 1
            continue

        vkv = bus_base_kv(bid)
        if vkv is None:
            warn_add(
                warnings,
                warn_counts,
                "SVC_NO_BASEKV",
                {"id": svc_id, "bus_id": bid},
                max_warnings,
            )
            skipped_svcs += 1
            continue

        attrib = svc.get("attrib") or {}
        bmin_s = safe_float(attrib.get("bMin"))
        bmax_s = safe_float(attrib.get("bMax"))
        if bmin_s is None and bmax_s is None:
            skipped_svcs += 1
            continue

        svc.setdefault("pu", {})
        svc["pu"]["baseMVA"] = base_mva
        svc["pu"]["base_kV_used"] = vkv
        if bmin_s is not None:
            svc["pu"]["bMin_pu"] = siemens_to_pu(bmin_s, vkv, base_mva)
        if bmax_s is not None:
            svc["pu"]["bMax_pu"] = siemens_to_pu(bmax_s, vkv, base_mva)

        converted_svcs += 1
        if len(svc_examples) < max_report_examples:
            svc_examples.append(
                {
                    "id": svc_id,
                    "bus_id": bid,
                    "v_kV_used": vkv,
                    "bMin_S": bmin_s,
                    "bMin_pu": svc["pu"].get("bMin_pu"),
                    "bMax_S": bmax_s,
                    "bMax_pu": svc["pu"].get("bMax_pu"),
                }
            )

    # -------------------- attach bus shunt totals (pu) --------------------

    buses_with_pu = 0
    for bid, b in bus_by_id.items():
        vkv = bus_base_kv(bid)
        if vkv is None:
            continue
        g_asym, b_asym = bus_shunt_asym.get(bid, (0.0, 0.0))
        g_const, b_const = bus_shunt_const.get(bid, (0.0, 0.0))
        b.setdefault("pu", {})
        b["pu"]["bus_shunt_pu"] = {
            "gs_asym_from_lines_pu": g_asym,
            "bs_asym_from_lines_pu": b_asym,
            "gs_from_const_shunts_pu": g_const,
            "bs_from_const_shunts_pu": b_const,
            "gs_total_pu": g_asym + g_const,
            "bs_total_pu": b_asym + b_const,
        }
        buses_with_pu += 1

    # -------------------- meta + outputs --------------------

    out.setdefault("meta", {})
    out["meta"].setdefault("transforms", [])
    out["meta"]["transforms"].append(
        {
            "step": "04_per_unitize",
            "created_at": iso_now(),
            "input": {"path": in_path, "size_bytes": in_size, "sha256": in_hash},
            "params": {"baseMVA": base_mva},
            "notes": [
                "Impedances converted using Zbase(VkV, baseMVA).",
                "Line asym shunts preserved via stored bus correction terms (pu).",
                "Only explicit constant (g,b) shunts converted; shuntLinearModel preserved without conversion.",
                "Transformer series conversion uses ratedU1 if available, else from-bus nominalV.",
            ],
        }
    )

    out["baseMVA"] = base_mva
    out["warnings"] = warnings
    out["warning_counts"] = warn_counts

    # Report aggregation
    report = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "input": {"path": in_path, "size_bytes": in_size, "sha256": in_hash},
            "params": {"baseMVA": base_mva},
        },
        "stats": {
            "buses": len(buses),
            "buses_with_pu_meta": buses_with_pu,
            "branches": len(branches),
            "shunts": len(shunts),
            "svcs": len(svcs),
            "loads": len(loads),
            "generators": len(gens),
            "converted_shunts": converted_shunts,
            "skipped_shunts": skipped_shunts,
            "converted_svcs": converted_svcs,
            "skipped_svcs": skipped_svcs,
            "line_basekv_mismatch_warnings": kv_mismatch_warnings,
        },
        "checks": {
            "roundtrip_branch_examples": branch_roundtrip,
            "roundtrip_shunt_examples": shunt_roundtrip,
        },
        "samples": {
            "svc_pu_examples": svc_examples,
            "bus_shunt_totals_first": [
                {
                    "bus_id": b.get("bus_id"),
                    "bus_shunt_pu": (b.get("pu") or {}).get("bus_shunt_pu"),
                }
                for b in buses[: min(20, len(buses))]
            ],
        },
        "warnings_summary": {
            "total_emitted": sum(warn_counts.values()),
            "stored": len(warnings),
            "counts_by_code": warn_counts,
            "examples": warnings[:max_report_examples],
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "baseMVA": base_mva,
                "buses": len(buses),
                "branches": len(branches),
                "converted_shunts": converted_shunts,
                "converted_svcs": converted_svcs,
                "warnings": report["warnings_summary"]["total_emitted"],
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Convert projected boundary model (JSON) to per-unit using baseMVA and bus nominalV (kV).",
    )
    ap.add_argument(
        "--in",
        dest="inp",
        required=True,
        help="Input JSON (from 03_boundary_dangling.py)",
    )
    ap.add_argument(
        "--out", default="04_pu.json", help="Output JSON (default: 04_pu.json)"
    )
    ap.add_argument(
        "--report",
        default="04_pu_report.json",
        help="Report JSON (default: 04_pu_report.json)",
    )
    ap.add_argument(
        "--base-mva", type=float, default=100.0, help="System baseMVA (default: 100)"
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
    ap.add_argument(
        "--kv-mismatch-tol",
        type=float,
        default=1e-3,
        help="Relative tolerance for line end kV mismatch (default: 1e-3)",
    )
    args = ap.parse_args()

    per_unitize(
        in_path=args.inp,
        out_path=args.out,
        report_path=args.report,
        base_mva=args.base_mva,
        max_warnings=args.max_warnings,
        max_report_examples=args.max_report_examples,
        kv_mismatch_tol=args.kv_mismatch_tol,
    )


if __name__ == "__main__":
    main()

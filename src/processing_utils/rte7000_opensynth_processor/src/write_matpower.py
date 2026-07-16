#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
05_write_matpower.py

Write a MATPOWER .m case from 04_pu.json (output of 04_per_unitize.py).

Inputs:
  --in  04_pu.json

Outputs:
  --out     case.m
  --sidecar 05_matpower_sidecar.json   (ID mappings, indices, provenance)
  --report  05_write_report.json       (sanity + policy choices)

Policies (default = minimal assumptions):
  - Bus numbering is remapped to contiguous 1..N (stable by original bus_id sort).
  - Bus types default to PQ (1). If --assign-slack is enabled, one REF (3) is created.
  - Generator rows are created for each generator object; PG/QG default to 0 if missing.
  - Branch rows created for each branch; TAP/SHIFT default to 1/0 if not inferable.
  - Bus shunts: uses bus.pu.bus_shunt_pu.{gs_total_pu, bs_total_pu} if present.
    Converts to MATPOWER GS/BS via GS = g_pu * baseMVA, BS = b_pu * baseMVA.
  - Line branch shunt: uses branch.pu.b_total_pu if present; else 0.
  - Voltage limits: derived from bus nominalV and voltageLevel limits if present; else 0.9..1.1.

This script aims to produce a syntactically valid MATPOWER case, not a solved operating point.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

try:
    from .common import ensure_list, iso_now, sha256_file
    from .common import safe_float_opt as safe_float
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import ensure_list, iso_now, sha256_file  # type: ignore
    from common import safe_float_opt as safe_float


# -------------------- utilities --------------------


def safe_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None


def get_in(d: Dict[str, Any], path: List[str], default: Any = None) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def rel_err(a: float, b: float, eps: float = 1e-12) -> float:
    return abs(a - b) / max(eps, abs(a), abs(b))


def fmt_num(x: Any) -> str:
    if x is None:
        return "0"
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, int):
        return str(x)
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return "0"
        if abs(v) < 1e-15:
            return "0"
        # full-precision-ish, MATLAB-readable, preserves tiny values
        return format(v, ".17g")
    except Exception:
        return "0"


def matlab_matrix(rows: List[List[Any]], indent: str = "    ") -> str:
    """
    Render a MATLAB numeric matrix using semicolon row separators.
    """
    if not rows:
        return "[]"
    lines = ["["]
    for r in rows:
        lines.append(indent + " ".join(fmt_num(x) for x in r) + ";")
    lines.append("]")
    return "\n".join(lines)


def matlab_cellstr(lines: List[str], indent: str = "    ") -> str:
    """
    Render MATLAB cell array of strings: {'a'; 'b'; ...}
    """
    if not lines:
        return "{}"
    out = ["{"]
    for s in lines:
        s2 = s.replace("'", "''")
        out.append(f"{indent}'{s2}';")
    out.append("}")
    return "\n".join(out)


# -------------------- extraction helpers --------------------


def parse_bus_limits_pu(bus: Dict[str, Any]) -> Tuple[float, float]:
    """
    Attempt to compute VMIN/VMAX in pu from voltage limits (kV) if present,
    otherwise default 0.9/1.1.
    """
    base_kv = safe_float(bus.get("nominalV"))
    if base_kv is None or base_kv == 0.0:
        return 0.9, 1.1

    # bus carries voltageLevelId; but limits may be embedded in bus dict depending on earlier steps
    # We support common fields:
    low_kv = safe_float(bus.get("lowVoltageLimit"))
    high_kv = safe_float(bus.get("highVoltageLimit"))

    if low_kv is not None and high_kv is not None and low_kv > 0 and high_kv > 0:
        return low_kv / base_kv, high_kv / base_kv

    # try from bus["pu"] if someone inserted:
    low_kv = safe_float(get_in(bus, ["pu", "lowVoltageLimit_kV"]))
    high_kv = safe_float(get_in(bus, ["pu", "highVoltageLimit_kV"]))
    if low_kv is not None and high_kv is not None and low_kv > 0 and high_kv > 0:
        return low_kv / base_kv, high_kv / base_kv

    return 0.9, 1.1


def infer_tap_shift(branch: Dict[str, Any]) -> Tuple[float, float, Dict[str, Any]]:
    """
    Best-effort extraction of transformer TAP and SHIFT from recorded tap changer info.
    Returns (tap, shift_deg, provenance_dict).
    """
    prov: Dict[str, Any] = {"tap_source": None, "shift_source": None}

    if branch.get("type") != "twoWindingsTransformer":
        return 1.0, 0.0, prov

    # 02_project_equipment stored tap changer dicts under keys ratioTapChanger/phaseTapChanger.
    rtc = branch.get("ratioTapChanger") or {}
    ptc = branch.get("phaseTapChanger") or {}

    tap = 1.0
    shift = 0.0

    # Ratio: look at selected_step fields for common keys.
    rtc_sel = rtc.get("selected_step") if isinstance(rtc, dict) else None
    if isinstance(rtc_sel, dict):
        for k in ("rho", "ratio", "tapRatio", "r", "value"):
            v = safe_float(rtc_sel.get(k))
            if v is not None and v > 0:
                tap = v
                prov["tap_source"] = f"ratioTapChanger.selected_step.{k}"
                break

    # If not in selected_step, maybe in attrib
    if prov["tap_source"] is None and isinstance(rtc, dict):
        rtc_attrib = rtc.get("attrib")
        if isinstance(rtc_attrib, dict):
            for k in ("tapRatio", "rho"):
                v = safe_float(rtc_attrib.get(k))
                if v is not None and v > 0:
                    tap = v
                    prov["tap_source"] = f"ratioTapChanger.attrib.{k}"
                    break

    # Phase shift: look for alpha/phase keys; assume degrees if provided.
    ptc_sel = ptc.get("selected_step") if isinstance(ptc, dict) else None
    if isinstance(ptc_sel, dict):
        for k in ("alpha", "phaseShift", "shift", "angle", "value"):
            v = safe_float(ptc_sel.get(k))
            if v is not None:
                shift = v
                prov["shift_source"] = f"phaseTapChanger.selected_step.{k}"
                break

    if prov["shift_source"] is None and isinstance(ptc, dict):
        ptc_attrib = ptc.get("attrib")
        if isinstance(ptc_attrib, dict):
            for k in ("phaseShift", "alpha"):
                v = safe_float(ptc_attrib.get(k))
                if v is not None:
                    shift = v
                    prov["shift_source"] = f"phaseTapChanger.attrib.{k}"
                    break

    return tap, shift, prov


def extract_gen_pq(gen: Dict[str, Any]) -> Tuple[float, float, Dict[str, Any]]:
    """
    Best-effort extract PG/QG from generator attrib. Defaults to 0/0.
    Returns (pg, qg, provenance).
    """
    a = gen.get("attrib") or {}
    prov = {"pg_source": None, "qg_source": None}

    # PG candidates
    for k in ("targetP", "p", "p0", "P", "pg"):
        v = safe_float(a.get(k))
        if v is not None:
            prov["pg_source"] = f"attrib.{k}"
            pg = v
            break
    else:
        pg = 0.0

    # QG candidates
    for k in ("targetQ", "q", "q0", "Q", "qg"):
        v = safe_float(a.get(k))
        if v is not None:
            prov["qg_source"] = f"attrib.{k}"
            qg = v
            break
    else:
        qg = 0.0

    return pg, qg, prov


def extract_load_pq(load: Dict[str, Any]) -> Tuple[float, float, Dict[str, Any]]:
    """
    Best-effort extract PD/QD from load attrib. Defaults to 0/0.
    Returns (pd, qd, provenance).
    """
    a = load.get("attrib") or {}
    prov = {"pd_source": None, "qd_source": None}

    for k in ("p0", "p", "P", "pd"):
        v = safe_float(a.get(k))
        if v is not None:
            prov["pd_source"] = f"attrib.{k}"
            pd = v
            break
    else:
        pd = 0.0

    for k in ("q0", "q", "Q", "qd"):
        v = safe_float(a.get(k))
        if v is not None:
            prov["qd_source"] = f"attrib.{k}"
            qd = v
            break
    else:
        qd = 0.0

    return pd, qd, prov


# -------------------- core writer --------------------


def write_matpower(
    in_path: str,
    out_m_path: str,
    sidecar_path: str,
    report_path: str,
    case_name: str = "rte_case",
    assign_slack: bool = False,
    slack_bus_id: Optional[int] = None,
    default_q_limits: float = 9999.0,
) -> None:
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)

    in_size = os.path.getsize(in_path)
    in_hash = sha256_file(in_path)

    with open(in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    base_mva = (
        safe_float(data.get("baseMVA"))
        or safe_float(get_in(data, ["meta", "transforms", -1, "params", "baseMVA"]))
        or 100.0
    )

    buses_in = ensure_list(data.get("buses"))
    loads_in = ensure_list(data.get("loads"))
    gens_in = ensure_list(data.get("generators"))
    branches_in = ensure_list(data.get("branches"))

    # Deterministic bus ordering by original bus_id
    bus_rows_sorted = sorted(
        [b for b in buses_in if isinstance(b.get("bus_id"), int)],
        key=lambda b: int(b["bus_id"]),
    )

    # Map original bus_id -> MATPOWER BUS_I (1..N)
    bus_id_to_i: Dict[int, int] = {}
    i_to_bus_id: Dict[int, int] = {}
    for idx, b in enumerate(bus_rows_sorted, start=1):
        bid = int(b["bus_id"])
        bus_id_to_i[bid] = idx
        i_to_bus_id[idx] = bid

    # Aggregate loads per bus (PD/QD)
    pd_sum: Dict[int, float] = {bid: 0.0 for bid in bus_id_to_i.keys()}
    qd_sum: Dict[int, float] = {bid: 0.0 for bid in bus_id_to_i.keys()}
    load_prov: Dict[str, Any] = {}
    for ld in loads_in:
        bid = ld.get("bus_id")
        if not isinstance(bid, int) or bid not in bus_id_to_i:
            continue
        pd, qd, prov = extract_load_pq(ld)
        pd_sum[bid] += pd
        qd_sum[bid] += qd
        if len(load_prov) < 50:
            load_prov[ld.get("id", f"load@{bid}")] = {"bus_id": bid, **prov}

    # Bus shunts (GS/BS) from pu totals if present
    gs_bus_mw: Dict[int, float] = {bid: 0.0 for bid in bus_id_to_i.keys()}
    bs_bus_mvar: Dict[int, float] = {bid: 0.0 for bid in bus_id_to_i.keys()}
    shunt_sources: List[Dict[str, Any]] = []

    for b in bus_rows_sorted:
        bid = int(b["bus_id"])
        sh = get_in(b, ["pu", "bus_shunt_pu"], {})
        g_pu = safe_float(sh.get("gs_total_pu")) or 0.0
        b_pu = safe_float(sh.get("bs_total_pu")) or 0.0
        gs_bus_mw[bid] = g_pu * base_mva
        bs_bus_mvar[bid] = b_pu * base_mva
        if len(shunt_sources) < 50:
            shunt_sources.append(
                {
                    "bus_id": bid,
                    "gs_pu": g_pu,
                    "bs_pu": b_pu,
                    "GS_MW": gs_bus_mw[bid],
                    "BS_MVAr": bs_bus_mvar[bid],
                }
            )

    # Determine bus types (default PQ)
    # If assign_slack: set one REF bus.
    # Otherwise: keep all PQ (1). (Optionally mark PV if generator has regulatorOn true.)
    bus_type: Dict[int, int] = {bid: 1 for bid in bus_id_to_i.keys()}  # 1 = PQ

    # Mark PV buses where generator exists & voltageRegulatorOn true
    for g in gens_in:
        bid = g.get("bus_id")
        if not isinstance(bid, int) or bid not in bus_type:
            continue
        vr = parse_bool(get_in(g, ["attrib", "voltageRegulatorOn"]))
        if vr is True:
            bus_type[bid] = 2  # PV

    chosen_slack_bus: Optional[int] = None
    if assign_slack:
        if slack_bus_id is not None and slack_bus_id in bus_type:
            chosen_slack_bus = slack_bus_id
        else:
            # Prefer a bus with at least one generator, else smallest bus id.
            gen_buses = [
                g.get("bus_id")
                for g in gens_in
                if isinstance(g.get("bus_id"), int) and g.get("bus_id") in bus_type
            ]
            chosen_slack_bus = (
                int(gen_buses[0])
                if gen_buses
                else int(bus_rows_sorted[0]["bus_id"])
                if bus_rows_sorted
                else None
            )
        if chosen_slack_bus is not None:
            bus_type[chosen_slack_bus] = 3  # REF

    # Build MATPOWER bus matrix
    # Columns: BUS_I BUS_TYPE PD QD GS BS BUS_AREA VM VA BASE_KV ZONE VMAX VMIN
    bus_mat: List[List[Any]] = []
    bus_row_map: Dict[
        int, int
    ] = {}  # original bus_id -> row index (1-based in matrix list sense)
    bus_limit_issues: List[Dict[str, Any]] = []

    for row_idx, b in enumerate(bus_rows_sorted, start=1):
        bid = int(b["bus_id"])
        bi = bus_id_to_i[bid]
        base_kv = safe_float(b.get("nominalV")) or 0.0

        vmin, vmax = parse_bus_limits_pu(b)
        # guard nonsense
        if vmax < vmin:
            bus_limit_issues.append({"bus_id": bid, "vmin": vmin, "vmax": vmax})
            vmin, vmax = 0.9, 1.1

        PD = pd_sum.get(bid, 0.0)
        QD = qd_sum.get(bid, 0.0)
        GS = gs_bus_mw.get(bid, 0.0)
        BS = bs_bus_mvar.get(bid, 0.0)

        bus_mat.append(
            [
                bi,
                bus_type[bid],
                PD,
                QD,
                GS,
                BS,
                1,  # BUS_AREA
                1.0,  # VM init
                0.0,  # VA init
                base_kv,
                1,  # ZONE
                vmax,
                vmin,
            ]
        )
        bus_row_map[bid] = row_idx

    # Build MATPOWER gen matrix
    # Columns: BUS PG QG QMAX QMIN VG MBASE GEN_STATUS PMAX PMIN
    gen_mat: List[List[Any]] = []
    gen_row_map: Dict[str, int] = {}
    gen_prov: Dict[str, Any] = {}
    q_limits_defaulted = 0

    for g in gens_in:
        gid = str(g.get("id", ""))
        bid = g.get("bus_id")
        if not isinstance(bid, int) or bid not in bus_id_to_i:
            continue
        bus_i = bus_id_to_i[bid]

        pg, qg, prov = extract_gen_pq(g)
        a = g.get("attrib") or {}

        pmax = safe_float(a.get("maxP"))
        pmin = safe_float(a.get("minP"))
        if pmax is None:
            pmax = 0.0
        if pmin is None:
            pmin = 0.0

        # Q limits usually not present at EQUIPMENT level; default to wide bounds
        qmax = safe_float(a.get("maxQ"))
        qmin = safe_float(a.get("minQ"))
        if qmax is None or qmin is None:
            qmax = default_q_limits
            qmin = -default_q_limits
            q_limits_defaulted += 1

        # Voltage setpoint (pu) might be present; else 1.0
        vg = safe_float(a.get("voltageSetpoint"))
        if vg is None:
            # could be in kV; if so we cannot know; keep 1.0
            vg = 1.0

        gen_status = 1

        gen_mat.append(
            [
                bus_i,
                pg,
                qg,
                qmax,
                qmin,
                vg,
                base_mva,  # MBASE
                gen_status,
                pmax,
                pmin,
            ]
        )
        gen_row_map[gid] = len(gen_mat)
        if len(gen_prov) < 100:
            gen_prov[gid] = {
                "bus_id": bid,
                **prov,
                "q_limits_defaulted": (
                    qmax == default_q_limits and qmin == -default_q_limits
                ),
            }

    # Build MATPOWER branch matrix
    # Columns: F_BUS T_BUS BR_R BR_X BR_B RATE_A RATE_B RATE_C TAP SHIFT BR_STATUS ANGMIN ANGMAX
    branch_mat: List[List[Any]] = []
    branch_row_map: Dict[str, int] = {}
    tap_shift_prov: Dict[str, Any] = {}
    branch_missing_pu = 0

    for br in branches_in:
        br_id = str(br.get("id", ""))
        fbus = br.get("fbus")
        tbus = br.get("tbus")
        if not isinstance(fbus, int) or not isinstance(tbus, int):
            continue
        if fbus not in bus_id_to_i or tbus not in bus_id_to_i:
            continue
        f_i = bus_id_to_i[fbus]
        t_i = bus_id_to_i[tbus]

        pu = br.get("pu") or {}
        r_pu = safe_float(pu.get("r_pu"))
        x_pu = safe_float(pu.get("x_pu"))
        if r_pu is None or x_pu is None:
            # still write zeros but report it
            branch_missing_pu += 1
            r_pu = r_pu if r_pu is not None else 0.0
            x_pu = x_pu if x_pu is not None else 0.0

        # Branch shunt susceptance: for lines, prefer b_total_pu; else 0
        b_pu = safe_float(pu.get("b_total_pu"))
        if b_pu is None:
            b_pu = 0.0

        tap, shift, prov = infer_tap_shift(br)
        if br.get("type") != "twoWindingsTransformer":
            tap = 0.0  # MATPOWER uses 0 => no transformer; 1 also ok; choose 0 to match convention for lines
            shift = 0.0

        # Ratings unknown -> 0
        rateA = 0.0
        rateB = 0.0
        rateC = 0.0
        status = 1
        angmin = -360.0
        angmax = 360.0

        branch_mat.append(
            [
                f_i,
                t_i,
                r_pu,
                x_pu,
                b_pu,
                rateA,
                rateB,
                rateC,
                tap if tap != 0.0 else 0.0,
                shift,
                status,
                angmin,
                angmax,
            ]
        )
        branch_row_map[br_id] = len(branch_mat)
        if br.get("type") == "twoWindingsTransformer" and len(tap_shift_prov) < 200:
            tap_shift_prov[br_id] = prov

    # If no generators but a REF bus was assigned, remove it (cannot be slack without gen row)
    ref_buses = [bid for bid, t in bus_type.items() if t == 3]
    if ref_buses and not gen_mat:
        # revert to PQ
        for bid in ref_buses:
            bus_type[bid] = 1
        # patch bus_mat
        for r in bus_mat:
            # BUS_I is r[0], map back to bid
            bi = int(r[0])
            bid = i_to_bus_id.get(bi)
            if bid in ref_buses:
                r[1] = 1
        chosen_slack_bus = None

    # MATPOWER case file content
    # Also embed a comment block with hashes for reproducibility.
    header_lines = [
        f"function mpc = {case_name}",
        "%MATPOWER Case Format : Version 2",
        f"%Created by 05_write_matpower.py at {iso_now()}",
        f"%Source JSON: {os.path.basename(in_path)}",
        f"%Source sha256: {in_hash}",
        f"%baseMVA: {base_mva}",
        "",
        "mpc.version = '2';",
        f"mpc.baseMVA = {fmt_num(base_mva)};",
        "",
        "% bus data",
        "% bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin",
        f"mpc.bus = {matlab_matrix(bus_mat)};",
        "",
        "% generator data",
        "% bus Pg Qg Qmax Qmin Vg mBase status Pmax Pmin",
        f"mpc.gen = {matlab_matrix(gen_mat)};",
        "",
        "% branch data",
        "% fbus tbus r x b rateA rateB rateC ratio angle status angmin angmax",
        f"mpc.branch = {matlab_matrix(branch_mat)};",
        "",
        "% (optional) generator cost data",
        "% left empty on purpose",
        "mpc.gencost = [];",
        "",
        "end",
        "",
    ]

    with open(out_m_path, "w", encoding="utf-8") as f:
        f.write("\n".join(header_lines))

    # Sidecar mappings
    sidecar = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "input": {"path": in_path, "size_bytes": in_size, "sha256": in_hash},
            "output_matpower": {"path": out_m_path},
            "case_name": case_name,
            "baseMVA": base_mva,
        },
        "bus_index": {
            "bus_id_to_BUS_I": bus_id_to_i,
            "BUS_I_to_bus_id": i_to_bus_id,
        },
        "row_index": {
            "bus_rows_by_bus_id": bus_row_map,  # original bus_id -> row number in mpc.bus (1-based in list)
            "gen_rows_by_gen_id": gen_row_map,  # gen IIDM id -> row number in mpc.gen (1-based)
            "branch_rows_by_branch_id": branch_row_map,  # branch id -> row number in mpc.branch (1-based)
        },
        "provenance": {
            "generator_pg_qg_sources": gen_prov,
            "load_pd_qd_sources_sample": load_prov,
            "transformer_tap_shift_sources": tap_shift_prov,
            "bus_shunt_sources_sample": shunt_sources,
        },
    }

    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)

    # Report
    report = {
        "meta": sidecar["meta"],
        "stats": {
            "buses_in": len(buses_in),
            "buses_written": len(bus_mat),
            "gens_in": len(gens_in),
            "gens_written": len(gen_mat),
            "branches_in": len(branches_in),
            "branches_written": len(branch_mat),
            "loads_in": len(loads_in),
        },
        "policies": {
            "bus_renumbering": "sorted by original bus_id, remapped to 1..N",
            "default_bus_type": "PQ(1), PV(2) if generator voltageRegulatorOn=true",
            "assign_slack": assign_slack,
            "chosen_slack_bus_id": chosen_slack_bus,
            "slack_rule": "explicit slack_bus_id if provided; else first generator bus; else smallest bus_id",
            "gen_pg_qg_default": "0 if not present in attrib",
            "gen_q_limits_default": {
                "QMAX": default_q_limits,
                "QMIN": -default_q_limits,
            },
            "tap_shift_inference": "best-effort from recorded tap changer selected_step keys",
        },
        "checks": {
            "ref_buses_count": len([r for r in bus_mat if int(r[1]) == 3]),
            "pv_buses_count": len([r for r in bus_mat if int(r[1]) == 2]),
            "pq_buses_count": len([r for r in bus_mat if int(r[1]) == 1]),
            "bus_limit_issues": bus_limit_issues[:50],
            "branches_missing_pu_r_or_x": branch_missing_pu,
            "q_limits_defaulted_generators": q_limits_defaulted,
        },
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "case_name": case_name,
                "baseMVA": base_mva,
                "bus": len(bus_mat),
                "gen": len(gen_mat),
                "branch": len(branch_mat),
                "assign_slack": assign_slack,
                "slack_bus_id": chosen_slack_bus,
                "branch_missing_pu": branch_missing_pu,
            },
            indent=2,
        )
    )


def parse_bool(x: Any) -> Optional[bool]:
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Write MATPOWER .m case from 04_pu.json.",
    )
    ap.add_argument(
        "--in", dest="inp", required=True, help="Input JSON (from 04_per_unitize.py)"
    )
    ap.add_argument(
        "--out", default="case.m", help="Output MATPOWER .m (default: case.m)"
    )
    ap.add_argument(
        "--sidecar",
        default="05_matpower_sidecar.json",
        help="Sidecar mapping JSON (default: 05_matpower_sidecar.json)",
    )
    ap.add_argument(
        "--report",
        default="05_write_report.json",
        help="Report JSON (default: 05_write_report.json)",
    )
    ap.add_argument(
        "--case-name",
        default="rte_case",
        help="MATLAB function name (default: rte_case)",
    )
    ap.add_argument(
        "--assign-slack",
        action="store_true",
        help="Assign one REF bus (to make PF runnable). Default off.",
    )
    ap.add_argument(
        "--slack-bus-id",
        type=int,
        default=None,
        help="Original bus_id to use as REF (only if --assign-slack).",
    )
    ap.add_argument(
        "--default-q-limits",
        type=float,
        default=9999.0,
        help="Default +/-Q limits when missing (MVAr).",
    )
    args = ap.parse_args()

    write_matpower(
        in_path=args.inp,
        out_m_path=args.out,
        sidecar_path=args.sidecar,
        report_path=args.report,
        case_name=args.case_name,
        assign_slack=args.assign_slack,
        slack_bus_id=args.slack_bus_id,
        default_q_limits=args.default_q_limits,
    )


if __name__ == "__main__":
    main()

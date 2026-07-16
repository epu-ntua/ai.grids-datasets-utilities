#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
06_validate_case.py

Validate a MATPOWER case written by 05_write_matpower.py.

Inputs:
  --case     case.m
  --sidecar  05_matpower_sidecar.json   (recommended)
Optional:
  --pu-json  04_pu.json                 (enables extra consistency checks)
Options:
  --run-pf   attempt AC PF via pypower (if installed)

Outputs:
  --report   06_validate_report.json
  --txt      06_validate.txt

Validations:
  - Parse baseMVA, bus/gen/branch matrices (no MATLAB required).
  - Structural checks: dimensions, indices, NaN/inf, endpoints.
  - Graph checks: islands, isolated buses, degree stats.
  - Slack/generator sanity.
  - Optional PF: runpf (pypower) if installed.
  - Optional cross-checks vs 04_pu.json (requires sidecar):
      * bus shunts (GS/BS) on system base
      * branch BR_B for line charging

Fixes included vs earlier versions:
  - rel_err() present
  - close_enough() uses abs+rel tolerance
  - cross-check mismatch counts are true totals; examples are capped
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from .common import iso_now, sha256_file
    from .common import safe_float_opt as safe_float
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import iso_now, sha256_file  # type: ignore
    from common import safe_float_opt as safe_float


# -------------------- utilities --------------------


def rel_err(a: float, b: float, eps: float = 1e-12) -> float:
    return abs(a - b) / max(eps, abs(a), abs(b))


def close_enough(a: float, b: float, rtol: float = 1e-9, atol: float = 1e-12) -> bool:
    return abs(a - b) <= atol + rtol * max(abs(a), abs(b))


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def strip_matlab_comments(s: str) -> str:
    return re.sub(r"%[^\n]*", "", s)


def extract_baseMVA(text: str) -> Optional[float]:
    m = re.search(r"mpc\.baseMVA\s*=\s*([0-9eE\+\-\.]+)\s*;", text)
    if not m:
        return None
    return safe_float(m.group(1))


def _find_matrix_block(text: str, varname: str) -> Optional[str]:
    key = f"mpc.{varname}"
    start = text.find(key)
    if start < 0:
        return None
    m = re.search(rf"mpc\.{re.escape(varname)}\s*=\s*\[", text[start:])
    if not m:
        return None
    block_start = start + m.end()  # just after '['
    end = text.find("];", block_start)
    if end < 0:
        return None
    return text[block_start:end]


def parse_numeric_matrix(block: str) -> np.ndarray:
    block = block.strip()
    if not block:
        return np.zeros((0, 0), dtype=float)

    rows_raw = [r.strip() for r in block.split(";")]
    rows_raw = [r for r in rows_raw if r]

    rows: List[List[float]] = []
    for r in rows_raw:
        parts = re.split(r"\s+", r.strip())
        vals: List[float] = []
        for p in parts:
            if not p:
                continue
            try:
                v = float(p)
            except Exception:
                v = float("nan")
            vals.append(v)
        if vals:
            rows.append(vals)

    if not rows:
        return np.zeros((0, 0), dtype=float)

    ncol = max(len(r) for r in rows)
    arr = np.full((len(rows), ncol), np.nan, dtype=float)
    for i, r in enumerate(rows):
        arr[i, : len(r)] = np.array(r, dtype=float)
    return arr


def count_nonfinite(arr: np.ndarray) -> int:
    return int(np.size(arr) - np.count_nonzero(np.isfinite(arr)))


def to_int_safe(arr: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.round(arr).astype(int)


def degrees_from_edges(n: int, edges: List[Tuple[int, int]]) -> List[int]:
    deg = [0] * (n + 1)
    for u, v in edges:
        if 1 <= u <= n and 1 <= v <= n and u != v:
            deg[u] += 1
            deg[v] += 1
    return deg[1:]


def connected_components(n: int, edges: List[Tuple[int, int]]) -> List[List[int]]:
    adj: List[List[int]] = [[] for _ in range(n + 1)]
    for u, v in edges:
        if 1 <= u <= n and 1 <= v <= n and u != v:
            adj[u].append(v)
            adj[v].append(u)

    seen = [False] * (n + 1)
    comps: List[List[int]] = []
    for i in range(1, n + 1):
        if seen[i]:
            continue
        stack = [i]
        seen[i] = True
        comp: List[int] = []
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj[x]:
                if not seen[y]:
                    seen[y] = True
                    stack.append(y)
        comps.append(sorted(comp))
    comps.sort(key=lambda c: (-len(c), c[0]))
    return comps


# -------------------- optional PF --------------------


def try_run_pf(ppc: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from pypower.api import ppoption, runpf  # type: ignore
    except Exception as e:
        return {"attempted": False, "available": False, "error": str(e)}

    try:
        ppopt = ppoption(VERBOSE=0, OUT_ALL=0)
        results, success = runpf(ppc, ppopt)
        bus = results["bus"]
        gen = results["gen"]
        return {
            "attempted": True,
            "available": True,
            "success": bool(success),
            "bus_vm_min": float(np.min(bus[:, 7])) if bus.size else None,
            "bus_vm_max": float(np.max(bus[:, 7])) if bus.size else None,
            "bus_va_min_deg": float(np.min(bus[:, 8])) if bus.size else None,
            "bus_va_max_deg": float(np.max(bus[:, 8])) if bus.size else None,
            "gen_pg_sum": float(np.sum(gen[:, 1])) if gen.size else None,
            "gen_qg_sum": float(np.sum(gen[:, 2])) if gen.size else None,
        }
    except Exception as e:
        return {"attempted": True, "available": True, "success": False, "error": str(e)}


# -------------------- consistency checks vs 04_pu.json --------------------


def load_pu_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_bus_shunt_pu_from_pu_json(
    pu_json: Dict[str, Any],
) -> Dict[int, Tuple[float, float]]:
    out: Dict[int, Tuple[float, float]] = {}
    for b in pu_json.get("buses", []) or []:
        bid = b.get("bus_id")
        if not isinstance(bid, int):
            continue
        sh = (b.get("pu") or {}).get("bus_shunt_pu") or {}
        gs = safe_float(sh.get("gs_total_pu")) or 0.0
        bs = safe_float(sh.get("bs_total_pu")) or 0.0
        out[bid] = (gs, bs)
    return out


def build_branch_b_total_pu_from_pu_json(pu_json: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for br in pu_json.get("branches", []) or []:
        br_id = str(br.get("id", ""))
        pu = br.get("pu") or {}
        btot = safe_float(pu.get("b_total_pu"))
        if btot is not None:
            out[br_id] = btot
    return out


# -------------------- main --------------------


def validate_case(
    case_path: str,
    sidecar_path: Optional[str],
    pu_json_path: Optional[str],
    report_path: str,
    txt_path: str,
    run_pf: bool,
    max_examples: int = 50,
) -> None:
    if not os.path.exists(case_path):
        raise FileNotFoundError(case_path)
    case_hash = sha256_file(case_path)
    case_size = os.path.getsize(case_path)

    sidecar = None
    sidecar_hash = None
    if sidecar_path:
        if not os.path.exists(sidecar_path):
            raise FileNotFoundError(sidecar_path)
        sidecar_hash = sha256_file(sidecar_path)
        with open(sidecar_path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)

    pu_json = None
    pu_hash = None
    if pu_json_path:
        if not os.path.exists(pu_json_path):
            raise FileNotFoundError(pu_json_path)
        pu_hash = sha256_file(pu_json_path)
        pu_json = load_pu_json(pu_json_path)

    text_raw = read_text(case_path)
    text = strip_matlab_comments(text_raw)

    baseMVA = extract_baseMVA(text)
    if baseMVA is None:
        baseMVA = 100.0

    blocks: Dict[str, Optional[str]] = {}
    for name in ("bus", "gen", "branch"):
        blocks[name] = _find_matrix_block(text, name)

    parse_errors: List[str] = []
    matrices: Dict[str, np.ndarray] = {}
    for name, blk in blocks.items():
        if blk is None:
            parse_errors.append(f"Missing matrix mpc.{name}")
            matrices[name] = np.zeros((0, 0), dtype=float)
        else:
            matrices[name] = parse_numeric_matrix(blk)

    bus = matrices["bus"]
    gen = matrices["gen"]
    branch = matrices["branch"]

    exp_cols = {"bus": 13, "gen": 10, "branch": 13}
    dim_issues: List[Dict[str, Any]] = []
    for name, arr in matrices.items():
        if arr.size == 0:
            continue
        if arr.shape[1] != exp_cols[name]:
            dim_issues.append(
                {
                    "matrix": name,
                    "expected_cols": exp_cols[name],
                    "got_cols": int(arr.shape[1]),
                }
            )

    nonfinite = {
        "bus": count_nonfinite(bus),
        "gen": count_nonfinite(gen),
        "branch": count_nonfinite(branch),
    }

    structural = {
        "parse_errors": parse_errors,
        "dim_issues": dim_issues,
        "nonfinite_counts": nonfinite,
    }

    n_bus = int(bus.shape[0]) if bus.size else 0
    n_gen = int(gen.shape[0]) if gen.size else 0
    n_branch = int(branch.shape[0]) if branch.size else 0

    bus_i = to_int_safe(bus[:, 0]) if n_bus else np.array([], dtype=int)
    bus_types = to_int_safe(bus[:, 1]) if n_bus else np.array([], dtype=int)

    bus_i_unique = len(set(bus_i.tolist())) == n_bus if n_bus else True
    bus_i_min = int(bus_i.min()) if n_bus else None
    bus_i_max = int(bus_i.max()) if n_bus else None
    bus_i_contig = (
        (bus_i_min == 1 and bus_i_max == n_bus and bus_i_unique) if n_bus else True
    )

    bus_type_counts = (
        collections.Counter(bus_types.tolist()) if n_bus else collections.Counter()
    )
    invalid_bus_types = (
        [t for t in set(bus_types.tolist()) if t not in (1, 2, 3, 4)] if n_bus else []
    )

    fbus = to_int_safe(branch[:, 0]) if n_branch else np.array([], dtype=int)
    tbus = to_int_safe(branch[:, 1]) if n_branch else np.array([], dtype=int)
    status = to_int_safe(branch[:, 10]) if n_branch else np.array([], dtype=int)

    bad_endpoints = 0
    bad_endpoint_examples: List[Dict[str, Any]] = []
    for i in range(n_branch):
        u = int(fbus[i])
        v = int(tbus[i])
        if not (1 <= u <= n_bus and 1 <= v <= n_bus):
            bad_endpoints += 1
            if len(bad_endpoint_examples) < max_examples:
                bad_endpoint_examples.append(
                    {"branch_row": i + 1, "fbus": u, "tbus": v}
                )

    edges: List[Tuple[int, int]] = []
    out_of_service = 0
    self_loops = 0
    for i in range(n_branch):
        if int(status[i]) == 0:
            out_of_service += 1
            continue
        u = int(fbus[i])
        v = int(tbus[i])
        if u == v:
            self_loops += 1
            continue
        if 1 <= u <= n_bus and 1 <= v <= n_bus:
            edges.append((u, v))

    comps = connected_components(n_bus, edges) if n_bus else []
    islands = len(comps)
    island_sizes = [len(c) for c in comps]
    largest_island = island_sizes[0] if island_sizes else 0

    deg = degrees_from_edges(n_bus, edges) if n_bus else []
    isolated = [i + 1 for i, d in enumerate(deg) if d == 0] if deg else []
    deg_stats = {}
    if deg:
        deg_stats = {
            "min": min(deg),
            "max": max(deg),
            "mean": float(statistics.mean(deg)),
            "median": float(statistics.median(deg)),
            "p95": float(np.percentile(np.array(deg, dtype=float), 95)),
        }

    ref_buses = (
        [int(bus_i[i]) for i in range(n_bus) if int(bus_types[i]) == 3] if n_bus else []
    )
    pv_buses = (
        [int(bus_i[i]) for i in range(n_bus) if int(bus_types[i]) == 2] if n_bus else []
    )

    gen_buses = to_int_safe(gen[:, 0]).tolist() if n_gen else []
    gen_bus_missing = [b for b in gen_buses if not (1 <= int(b) <= n_bus)]
    gen_bus_counts = (
        collections.Counter(int(b) for b in gen_buses)
        if n_gen
        else collections.Counter()
    )

    pd_total = float(np.sum(bus[:, 2])) if n_bus else 0.0
    qd_total = float(np.sum(bus[:, 3])) if n_bus else 0.0
    pg_total = float(np.sum(gen[:, 1])) if n_gen else 0.0
    qg_total = float(np.sum(gen[:, 2])) if n_gen else 0.0

    # -------------------- PU cross-checks --------------------
    pu_checks: Dict[str, Any] = {"attempted": False}
    if pu_json is not None and sidecar is not None:
        pu_checks["attempted"] = True

        pu_bus_sh = build_bus_shunt_pu_from_pu_json(pu_json)
        pu_branch_btot = build_branch_b_total_pu_from_pu_json(pu_json)

        # Map MATPOWER BUS_I -> original bus_id (keys may be strings)
        bi_to_busid = (sidecar.get("bus_index") or {}).get("BUS_I_to_bus_id") or {}
        bi_to_busid_int: Dict[int, int] = {}
        for k, v in bi_to_busid.items():
            try:
                bi_to_busid_int[int(k)] = int(v)
            except Exception:
                pass

        # Map branch id -> row in mpc.branch (1-based) (keys may be strings)
        br_rows_by_id = (sidecar.get("row_index") or {}).get(
            "branch_rows_by_branch_id"
        ) or {}
        br_row_map: Dict[str, int] = {}
        for k, v in br_rows_by_id.items():
            try:
                br_row_map[str(k)] = int(v)
            except Exception:
                pass

        # Bus shunt cross-check
        mismatch_total = 0
        shunt_mismatches: List[Dict[str, Any]] = []
        checked = 0

        for i in range(n_bus):
            bi = int(bus_i[i])
            orig_busid = bi_to_busid_int.get(bi)
            if orig_busid is None:
                continue

            gs_pu_expected, bs_pu_expected = pu_bus_sh.get(orig_busid, (0.0, 0.0))

            gs_mw = float(bus[i, 4])
            bs_mvar = float(bus[i, 5])
            gs_pu_got = gs_mw / float(baseMVA)
            bs_pu_got = bs_mvar / float(baseMVA)

            checked += 1

            if (not close_enough(gs_pu_expected, gs_pu_got)) or (
                not close_enough(bs_pu_expected, bs_pu_got)
            ):
                mismatch_total += 1
                if len(shunt_mismatches) < max_examples:
                    shunt_mismatches.append(
                        {
                            "BUS_I": bi,
                            "orig_bus_id": orig_busid,
                            "gs_pu_expected": gs_pu_expected,
                            "gs_pu_got": gs_pu_got,
                            "bs_pu_expected": bs_pu_expected,
                            "bs_pu_got": bs_pu_got,
                        }
                    )

        bus_shunt_check = {
            "checked_buses": checked,
            "mismatches_total": mismatch_total,
            "examples_stored": len(shunt_mismatches),
            "examples": shunt_mismatches,
        }

        # Branch BR_B cross-check
        mismatch_total_br = 0
        branch_mismatches: List[Dict[str, Any]] = []
        checked_br = 0

        for br_id, btot_pu_expected in pu_branch_btot.items():
            r1 = br_row_map.get(br_id)
            if r1 is None:
                continue
            idx = r1 - 1
            if not (0 <= idx < n_branch):
                continue

            b_pu_got = float(branch[idx, 4])
            checked_br += 1

            if not close_enough(btot_pu_expected, b_pu_got):
                mismatch_total_br += 1
                if len(branch_mismatches) < max_examples:
                    branch_mismatches.append(
                        {
                            "branch_id": br_id,
                            "row": r1,
                            "b_pu_expected": btot_pu_expected,
                            "b_pu_got": b_pu_got,
                        }
                    )

        branch_b_check = {
            "checked_branches": checked_br,
            "mismatches_total": mismatch_total_br,
            "examples_stored": len(branch_mismatches),
            "examples": branch_mismatches,
        }

        pu_checks.update(
            {
                "bus_shunt_check": bus_shunt_check,
                "branch_b_check": branch_b_check,
            }
        )

    # -------------------- Optional PF --------------------
    pf = {"attempted": False}
    if run_pf:
        ppc = {
            "version": "2",
            "baseMVA": float(baseMVA),
            "bus": bus.astype(float) if bus.size else np.zeros((0, 13), dtype=float),
            "gen": gen.astype(float) if gen.size else np.zeros((0, 10), dtype=float),
            "branch": branch.astype(float)
            if branch.size
            else np.zeros((0, 13), dtype=float),
            "gencost": np.zeros((0, 0), dtype=float),
        }
        pf = try_run_pf(ppc)

    report: Dict[str, Any] = {
        "meta": {
            "created_at": iso_now(),
            "script": os.path.basename(__file__),
            "inputs": {
                "case": {
                    "path": case_path,
                    "size_bytes": case_size,
                    "sha256": case_hash,
                },
                "sidecar": {"path": sidecar_path, "sha256": sidecar_hash}
                if sidecar_path
                else None,
                "pu_json": {"path": pu_json_path, "sha256": pu_hash}
                if pu_json_path
                else None,
            },
        },
        "matpower": {
            "baseMVA": float(baseMVA),
            "sizes": {"bus": n_bus, "gen": n_gen, "branch": n_branch},
        },
        "structural": structural,
        "checks": {
            "bus_indexing": {
                "unique": bus_i_unique,
                "contiguous_1_to_N": bus_i_contig,
                "min": bus_i_min,
                "max": bus_i_max,
                "invalid_bus_types": invalid_bus_types,
                "bus_type_counts": dict(bus_type_counts),
            },
            "branch_endpoints": {
                "bad_endpoints": bad_endpoints,
                "bad_endpoint_examples": bad_endpoint_examples,
                "out_of_service": out_of_service,
                "self_loops_in_service": self_loops,
            },
            "connectivity": {
                "islands": islands,
                "largest_island_size": largest_island,
                "island_sizes_top10": island_sizes[:10],
                "isolated_buses_count": len(isolated),
                "isolated_buses_examples": isolated[: min(max_examples, len(isolated))],
                "degree_stats": deg_stats,
            },
            "slack_and_gens": {
                "ref_buses": ref_buses[: min(max_examples, len(ref_buses))],
                "ref_count": len(ref_buses),
                "pv_count": len(pv_buses),
                "gen_bus_missing_count": len(gen_bus_missing),
                "gen_bus_missing_examples": gen_bus_missing[
                    : min(max_examples, len(gen_bus_missing))
                ],
                "gen_bus_counts_top10": gen_bus_counts.most_common(10),
            },
            "power_sums": {
                "PD_total": pd_total,
                "QD_total": qd_total,
                "PG_total": pg_total,
                "QG_total": qg_total,
                "note": "Sums reflect whatever was written; not a balance check.",
            },
            "pu_cross_checks": pu_checks,
            "power_flow": pf,
        },
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # -------------------- TXT summary --------------------
    lines: List[str] = []
    lines.append(f"CASE: {case_path}")
    lines.append(f"SHA256: {case_hash}")
    lines.append(f"baseMVA: {baseMVA}")
    lines.append("")
    if parse_errors:
        lines.append("PARSE ERRORS:")
        for e in parse_errors:
            lines.append(f"  - {e}")
        lines.append("")
    if dim_issues:
        lines.append("DIMENSION ISSUES:")
        for d in dim_issues:
            lines.append(f"  - {d}")
        lines.append("")
    lines.append("SIZES:")
    lines.append(f"  bus: {n_bus}, gen: {n_gen}, branch: {n_branch}")
    lines.append("")
    lines.append("BUS INDEXING:")
    lines.append(f"  unique: {bus_i_unique}")
    lines.append(f"  contiguous 1..N: {bus_i_contig}")
    lines.append(f"  min BUS_I: {bus_i_min}, max BUS_I: {bus_i_max}")
    lines.append(f"  bus types: {dict(bus_type_counts)}")
    if invalid_bus_types:
        lines.append(f"  invalid bus types: {invalid_bus_types}")
    lines.append("")
    lines.append("BRANCH ENDPOINTS / STATUS:")
    lines.append(f"  bad endpoints: {bad_endpoints}")
    lines.append(f"  out-of-service branches: {out_of_service}")
    lines.append(f"  self-loops in service: {self_loops}")
    lines.append("")
    lines.append("CONNECTIVITY:")
    lines.append(f"  islands: {islands}")
    lines.append(f"  largest island size: {largest_island}")
    lines.append(f"  isolated buses: {len(isolated)}")
    if deg_stats:
        lines.append(f"  degree stats: {deg_stats}")
    lines.append("")
    lines.append("SLACK / GENERATORS:")
    lines.append(f"  REF count: {len(ref_buses)}  (examples: {ref_buses[:10]})")
    lines.append(f"  PV count: {len(pv_buses)}")
    lines.append(f"  gen buses missing: {len(gen_bus_missing)}")
    lines.append("")
    lines.append("POWER SUMS (as written):")
    lines.append(f"  PD: {pd_total}, QD: {qd_total}, PG: {pg_total}, QG: {qg_total}")
    lines.append("")
    if pu_checks.get("attempted"):
        bs = pu_checks.get("bus_shunt_check", {})
        brc = pu_checks.get("branch_b_check", {})
        lines.append("PU CROSS-CHECKS (vs 04_pu.json):")
        lines.append(
            f"  bus shunts checked: {bs.get('checked_buses')} mismatches_total: {bs.get('mismatches_total')} examples_stored: {bs.get('examples_stored')}"
        )
        lines.append(
            f"  branch BR_B checked: {brc.get('checked_branches')} mismatches_total: {brc.get('mismatches_total')} examples_stored: {brc.get('examples_stored')}"
        )
        lines.append("")
    if pf.get("attempted"):
        lines.append("POWER FLOW (pypower):")
        lines.append(f"  available: {pf.get('available')}")
        lines.append(f"  success: {pf.get('success')}")
        if pf.get("error"):
            lines.append(f"  error: {pf.get('error')}")
        else:
            lines.append(
                f"  VM range: {pf.get('bus_vm_min')} .. {pf.get('bus_vm_max')}"
            )
        lines.append("")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(
        json.dumps(
            {
                "baseMVA": baseMVA,
                "bus": n_bus,
                "gen": n_gen,
                "branch": n_branch,
                "islands": islands,
                "isolated_buses": len(isolated),
                "bad_branch_endpoints": bad_endpoints,
                "ref_count": len(ref_buses),
                "pf_attempted": pf.get("attempted", False),
                "pf_success": pf.get("success", None),
                "pu_cross_checks_attempted": pu_checks.get("attempted", False),
                "pu_shunt_mismatches_total": (
                    pu_checks.get("bus_shunt_check") or {}
                ).get("mismatches_total")
                if pu_checks.get("attempted")
                else None,
                "pu_branch_b_mismatches_total": (
                    pu_checks.get("branch_b_check") or {}
                ).get("mismatches_total")
                if pu_checks.get("attempted")
                else None,
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Validate MATPOWER case.m written by 05_write_matpower.py",
    )
    ap.add_argument("--case", required=True, help="Path to MATPOWER case .m")
    ap.add_argument(
        "--sidecar", default=None, help="Path to 05_matpower_sidecar.json (recommended)"
    )
    ap.add_argument(
        "--pu-json",
        default=None,
        help="Path to 04_pu.json for extra consistency checks",
    )
    ap.add_argument(
        "--report", default="06_validate_report.json", help="Output JSON report"
    )
    ap.add_argument("--txt", default="06_validate.txt", help="Output text summary")
    ap.add_argument(
        "--run-pf",
        action="store_true",
        help="Attempt AC PF using pypower (if installed)",
    )
    ap.add_argument(
        "--max-examples", type=int, default=50, help="Max examples stored per check"
    )
    args = ap.parse_args()

    validate_case(
        case_path=args.case,
        sidecar_path=args.sidecar,
        pu_json_path=args.pu_json,
        report_path=args.report,
        txt_path=args.txt,
        run_pf=args.run_pf,
        max_examples=args.max_examples,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
07_drop_isolated_buses.py

Drops degree-0 (isolated) buses from a MATPOWER case:
- Removes buses with no in-service incident branches.
- Removes gens attached to removed buses (safety).
- Removes branches touching removed buses (safety).
- Renumbers BUS_I to 1..N and updates GEN_BUS / F_BUS / T_BUS.
- Preserves mpc.baseMVA and (if present) mpc.gencost (filtered to kept generators).

Also emits:
- report JSON (counts before/after + connectivity stats)
- bus renumber map JSON (old_bus_id -> new_bus_id, plus removed list)

Usage:
  python 07_drop_isolated_buses.py --case ./out/rte-01Jan21-0000_complete.m --out ./out/rte-01Jan21-0000_pruned.m --report 07_prune_report.json --busmap 07_busmap.json

Optional:
  --keep-largest   Keep only the largest connected component AFTER dropping isolated buses.
                  (Useful when you want a single connected grid for PF/OPF tooling.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple


def _strip_matlab_comments(line: str) -> str:
    # MATPOWER uses '%' for comments.
    return line.split("%", 1)[0].strip()


def _find_block(text: str, field: str) -> Optional[Tuple[int, int]]:
    """
    Find the substring boundaries (start, end) of the matrix block content inside:
        mpc.<field> = [
            ...
        ];
    Returns indices covering ONLY the inside content (between '[' and '];').
    """
    # Find "mpc.<field> = ["
    m = re.search(rf"mpc\.{re.escape(field)}\s*=\s*\[\s*", text)
    if not m:
        return None
    start = m.end()
    end = text.find("];", start)
    if end < 0:
        raise ValueError(f"Could not find closing '];' for mpc.{field}")
    return start, end


def _parse_matrix_block(block: str, field: str) -> List[List[float]]:
    rows: List[List[float]] = []
    for raw in block.splitlines():
        line = _strip_matlab_comments(raw)
        if not line:
            continue
        line = line.strip()
        # allow trailing ';'
        if line.endswith(";"):
            line = line[:-1].strip()
        if not line:
            continue
        # MATPOWER commonly uses spaces; tolerate commas.
        line = line.replace(",", " ")
        parts = [p for p in line.split() if p]
        try:
            row = [float(p) for p in parts]
        except ValueError as e:
            raise ValueError(f"Failed parsing mpc.{field} row: {raw!r}") from e
        rows.append(row)
    return rows


def _read_scalar(text: str, field: str) -> Optional[float]:
    m = re.search(rf"mpc\.{re.escape(field)}\s*=\s*([\-0-9.eE\+]+)\s*;", text)
    if not m:
        return None
    return float(m.group(1))


def _read_version(text: str) -> Optional[str]:
    m = re.search(r"mpc\.version\s*=\s*'([^']+)'\s*;", text)
    if not m:
        return None
    return m.group(1)


def _read_case_name(text: str) -> Optional[str]:
    m = re.search(r"function\s+mpc\s*=\s*([A-Za-z0-9_]+)\s*", text)
    return m.group(1) if m else None


def _fmt_num(x: float) -> str:
    # Keep integers readable, keep floats precise enough for power system data.
    if x == 0.0:
        return "0"
    if abs(x - round(x)) < 1e-12 and abs(x) < 1e12:
        return str(int(round(x)))
    # 16 sig figs is plenty for double round-trip in this context.
    return f"{x:.16g}"


def _write_matrix(f, field: str, mat: List[List[float]]) -> None:
    f.write(f"mpc.{field} = [\n")
    for row in mat:
        f.write("  " + " ".join(_fmt_num(v) for v in row) + ";\n")
    f.write("];\n\n")


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            self.p[ra] = rb
        elif self.r[ra] > self.r[rb]:
            self.p[rb] = ra
        else:
            self.p[rb] = ra
            self.r[ra] += 1


def _connectivity_stats(
    bus_ids: List[int], branch: List[List[float]]
) -> Dict[str, object]:
    """
    Compute island stats using in-service branches.
    Assumes bus_ids contains ALL buses under consideration.
    """
    idx = {bid: i for i, bid in enumerate(bus_ids)}
    n = len(bus_ids)
    uf = UnionFind(n)
    deg = [0] * n

    # MATPOWER BR_STATUS is column 11 (0-index 10) when present.
    for br in branch:
        if len(br) >= 11:
            status = int(round(br[10]))
        else:
            status = 1
        if status == 0:
            continue
        fb = int(round(br[0]))
        tb = int(round(br[1]))
        if fb not in idx or tb not in idx:
            continue
        i, j = idx[fb], idx[tb]
        uf.union(i, j)
        deg[i] += 1
        deg[j] += 1

    # Count components, including isolated (deg==0) as size-1 components.
    comp_sizes: Dict[int, int] = {}
    for i in range(n):
        r = uf.find(i)
        comp_sizes[r] = comp_sizes.get(r, 0) + 1

    sizes = sorted(comp_sizes.values(), reverse=True)
    islands = len(sizes)
    largest = sizes[0] if sizes else 0
    isolated = sum(1 for d in deg if d == 0)

    return {
        "islands": islands,
        "largest_island_size": largest,
        "isolated_buses": isolated,
        "degree_min": min(deg) if deg else 0,
        "degree_max": max(deg) if deg else 0,
        "degree_mean": (sum(deg) / n) if n else 0.0,
    }


def drop_isolated_buses_case(
    case_path: str,
    out_path: str,
    report_path: str,
    busmap_path: str,
    keep_largest: bool = False,
    case_name: str | None = None,
) -> None:
    """
    Programmatic entrypoint for pruning. Mirrors CLI behavior.
    """

    with open(case_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    baseMVA = _read_scalar(text, "baseMVA")
    if baseMVA is None:
        raise ValueError("mpc.baseMVA not found in input case")
    version = _read_version(text) or "2"
    in_case_name = _read_case_name(text) or "case"
    out_case_name = case_name or f"{in_case_name}_pruned"

    blocks = {}
    for field in ("bus", "gen", "branch", "gencost"):
        loc = _find_block(text, field)
        if loc is None:
            blocks[field] = None
            continue
        s, e = loc
        blocks[field] = _parse_matrix_block(text[s:e], field)

    if blocks["bus"] is None or blocks["gen"] is None or blocks["branch"] is None:
        raise ValueError("Input case must contain mpc.bus, mpc.gen, and mpc.branch")

    bus = blocks["bus"]
    gen = blocks["gen"]
    branch = blocks["branch"]
    gencost = blocks["gencost"]  # may be None

    # Basic counts in
    bus_ids_in = [int(round(r[0])) for r in bus]
    stats_in = _connectivity_stats(bus_ids_in, branch)

    # Compute degrees (in-service branches only)
    deg: Dict[int, int] = {bid: 0 for bid in bus_ids_in}
    for br in branch:
        if len(br) >= 11:
            status = int(round(br[10]))
        else:
            status = 1
        if status == 0:
            continue
        fb = int(round(br[0]))
        tb = int(round(br[1]))
        if fb in deg:
            deg[fb] += 1
        if tb in deg:
            deg[tb] += 1

    isolated = sorted([bid for bid, d in deg.items() if d == 0])
    keep_set = set(bus_ids_in) - set(isolated)

    # Optionally keep only largest component (after removing isolated)
    if keep_largest and keep_set:
        kept_bus_ids = [bid for bid in bus_ids_in if bid in keep_set]
        idx = {bid: i for i, bid in enumerate(kept_bus_ids)}
        uf = UnionFind(len(kept_bus_ids))

        for br in branch:
            if len(br) >= 11:
                status = int(round(br[10]))
            else:
                status = 1
            if status == 0:
                continue
            fb = int(round(br[0]))
            tb = int(round(br[1]))
            if fb in idx and tb in idx:
                uf.union(idx[fb], idx[tb])

        comp: Dict[int, List[int]] = {}
        for bid in kept_bus_ids:
            r = uf.find(idx[bid])
            comp.setdefault(r, []).append(bid)

        largest_comp_buses = max(comp.values(), key=len)
        keep_set = set(largest_comp_buses)

    # Filter bus/gen/branch
    bus_kept = [row for row in bus if int(round(row[0])) in keep_set]
    gen_kept = [row for row in gen if int(round(row[0])) in keep_set]
    branch_kept = []
    for br in branch:
        fb = int(round(br[0]))
        tb = int(round(br[1]))
        if fb in keep_set and tb in keep_set:
            branch_kept.append(br)

    # If gencost exists, keep rows aligned to kept generators (by row order).
    # MATPOWER convention: gencost rows correspond to gen rows.
    gencost_kept = None
    if gencost is not None:
        if len(gencost) != len(gen):
            # If mismatch, preserve as-is rather than silently corrupt.
            # DataKit PF mode likely doesn't need gencost; OPF mode does.
            gencost_kept = gencost
        else:
            # Keep costs for kept generator rows.
            keep_mask = [int(round(g[0])) in keep_set for g in gen]
            gencost_kept = [row for row, m in zip(gencost, keep_mask) if m]

    # Renumber BUS_I to 1..N (preserve kept bus order as in original bus matrix)
    kept_old_ids = [int(round(r[0])) for r in bus_kept]
    renum = {old: new for new, old in enumerate(kept_old_ids, start=1)}

    removed_buses = [bid for bid in bus_ids_in if bid not in renum]

    # Update matrices in-place
    for r in bus_kept:
        old = int(round(r[0]))
        r[0] = float(renum[old])

    for g in gen_kept:
        old = int(round(g[0]))
        g[0] = float(renum[old])

    for br in branch_kept:
        fb = int(round(br[0]))
        tb = int(round(br[1]))
        br[0] = float(renum[fb])
        br[1] = float(renum[tb])

    # Ensure exactly one REF bus remains after pruning.
    # Existing REF can be dropped when keep_largest removes its component.
    # Prefer a bus that has at least one generator in the kept case.
    chosen_ref_bus_i: int | None = None
    if bus_kept:
        ref_rows = [r for r in bus_kept if len(r) >= 2 and int(round(r[1])) == 3]
        if not ref_rows:
            gen_bus_is = {int(round(g[0])) for g in gen_kept if len(g) >= 1}
            chosen_ref_bus_i = (
                min(gen_bus_is) if gen_bus_is else int(round(bus_kept[0][0]))
            )

            # Demote PV/REF to PQ first, then set chosen REF.
            for r in bus_kept:
                if len(r) >= 2 and int(round(r[1])) in (2, 3):
                    r[1] = 1.0
            for r in bus_kept:
                if int(round(r[0])) == chosen_ref_bus_i and len(r) >= 2:
                    r[1] = 3.0
                    break

    # Connectivity stats out
    bus_ids_out = [int(round(r[0])) for r in bus_kept]
    stats_out = _connectivity_stats(bus_ids_out, branch_kept)

    report = {
        "input_case": os.path.abspath(case_path),
        "output_case": os.path.abspath(out_path),
        "baseMVA": baseMVA,
        "dropped_isolated_only": (not keep_largest),
        "keep_largest_component": bool(keep_largest),
        "counts_in": {"bus": len(bus), "gen": len(gen), "branch": len(branch)},
        "counts_out": {
            "bus": len(bus_kept),
            "gen": len(gen_kept),
            "branch": len(branch_kept),
        },
        "isolated_buses_found": len(isolated),
        "isolated_buses_dropped": len([b for b in isolated if b in removed_buses]),
        "connectivity_in": stats_in,
        "connectivity_out": stats_out,
        "ref_bus_reassigned": bool(chosen_ref_bus_i is not None),
        "chosen_ref_bus_i_after_prune": chosen_ref_bus_i,
    }

    # Write busmap json
    busmap_obj = {
        "old_to_new": renum,  # old->new for kept buses
        "removed_old_bus_ids": removed_buses,
        "input_bus_count": len(bus),
        "output_bus_count": len(bus_kept),
    }
    with open(busmap_path, "w", encoding="utf-8") as f:
        json.dump(busmap_obj, f, indent=2, sort_keys=True)

    # Write report json
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)

    # Write MATPOWER case
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"function mpc = {out_case_name}\n")
        f.write("%% pruned MATPOWER case: isolated buses removed\n")
        f.write(f"mpc.version = '{version}';\n")
        f.write(f"mpc.baseMVA = {_fmt_num(baseMVA)};\n\n")

        _write_matrix(f, "bus", bus_kept)
        _write_matrix(f, "gen", gen_kept)
        _write_matrix(f, "branch", branch_kept)
        if gencost_kept is not None:
            _write_matrix(f, "gencost", gencost_kept)

    # Print summary JSON to stdout (matches your other scripts’ style)
    print(
        json.dumps(
            {
                "baseMVA": baseMVA,
                "buses_in": len(bus),
                "buses_out": len(bus_kept),
                "gens_in": len(gen),
                "gens_out": len(gen_kept),
                "branches_in": len(branch),
                "branches_out": len(branch_kept),
                "isolated_buses_dropped": len(removed_buses),
                "keep_largest_component": bool(keep_largest),
                "connectivity_out": stats_out,
                "wrote_case": out_path,
                "wrote_report": report_path,
                "wrote_busmap": busmap_path,
            },
            indent=2,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, help="Input MATPOWER .m case")
    ap.add_argument("--out", required=True, help="Output pruned MATPOWER .m case")
    ap.add_argument("--report", required=True, help="Output JSON report path")
    ap.add_argument(
        "--busmap", required=True, help="Output JSON bus renumber mapping path"
    )
    ap.add_argument(
        "--keep-largest",
        action="store_true",
        help="After dropping isolated buses, keep only the largest connected component.",
    )
    ap.add_argument(
        "--case-name",
        default=None,
        help="Override output MATPOWER function name (default: <input>_pruned)",
    )
    args = ap.parse_args()

    drop_isolated_buses_case(
        case_path=args.case,
        out_path=args.out,
        report_path=args.report,
        busmap_path=args.busmap,
        keep_largest=bool(args.keep_largest),
        case_name=args.case_name,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

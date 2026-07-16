#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]

ap = argparse.ArgumentParser(
    description="""
Run from repository root.

Example:
python ./validation/matpower/matpower_pf_soundness.py --input ./data/rte.m
"""
)

# Optional SciPy acceleration (sparse + svds)
try:
    import scipy.sparse as sp  # type: ignore
    import scipy.sparse.linalg as spla  # type: ignore

    _HAVE_SCIPY = True
except Exception:
    sp = None  # type: ignore
    spla = None  # type: ignore
    _HAVE_SCIPY = False


# MATPOWER column indices (0-based)
# bus: [BUS_I, BUS_TYPE, PD, QD, GS, BS, BUS_AREA, VM, VA, BASE_KV, ZONE, VMAX, VMIN]
BUS_I, BUS_TYPE, PD, QD, GS, BS = 0, 1, 2, 3, 4, 5

# gen: [GEN_BUS, PG, QG, QMAX, QMIN, VG, MBASE, GEN_STATUS, PMAX, PMIN]
GEN_BUS, GEN_STATUS = 0, 7

# branch: [F_BUS, T_BUS, BR_R, BR_X, BR_B, RATE_A, RATE_B, RATE_C, TAP, SHIFT, BR_STATUS, ANGMIN, ANGMAX]
F_BUS, T_BUS, BR_R, BR_X, BR_B, TAP, SHIFT, BR_STATUS = 0, 1, 2, 3, 4, 8, 9, 10

# bus types
PQ, PV, REF, NONE = 1, 2, 3, 4


@dataclass
class IslandReport:
    island_id: int
    nbus: int
    ref_buses: List[int]
    condest_redY: Optional[float]


@dataclass
class Report:
    input_path: str
    baseMVA: Optional[float]
    errors: List[str]
    warnings: List[str]
    islands: List[IslandReport]
    summary: str


def _strip_matlab_comments(text: str) -> str:
    # remove line comments starting with % (MATLAB/Octave)
    out_lines: List[str] = []
    for line in text.splitlines():
        if "%" in line:
            line = line.split("%", 1)[0]
        out_lines.append(line)
    return "\n".join(out_lines)


def _extract_scalar(text: str, name: str) -> Optional[float]:
    m = re.search(
        rf"\bmpc\.{re.escape(name)}\s*=\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*;",
        text,
    )
    if not m:
        return None
    return float(m.group(1))


def _extract_matrix_block(text: str, name: str) -> Optional[str]:
    # non-greedy capture inside [...]
    m = re.search(rf"\bmpc\.{re.escape(name)}\s*=\s*\[(.*?)\]\s*;", text, flags=re.S)
    if not m:
        # allow empty matrix: mpc.bus = [];
        m2 = re.search(rf"\bmpc\.{re.escape(name)}\s*=\s*\[\s*\]\s*;", text)
        if m2:
            return ""
        return None
    block = m.group(1)
    # remove MATLAB continuation "..."
    block = block.replace("...", " ")
    return block


def _parse_mat_matrix(block: str, name: str) -> np.ndarray:
    block = block.strip()
    if block == "":
        return np.zeros((0, 0), dtype=float)

    # MATLAB allows rows separated by ';' OR by newlines.
    # Normalize: convert ';' to '\n', then split by lines.
    block = block.replace(",", " ")
    block = block.replace(";", "\n")

    rows = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        toks = ln.split()
        try:
            rows.append([float(t) for t in toks])
        except ValueError as e:
            raise ValueError(f"{name}: failed to parse numeric row: {ln}") from e

    if not rows:
        return np.zeros((0, 0), dtype=float)

    width = max(len(r) for r in rows)
    if any(len(r) != width for r in rows):
        raise ValueError(f"{name}: ragged rows detected (inconsistent column count).")

    return np.array(rows, dtype=float)


def _is_intish(x: float, tol: float = 1e-9) -> bool:
    return abs(x - round(x)) <= tol


def _union_find_components(
    n: int, edges: List[Tuple[int, int]]
) -> Tuple[np.ndarray, int]:
    parent = np.arange(n + 1, dtype=int)
    rank = np.zeros(n + 1, dtype=int)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for a, b in edges:
        union(a, b)

    roots = np.array([find(i) for i in range(1, n + 1)], dtype=int)
    # compress to 1..k
    uniq = sorted(set(int(r) for r in roots))
    remap = {r: i + 1 for i, r in enumerate(uniq)}
    comp = np.array([remap[int(r)] for r in roots], dtype=int)
    return comp, len(uniq)


def _build_ybus(
    baseMVA: float, bus: np.ndarray, branch: np.ndarray, warnings: List[str]
) -> "sp.csr_matrix | np.ndarray":
    nb = bus.shape[0]
    use_sparse = _HAVE_SCIPY and nb >= 200  # heuristic

    # shunts
    gs = bus[:, GS] / baseMVA
    bs = bus[:, BS] / baseMVA
    ysh = gs + 1j * bs

    if use_sparse:
        assert sp is not None
        # build COO triplets
        I: List[int] = []
        J: List[int] = []
        V: List[complex] = []

        # diagonal shunts
        for i in range(nb):
            if ysh[i] != 0:
                I.append(i)
                J.append(i)
                V.append(ysh[i])

        for k in range(branch.shape[0]):
            if int(round(branch[k, BR_STATUS])) == 0:
                continue

            f = int(round(branch[k, F_BUS])) - 1
            t = int(round(branch[k, T_BUS])) - 1

            r = float(branch[k, BR_R])
            x = float(branch[k, BR_X])
            b = float(branch[k, BR_B])

            if not (math.isfinite(r) and math.isfinite(x) and math.isfinite(b)):
                continue

            denom = complex(r, x)
            if abs(denom) == 0.0:
                warnings.append(
                    f"Branch {k}: zero (r,x); using epsilon to avoid division-by-zero in Ybus build."
                )
                denom = complex(1e-9, 0.0)

            ys = 1.0 / denom
            bc = 1j * b / 2.0

            tap = float(branch[k, TAP])
            shift = float(branch[k, SHIFT])

            if tap == 0.0:
                tap = 1.0

            tapc = tap * np.exp(1j * (shift * np.pi / 180.0))

            yff = (ys + bc) / (tapc * np.conj(tapc))
            ytt = ys + bc
            yft = -ys / np.conj(tapc)
            ytf = -ys / tapc

            # stamp
            I += [f, t, f, t]
            J += [f, t, t, f]
            V += [yff, ytt, yft, ytf]

        Y = sp.coo_matrix(
            (np.array(V, dtype=complex), (np.array(I), np.array(J))), shape=(nb, nb)
        ).tocsr()
        return Y

    # dense fallback
    Y = np.zeros((nb, nb), dtype=complex)
    Y[np.arange(nb), np.arange(nb)] += ysh

    for k in range(branch.shape[0]):
        if int(round(branch[k, BR_STATUS])) == 0:
            continue

        f = int(round(branch[k, F_BUS])) - 1
        t = int(round(branch[k, T_BUS])) - 1

        r = float(branch[k, BR_R])
        x = float(branch[k, BR_X])
        b = float(branch[k, BR_B])

        denom = complex(r, x)
        if abs(denom) == 0.0:
            warnings.append(
                f"Branch {k}: zero (r,x); using epsilon to avoid division-by-zero in Ybus build."
            )
            denom = complex(1e-9, 0.0)

        ys = 1.0 / denom
        bc = 1j * b / 2.0

        tap = float(branch[k, TAP])
        shift = float(branch[k, SHIFT])
        if tap == 0.0:
            tap = 1.0

        tapc = tap * np.exp(1j * (shift * np.pi / 180.0))

        yff = (ys + bc) / (tapc * np.conj(tapc))
        ytt = ys + bc
        yft = -ys / np.conj(tapc)
        ytf = -ys / tapc

        Y[f, f] += yff
        Y[t, t] += ytt
        Y[f, t] += yft
        Y[t, f] += ytf

    return Y


def _condest(A: "sp.csr_matrix | np.ndarray") -> float:
    n = A.shape[0]
    if n <= 1:
        return float("nan")

    if _HAVE_SCIPY and sp is not None and isinstance(A, sp.spmatrix):
        # estimate condition number via extreme singular values
        try:
            smax = float(
                spla.svds(A, k=1, which="LM", return_singular_vectors=False)[0]
            )
            smin = float(
                spla.svds(A, k=1, which="SM", return_singular_vectors=False)[0]
            )
            if not (math.isfinite(smax) and math.isfinite(smin)) or smin <= 0.0:
                return float("inf")
            return smax / smin
        except Exception:
            # fallback: densify this block
            Ad = A.toarray()
            return float(np.linalg.cond(Ad))

    # dense
    return float(np.linalg.cond(np.asarray(A)))


def load_matpower_m(
    path: str,
) -> Tuple[Optional[float], np.ndarray, np.ndarray, np.ndarray]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    txt = _strip_matlab_comments(raw)

    baseMVA = _extract_scalar(txt, "baseMVA")

    bus_blk = _extract_matrix_block(txt, "bus")
    gen_blk = _extract_matrix_block(txt, "gen")
    br_blk = _extract_matrix_block(txt, "branch")

    if bus_blk is None or gen_blk is None or br_blk is None:
        missing = [
            n
            for n, b in [("bus", bus_blk), ("gen", gen_blk), ("branch", br_blk)]
            if b is None
        ]
        raise ValueError(f"Missing required matrix assignment(s): {', '.join(missing)}")

    bus = _parse_mat_matrix(bus_blk, "bus")
    gen = _parse_mat_matrix(gen_blk, "gen")
    branch = _parse_mat_matrix(br_blk, "branch")

    if bus.size == 0:
        raise ValueError("bus matrix is empty.")
    if branch.size == 0:
        # allow; connectivity checks still run (islands become isolated buses)
        branch = np.zeros((0, 13), dtype=float)

    return baseMVA, bus, gen, branch


def normalize_internal_numbering(
    bus: np.ndarray,
    gen: np.ndarray,
    branch: np.ndarray,
    errors: List[str],
    warnings: List[str],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    if bus.shape[1] < 13:
        errors.append(f"bus has {bus.shape[1]} columns; MATPOWER expects >= 13.")
        return bus, gen, branch
    if gen.size and gen.shape[1] < 10:
        errors.append(f"gen has {gen.shape[1]} columns; MATPOWER expects >= 10.")
        return bus, gen, branch
    if branch.size and branch.shape[1] < 13:
        errors.append(f"branch has {branch.shape[1]} columns; MATPOWER expects >= 13.")
        return bus, gen, branch

    bus_ids = bus[:, BUS_I]
    if not np.all([_is_intish(float(x)) for x in bus_ids]):
        errors.append("BUS_I contains non-integer bus identifiers.")
        return bus, gen, branch

    bus_ids_i = np.array([int(round(float(x))) for x in bus_ids], dtype=int)

    if len(set(bus_ids_i.tolist())) != bus_ids_i.size:
        errors.append("Duplicate bus IDs detected in BUS_I.")
        return bus, gen, branch

    # strict MATPOWER expectation (many loaders assume bus rows are ordered)
    if not np.all(bus_ids_i == np.sort(bus_ids_i)):
        errors.append("Bus rows are not sorted by BUS_I (ascending).")

    # Remap to 1..nb using sorted BUS_I
    old_sorted = np.sort(bus_ids_i)
    mapping: Dict[int, int] = {old: new for new, old in enumerate(old_sorted, start=1)}

    nb = bus.shape[0]
    bus2 = bus.copy()
    # reorder bus rows by ascending BUS_I
    order = np.argsort(bus_ids_i)
    bus2 = bus2[order, :]
    bus2[:, BUS_I] = np.array(
        [mapping[int(round(x))] for x in bus2[:, BUS_I]], dtype=float
    )

    gen2 = gen.copy()
    if gen2.size:
        gb = gen2[:, GEN_BUS]
        if not np.all([_is_intish(float(x)) for x in gb]):
            errors.append("GEN_BUS contains non-integer bus references.")
        gb_i = np.array([int(round(float(x))) for x in gb], dtype=int)
        missing = sorted(set(gb_i.tolist()) - set(bus_ids_i.tolist()))
        if missing:
            errors.append(
                f"Generator references missing BUS_I values: {missing[:10]}"
                + (" ..." if len(missing) > 10 else "")
            )
        gen2[:, GEN_BUS] = np.array([mapping.get(b, -1) for b in gb_i], dtype=float)

    br2 = branch.copy()
    if br2.size:
        fb = br2[:, F_BUS]
        tb = br2[:, T_BUS]
        if not np.all([_is_intish(float(x)) for x in fb]) or not np.all(
            [_is_intish(float(x)) for x in tb]
        ):
            errors.append("Branch F_BUS/T_BUS contains non-integer bus references.")
        fb_i = np.array([int(round(float(x))) for x in fb], dtype=int)
        tb_i = np.array([int(round(float(x))) for x in tb], dtype=int)
        missing = sorted(
            (set(fb_i.tolist()) | set(tb_i.tolist())) - set(bus_ids_i.tolist())
        )
        if missing:
            errors.append(
                f"Branch references missing BUS_I values: {missing[:10]}"
                + (" ..." if len(missing) > 10 else "")
            )
        br2[:, F_BUS] = np.array([mapping.get(b, -1) for b in fb_i], dtype=float)
        br2[:, T_BUS] = np.array([mapping.get(b, -1) for b in tb_i], dtype=float)

    # internal numbering should now be 1..nb exactly
    if not np.all(bus2[:, BUS_I] == np.arange(1, nb + 1, dtype=float)):
        errors.append("Internal renumbering failed to produce BUS_I == 1..nb.")

    return bus2, gen2, br2


def validate(case_path: str) -> Report:
    errors: List[str] = []
    warnings: List[str] = []
    islands: List[IslandReport] = []

    baseMVA: Optional[float] = None
    try:
        baseMVA, bus, gen, branch = load_matpower_m(case_path)
    except Exception as e:
        return Report(
            input_path=case_path,
            baseMVA=None,
            errors=[f"Failed to parse MATPOWER .m file: {e}"],
            warnings=[],
            islands=[],
            summary="Errors: 1 | Warnings: 0 | Islands: 0",
        )

    if baseMVA is None or not math.isfinite(float(baseMVA)) or float(baseMVA) <= 0:
        errors.append("baseMVA missing or invalid (must be finite > 0).")

    # numeric finiteness
    if not np.isfinite(bus).all():
        errors.append("bus contains NaN/Inf.")
    if gen.size and not np.isfinite(gen).all():
        errors.append("gen contains NaN/Inf.")
    if branch.size and not np.isfinite(branch).all():
        errors.append("branch contains NaN/Inf.")

    # normalize numbering for downstream checks (even if ordering error exists)
    bus, gen, branch = normalize_internal_numbering(bus, gen, branch, errors, warnings)

    nb = bus.shape[0]
    nl = branch.shape[0]
    ng = gen.shape[0] if gen.size else 0

    # bus type sanity
    bt = bus[:, BUS_TYPE].astype(int, copy=False)
    bad_types = np.setdiff1d(np.unique(bt), np.array([PQ, PV, REF, NONE]))
    if bad_types.size:
        errors.append(f"Invalid BUS_TYPE values present: {bad_types.tolist()}")

    # generator bus refs after normalization
    if ng:
        gb = gen[:, GEN_BUS].astype(int, copy=False)
        if np.any((gb < 1) | (gb > nb)):
            errors.append(
                "Generator references out-of-range internal bus index after renumbering."
            )
        gs = gen[:, GEN_STATUS]
        if not np.all([_is_intish(float(x)) for x in gs]):
            errors.append("GEN_STATUS contains non-integer values.")
        elif np.any((gs != 0) & (gs != 1)):
            warnings.append(
                "GEN_STATUS contains values other than 0/1 (treated as nonzero=in-service by many tools)."
            )

    # branch param checks
    if nl:
        status = branch[:, BR_STATUS]
        on = status != 0

        r = branch[:, BR_R]
        x = branch[:, BR_X]
        tap = branch[:, TAP]

        z0 = on & ((np.abs(r) + np.abs(x)) == 0)
        if np.any(z0):
            warnings.append(f"In-service zero-impedance branches: {int(np.sum(z0))}")

        neg = on & ((r < 0) | (x < 0))
        if np.any(neg):
            warnings.append(
                f"In-service branches with negative R or X: {int(np.sum(neg))}"
            )

        badtap = on & (~np.isfinite(tap) | (tap < 0))
        if np.any(badtap):
            errors.append(
                f"Invalid transformer tap ratios on {int(np.sum(badtap))} in-service branches."
            )

        # internal bus index validity
        fb = branch[:, F_BUS].astype(int, copy=False)
        tb = branch[:, T_BUS].astype(int, copy=False)
        if np.any((fb < 1) | (fb > nb) | (tb < 1) | (tb > nb)):
            errors.append(
                "Branch references out-of-range internal bus index after renumbering."
            )

    # connectivity + islands
    edges: List[Tuple[int, int]] = []
    deg = np.zeros(nb + 1, dtype=int)
    if nl:
        on = branch[:, BR_STATUS] != 0
        fb = branch[on, F_BUS].astype(int, copy=False)
        tb = branch[on, T_BUS].astype(int, copy=False)
        for a, b in zip(fb.tolist(), tb.tolist()):
            if a <= 0 or b <= 0:
                continue
            edges.append((a, b))
            deg[a] += 1
            deg[b] += 1

    comp, ncomp = (
        _union_find_components(nb, edges) if nb else (np.zeros(0, dtype=int), 0)
    )

    # isolated buses not marked NONE
    iso = [
        i for i in range(1, nb + 1) if deg[i] == 0 and int(bus[i - 1, BUS_TYPE]) != NONE
    ]
    if iso:
        errors.append(
            f"Isolated buses not marked NONE: count={len(iso)} (first: {iso[:10]})"
        )

    # build Ybus only if there is at least one REF bus somewhere
    # (otherwise reduced-Y checks are skipped anyway, so building Ybus is wasted time)
    Ybus = None
    has_any_ref = np.any(bus[:, BUS_TYPE].astype(int, copy=False) == REF)

    if (
        has_any_ref
        and baseMVA is not None
        and math.isfinite(float(baseMVA))
        and float(baseMVA) > 0
    ):
        try:
            Ybus = _build_ybus(float(baseMVA), bus, branch, warnings)
        except Exception as e:
            errors.append(f"Failed to build Ybus: {e}")
            Ybus = None

    for k in range(1, ncomp + 1):
        buses_k = np.where(comp == k)[0] + 1  # 1-based bus indices
        types_k = bus[buses_k - 1, BUS_TYPE].astype(int, copy=False)
        refs_k = buses_k[types_k == REF].tolist()

        if not refs_k:
            errors.append(f"Island {k} has no REF (slack) bus.")
        elif len(refs_k) > 1:
            warnings.append(f"Island {k} has multiple REF buses ({len(refs_k)}).")

        cnd: Optional[float] = None
        if Ybus is not None and refs_k and len(buses_k) > 1:
            ref = refs_k[0]
            keep = [b for b in buses_k.tolist() if b != ref]
            idx0 = np.array([b - 1 for b in keep], dtype=int)
            try:
                if _HAVE_SCIPY and sp is not None and isinstance(Ybus, sp.spmatrix):
                    Yred = Ybus[idx0[:, None], idx0]
                else:
                    Yred = np.asarray(Ybus)[np.ix_(idx0, idx0)]
                cnd = _condest(Yred)
                if (not math.isfinite(float(cnd))) or float(cnd) > 1e12:
                    warnings.append(
                        f"Island {k} reduced Ybus ill-conditioned (condest ~ {cnd:.3g})."
                    )
            except Exception as e:
                warnings.append(f"Island {k} reduced Ybus condest failed: {e}")
                cnd = None

        islands.append(
            IslandReport(
                island_id=k, nbus=int(len(buses_k)), ref_buses=refs_k, condest_redY=cnd
            )
        )

    summary = f"Errors: {len(errors)} | Warnings: {len(warnings)} | Islands: {ncomp}"
    return Report(
        input_path=case_path,
        baseMVA=baseMVA,
        errors=errors,
        warnings=warnings,
        islands=islands,
        summary=summary,
    )


def _print_report(r: Report) -> None:
    print(r.summary)
    print(f"Input: {r.input_path}")
    if r.baseMVA is not None:
        print(f"baseMVA: {r.baseMVA}")
    print()

    if r.errors:
        print("ERRORS:")
        for e in r.errors:
            print(f"  - {e}")
        print()

    if r.warnings:
        print("WARNINGS:")
        for w in r.warnings:
            print(f"  - {w}")
        print()

    if r.islands:
        print("ISLANDS:")
        for isl in r.islands:
            cnd = (
                "None"
                if isl.condest_redY is None
                else (
                    f"{isl.condest_redY:.3g}"
                    if math.isfinite(float(isl.condest_redY))
                    else str(isl.condest_redY)
                )
            )
            print(
                f"  - id={isl.island_id} nbus={isl.nbus} ref_buses={isl.ref_buses} condest_redY={cnd}"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        required=True,
        help="Path to MATPOWER .m case file relative to the repository root",
    )
    ap.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON report"
    )
    args = ap.parse_args()

    case_path = REPO_ROOT / args.input

    if not case_path.exists():
        print(f"ERROR: file not found: {case_path}", file=sys.stderr)
        return 2

    case_path = str(case_path)

    r = validate(case_path)

    if args.json:
        print(json.dumps(asdict(r), indent=2))
    else:
        _print_report(r)

    return 1 if r.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from processing_utils.graph_checks import connectivity_stats as graph_connectivity_stats


def basic_counts(loader: Any) -> Dict[str, int]:
    t = loader.load_tables()
    return {
        "buses_total": int(len(t.get("bus", []))),
        "gens_total": int(len(t.get("gen", []))),
        "branches_total": int(len(t.get("branch", []))),
        "gencost_total": int(len(t.get("gencost", []))),
    }


def counts_for_plot(loader: Any) -> Dict[str, int]:
    c = basic_counts(loader)
    return {
        "buses": c["buses_total"],
        "gens": c["gens_total"],
        "branches": c["branches_total"],
    }


def duplicate_and_empty_checks(loader: Any) -> Dict[str, Any]:
    t = loader.load_tables()
    bus = t.get("bus")
    out: Dict[str, Any] = {}

    if isinstance(bus, pd.DataFrame) and "bus_id" in bus.columns:
        ids = bus["bus_id"]
        out["bus_id_null"] = int(ids.isna().sum())
        out["bus_id_duplicates"] = int(ids.dropna().duplicated().sum())
    else:
        out["bus_id_null"] = None
        out["bus_id_duplicates"] = None

    branch = t.get("branch")
    if isinstance(branch, pd.DataFrame):
        cols = [
            c
            for c in ["from_bus", "to_bus", "BR_R", "BR_X", "BR_STATUS"]
            if c in branch.columns
        ]
        out["branch_null_pct"] = {
            c: float(branch[c].isna().mean() * 100.0) for c in cols
        }
    else:
        out["branch_null_pct"] = {}

    gen = t.get("gen")
    if isinstance(gen, pd.DataFrame):
        cols = [c for c in ["bus_id", "PG", "QG", "GEN_STATUS"] if c in gen.columns]
        out["gen_null_pct"] = {c: float(gen[c].isna().mean() * 100.0) for c in cols}
    else:
        out["gen_null_pct"] = {}

    return out


def endpoint_integrity(loader: Any) -> Dict[str, Any]:
    t = loader.load_tables()
    bus = t.get("bus")
    branch = t.get("branch")
    gen = t.get("gen")

    if not isinstance(bus, pd.DataFrame) or "bus_id" not in bus.columns:
        return {}

    bus_ids = set(bus["bus_id"].dropna().astype(int).tolist())

    def _bad(df: Optional[pd.DataFrame], col: str) -> Tuple[int, List[int]]:
        if not isinstance(df, pd.DataFrame) or col not in df.columns:
            return 0, []
        vals = df[col].dropna().astype(int).tolist()
        bad = sorted(list(set(vals) - bus_ids))
        return int(len(bad)), bad[:20]

    bad_from_n, bad_from_s = _bad(branch, "from_bus")
    bad_to_n, bad_to_s = _bad(branch, "to_bus")
    bad_gen_n, bad_gen_s = _bad(gen, "bus_id")

    return {
        "branch_bad_from_bus_count": bad_from_n,
        "branch_bad_to_bus_count": bad_to_n,
        "branch_bad_from_bus_sample": bad_from_s,
        "branch_bad_to_bus_sample": bad_to_s,
        "gen_bad_bus_count": bad_gen_n,
        "gen_bad_bus_sample": bad_gen_s,
    }


def connectivity_stats(
    loader: Any, *, include_only_in_service: bool = True
) -> Dict[str, Any]:
    t = loader.load_tables()
    bus = t.get("bus")
    branch = t.get("branch")

    if not isinstance(bus, pd.DataFrame) or not isinstance(branch, pd.DataFrame):
        return {}

    if (
        "bus_id" not in bus.columns
        or "from_bus" not in branch.columns
        or "to_bus" not in branch.columns
    ):
        return {}

    nodes = bus["bus_id"].dropna().astype(int).tolist()
    br = branch
    if include_only_in_service and "in_service" in br.columns:
        br = br[br["in_service"].fillna(False)]

    edges = list(
        zip(
            br["from_bus"].dropna().astype(int).tolist(),
            br["to_bus"].dropna().astype(int).tolist(),
        )
    )

    stats = graph_connectivity_stats(
        nodes, edges, isolated_sample=20, top_components=10
    )
    return {
        "nodes": int(stats["nodes"]),
        "edges": int(stats["edges"]),
        "connected_components": int(stats["connected_components"]),
        "largest_component_size": int(stats["largest_component_size"]),
        "top_component_sizes": list(stats["top_component_sizes"]),
        "isolated_buses_count": int(stats["isolated_buses_count"]),
        "isolated_buses_sample": list(stats["isolated_buses_sample"]),
        "edges_only_in_service": bool(include_only_in_service),
    }

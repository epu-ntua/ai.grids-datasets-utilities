from __future__ import annotations

from typing import Any


def build_nx_graph_from_bundle(
    bundle: dict[str, Any],
    *,
    include_only_in_service: bool = True,
    include_trafos: bool = True,
):
    import networkx as nx

    nodes = bundle.get("nodes")
    edges = bundle.get("edges")

    G = nx.Graph()

    if nodes is not None and not nodes.empty and "node_id" in nodes.columns:
        for r in nodes.itertuples(index=False):
            nid = str(getattr(r, "node_id"))
            G.add_node(
                nid,
                vn_kv=getattr(r, "vn_kv", None),
                has_load=getattr(r, "has_load", None),
                has_sgen=getattr(r, "has_sgen", None),
                is_slack=getattr(r, "is_slack", None),
                in_service=getattr(r, "in_service", None),
            )

    if edges is None:
        return G

    e = edges.copy()
    if include_only_in_service and "in_service" in e.columns:
        e = e[e["in_service"].fillna(True)]

    if not include_trafos and "edge_type" in e.columns:
        e = e[e["edge_type"] != "trafo"]

    cols = set(e.columns)
    for r in e.itertuples(index=False):
        u = str(getattr(r, "src"))
        v = str(getattr(r, "dst"))
        if not u.strip() or not v.strip():
            continue
        G.add_edge(
            u,
            v,
            edge_id=getattr(r, "edge_id", None) if "edge_id" in cols else None,
            edge_type=getattr(r, "edge_type", None) if "edge_type" in cols else None,
            in_service=getattr(r, "in_service", None) if "in_service" in cols else None,
        )

    return G

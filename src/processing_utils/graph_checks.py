from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple


def _build_components(
    nodes: Sequence[int],
    edges: Iterable[Tuple[int, int]],
) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, int]]:
    parent = {n: n for n in nodes}
    size = {n: 1 for n in nodes}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        if a not in parent or b not in parent:
            return
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if size[ra] < size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size[ra] += size[rb]

    for u, v in edges:
        union(u, v)

    comp_sizes: Dict[int, int] = {}
    for n in nodes:
        r = find(n)
        comp_sizes[r] = comp_sizes.get(r, 0) + 1

    return parent, size, comp_sizes


def connectivity_stats(
    nodes: Sequence[int],
    edges: Sequence[Tuple[int, int]],
    *,
    isolated_sample: int = 20,
    top_components: int = 10,
) -> Dict[str, object]:
    if not nodes:
        return {
            "nodes": 0,
            "edges": int(len(edges)),
            "connected_components": 0,
            "largest_component_size": 0,
            "top_component_sizes": [],
            "isolated_buses_count": 0,
            "isolated_buses_sample": [],
        }

    parent, _size, comp_sizes = _build_components(nodes, edges)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    sizes_sorted = sorted(comp_sizes.values(), reverse=True)
    isolated = [n for n in nodes if comp_sizes.get(find(n), 0) == 1]

    return {
        "nodes": int(len(nodes)),
        "edges": int(len(edges)),
        "connected_components": int(len(comp_sizes)),
        "largest_component_size": int(sizes_sorted[0]) if sizes_sorted else 0,
        "top_component_sizes": sizes_sorted[: int(top_components)],
        "isolated_buses_count": int(len(isolated)),
        "isolated_buses_sample": isolated[: int(isolated_sample)],
    }


def small_components_stats(
    nodes: Sequence[int],
    edges: Sequence[Tuple[int, int]],
    *,
    max_size: int = 5,
    sample: int = 10,
) -> Dict[str, object]:
    if not nodes:
        return {
            f"small_components_count_(<={max_size})": 0,
            "small_components_sample": [],
        }

    parent, _size, comp_sizes = _build_components(nodes, edges)

    comps: Dict[int, List[int]] = {}
    for n in nodes:
        r = n
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        comps.setdefault(r, []).append(n)

    small = [sorted(v) for v in comps.values() if len(v) <= int(max_size)]
    small_sorted = sorted(small, key=lambda x: (len(x), x[0] if x else -1))

    return {
        f"small_components_count_(<={max_size})": int(len(small_sorted)),
        "small_components_sample": small_sorted[: int(sample)],
    }

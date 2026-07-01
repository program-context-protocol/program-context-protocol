"""Cluster detection: find natural module boundaries from dependency graph.

Algorithm: Union-Find on bidirectional edges, then group by top-level directory.
Directory grouping is the primary signal — import graph refines it.
Files with no cluster affinity (shared utils, config) → 'shared' pseudo-cluster.
"""

from collections import defaultdict
from pathlib import Path


class _UnionFind:
    def __init__(self, nodes):
        self.parent = {n: n for n in nodes}
        self.rank = {n: 0 for n in nodes}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _top_dir(file_key: str) -> str:
    parts = file_key.split("/")
    if len(parts) == 1:
        return "__root__"
    # skip 'src/' prefix as it's not a domain
    if parts[0] in ("src", "lib", "app", "pkg") and len(parts) > 2:
        return parts[1]
    return parts[0]


def detect_clusters(
    graph: dict[str, set[str]],
    files: list[Path],
    root: Path,
) -> dict[str, list[str]]:
    """
    Returns {cluster_name: [file_key, ...]}

    Strategy:
    1. Primary grouping: top-level directory (or src/<dir>)
    2. Refinement: if two directories are heavily cross-coupled (>50% of edges
       cross between them), merge into one cluster
    3. Root-level files with no dir affinity → 'shared'
    """
    file_keys = [str(f.relative_to(root)) for f in files]

    # Step 1: group by top-level dir
    dir_groups: dict[str, list[str]] = defaultdict(list)
    for key in file_keys:
        dir_groups[_top_dir(key)].append(key)

    # Step 2: count cross-dir edges
    cross: dict[tuple[str, str], int] = defaultdict(int)
    internal: dict[str, int] = defaultdict(int)

    for src, targets in graph.items():
        src_dir = _top_dir(src)
        for tgt in targets:
            tgt_dir = _top_dir(tgt)
            if src_dir == tgt_dir:
                internal[src_dir] += 1
            else:
                key = tuple(sorted([src_dir, tgt_dir]))
                cross[key] += 1

    # Step 3: merge dirs where cross-edges > internal edges of both (tight coupling)
    uf = _UnionFind(list(dir_groups.keys()))
    for (a, b), cross_count in cross.items():
        total_a = internal[a] + cross_count
        total_b = internal[b] + cross_count
        if total_a == 0 or total_b == 0:
            continue
        # merge if cross traffic is dominant in either direction
        if cross_count / total_a > 0.6 or cross_count / total_b > 0.6:
            uf.union(a, b)

    # Step 4: build final clusters
    clusters: dict[str, list[str]] = defaultdict(list)
    for dir_name, file_list in dir_groups.items():
        cluster_root = uf.find(dir_name)
        # name: use the most specific dir in the merged group
        cluster_name = cluster_root if cluster_root != "__root__" else "shared"
        clusters[cluster_name].extend(file_list)

    # rename __root__ entries
    if "__root__" in clusters:
        clusters["shared"].extend(clusters.pop("__root__"))

    return dict(clusters)


def compute_coupling_matrix(
    graph: dict[str, set[str]],
    clusters: dict[str, list[str]],
) -> dict[tuple[str, str], int]:
    """Count cross-cluster edges. High count = coupling violation."""
    file_to_cluster = {}
    for cluster, files in clusters.items():
        for f in files:
            file_to_cluster[f] = cluster

    cross_edges: dict[tuple[str, str], int] = defaultdict(int)
    for src, targets in graph.items():
        src_cluster = file_to_cluster.get(src)
        for tgt in targets:
            tgt_cluster = file_to_cluster.get(tgt)
            if src_cluster and tgt_cluster and src_cluster != tgt_cluster:
                key = (src_cluster, tgt_cluster)
                cross_edges[key] += 1

    return dict(cross_edges)

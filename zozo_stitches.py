# SPDX-License-Identifier: GPL-3.0-or-later
"""ZOZO 用の緩い縫い辺（stitch）を服メッシュ上に再構築する。

マーベラスデザイナー由来の服はパネルが分離したまま着装位置に並んでいることが
多く、Blender 上の loose sewing edge は欠けている。ZOZO Contact Solver は
その緩い辺をステッチとして閉じるため、書き出し時に作り直す。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import bpy
import bmesh
import numpy as np


# 着装済みパネル同士の境界がほぼ接している前提の対応距離。
_DEFAULT_MAX_DIST_M = 0.008
# 同一パネル内ダーツ等: 境界上で近いが隣接していない頂点対。
_SELF_MAX_DIST_M = 0.006
_SELF_MIN_BOUNDARY_HOPS = 8


@dataclass(frozen=True)
class StitchRebuildReport:
    stitch_count: int
    added_edges: int
    component_count: int
    component_pairs_used: int
    max_gap_m: float
    mean_gap_m: float
    method: str

    def summary_ja(self) -> str:
        if self.stitch_count <= 0:
            return "縫い辺 0 本（再構築できず）"
        return (
            f"縫い辺 {self.stitch_count} 本を再構築"
            f"（追加 {self.added_edges}、パネル {self.component_count}、"
            f"組 {self.component_pairs_used}、"
            f"すきま 平均 {self.mean_gap_m * 1000.0:.2f} mm / "
            f"最大 {self.max_gap_m * 1000.0:.2f} mm、{self.method}）"
        )


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    mesh = obj.data
    local = np.empty((len(mesh.vertices), 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in obj.matrix_world], dtype=np.float64)
    return np.ascontiguousarray(local @ matrix[:3, :3].T + matrix[:3, 3])


def _connected_components(mesh: bpy.types.Mesh) -> list[np.ndarray]:
    n = len(mesh.vertices)
    adj: list[set[int]] = [set() for _ in range(n)]
    for edge in mesh.edges:
        a, b = int(edge.vertices[0]), int(edge.vertices[1])
        adj[a].add(b)
        adj[b].add(a)
    seen = np.zeros(n, dtype=bool)
    comps: list[np.ndarray] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        members: list[int] = []
        while stack:
            v = stack.pop()
            members.append(v)
            for nb in adj[v]:
                if not seen[nb]:
                    seen[nb] = True
                    stack.append(nb)
        comps.append(np.asarray(members, dtype=np.int32))
    return comps


def _boundary_vertices(mesh: bpy.types.Mesh) -> set[int]:
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for poly in mesh.polygons:
        verts = list(poly.vertices)
        for i, a in enumerate(verts):
            b = verts[(i + 1) % len(verts)]
            counts[tuple(sorted((int(a), int(b))))] += 1
    out: set[int] = set()
    for (a, b), c in counts.items():
        if c == 1:
            out.add(a)
            out.add(b)
    return out


def _pair_boundaries(
    verts_a: np.ndarray,
    world: np.ndarray,
    verts_b: np.ndarray,
    *,
    max_dist_m: float,
) -> list[tuple[int, int, float]]:
    """一意な最近傍対応（A→B 貪欲）。"""
    if verts_a.size == 0 or verts_b.size == 0:
        return []
    wa = world[verts_a]
    wb = world[verts_b]
    # 中規模メッシュ向け: B をそのまま、A を走査
    used_b: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    # 近い候補から取るため、各 A の最小距離でソート
    candidates: list[tuple[float, int, int]] = []
    for ia, va in enumerate(verts_a):
        d = np.linalg.norm(wb - wa[ia], axis=1)
        jb = int(np.argmin(d))
        dist = float(d[jb])
        if dist <= max_dist_m:
            candidates.append((dist, int(va), int(verts_b[jb])))
    candidates.sort(key=lambda item: item[0])
    used_a: set[int] = set()
    for dist, va, vb in candidates:
        if va in used_a or vb in used_b:
            continue
        used_a.add(va)
        used_b.add(vb)
        pairs.append((va, vb, dist))
    return pairs


def _self_pairs_on_component(
    verts: np.ndarray,
    world: np.ndarray,
    mesh: bpy.types.Mesh,
    boundary: set[int],
    *,
    max_dist_m: float,
) -> list[tuple[int, int, float]]:
    """同一成分内の近い境界頂点（ダーツ・袖閉じ等）。"""
    bd = np.asarray([int(v) for v in verts if int(v) in boundary], dtype=np.int32)
    if bd.size < 4:
        return []
    # 境界グラフの隣接
    edge_face: dict[tuple[int, int], int] = defaultdict(int)
    for poly in mesh.polygons:
        vs = list(poly.vertices)
        for i, a in enumerate(vs):
            b = vs[(i + 1) % len(vs)]
            edge_face[tuple(sorted((int(a), int(b))))] += 1
    boundary_adj: dict[int, set[int]] = defaultdict(set)
    for (a, b), c in edge_face.items():
        if c == 1 and a in boundary and b in boundary:
            boundary_adj[a].add(b)
            boundary_adj[b].add(a)

    def hops(u: int, v: int, limit: int = 64) -> int:
        if u == v:
            return 0
        q = [u]
        dist = {u: 0}
        while q:
            x = q.pop(0)
            if dist[x] >= limit:
                continue
            for y in boundary_adj.get(x, ()):
                if y not in dist:
                    dist[y] = dist[x] + 1
                    if y == v:
                        return dist[y]
                    q.append(y)
        return 10**9

    wb = world[bd]
    candidates: list[tuple[float, int, int]] = []
    for i in range(len(bd)):
        d = np.linalg.norm(wb - wb[i], axis=1)
        for j in range(i + 1, len(bd)):
            dist = float(d[j])
            if dist <= 1.0e-9 or dist > max_dist_m:
                continue
            va, vb = int(bd[i]), int(bd[j])
            if vb in boundary_adj.get(va, ()):
                continue
            if hops(va, vb) < _SELF_MIN_BOUNDARY_HOPS:
                continue
            candidates.append((dist, va, vb))
    candidates.sort(key=lambda item: item[0])
    used: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for dist, va, vb in candidates:
        if va in used or vb in used:
            continue
        used.add(va)
        used.add(vb)
        pairs.append((va, vb, dist))
    return pairs


def _add_loose_edges(
    obj: bpy.types.Object, pairs: list[tuple[int, int]]
) -> tuple[int, set[tuple[int, int]]]:
    """緩い辺を追加し、最終的な stitch キー集合を返す。"""
    mesh = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        existing = {
            tuple(sorted((e.verts[0].index, e.verts[1].index))) for e in bm.edges
        }
        wanted: set[tuple[int, int]] = set()
        added = 0
        for a, b in pairs:
            if a == b:
                continue
            key = tuple(sorted((int(a), int(b))))
            wanted.add(key)
            if key in existing:
                continue
            try:
                bm.edges.new((bm.verts[key[0]], bm.verts[key[1]]))
                existing.add(key)
                added += 1
            except ValueError:
                pass
        bm.to_mesh(mesh)
        mesh.update(calc_edges=True, calc_edges_loose=True)
    finally:
        bm.free()
    return added, wanted


def _tag_stitch_edges(obj: bpy.types.Object, stitch_keys: set[tuple[int, int]]) -> int:
    mesh = obj.data
    attr = mesh.attributes.get("ominaeshi_zozo_stitch")
    if attr is None:
        attr = mesh.attributes.new(
            name="ominaeshi_zozo_stitch", type="BOOLEAN", domain="EDGE"
        )
    found = 0
    for edge in mesh.edges:
        key = tuple(sorted((int(edge.vertices[0]), int(edge.vertices[1]))))
        is_stitch = key in stitch_keys
        attr.data[edge.index].value = bool(is_stitch)
        if is_stitch:
            found += 1
    return found


def rebuild_zozo_stitches(
    obj: bpy.types.Object,
    *,
    max_dist_m: float = _DEFAULT_MAX_DIST_M,
    include_self_seams: bool = True,
) -> StitchRebuildReport:
    """服オブジェクト上に ZOZO 向け縫い辺を再構築する（元トポロジに緩い辺を追加）。"""
    if obj is None or obj.type != "MESH":
        return StitchRebuildReport(0, 0, 0, 0, 0.0, 0.0, "none")

    mesh = obj.data
    world = _world_vertices(obj)
    comps = _connected_components(mesh)
    boundary = _boundary_vertices(mesh)

    all_pairs: list[tuple[int, int, float]] = []
    component_pairs_used = 0

    for i in range(len(comps)):
        bi = np.asarray([int(v) for v in comps[i] if int(v) in boundary], dtype=np.int32)
        for j in range(i + 1, len(comps)):
            bj = np.asarray(
                [int(v) for v in comps[j] if int(v) in boundary], dtype=np.int32
            )
            pairs = _pair_boundaries(bi, world, bj, max_dist_m=max_dist_m)
            if pairs:
                component_pairs_used += 1
                all_pairs.extend(pairs)

    if include_self_seams and len(comps) >= 1:
        for comp in comps:
            all_pairs.extend(
                _self_pairs_on_component(
                    comp, world, mesh, boundary, max_dist_m=_SELF_MAX_DIST_M
                )
            )

    # 一意化
    best: dict[tuple[int, int], float] = {}
    for a, b, dist in all_pairs:
        key = tuple(sorted((int(a), int(b))))
        if key[0] == key[1]:
            continue
        prev = best.get(key)
        if prev is None or dist < prev:
            best[key] = dist

    ordered = sorted(best.items(), key=lambda item: item[1])
    edge_list = [key for key, _ in ordered]
    gaps = [dist for _, dist in ordered]

    added, stitch_keys = _add_loose_edges(obj, edge_list)
    found = _tag_stitch_edges(obj, stitch_keys)

    max_gap = float(max(gaps)) if gaps else 0.0
    mean_gap = float(sum(gaps) / len(gaps)) if gaps else 0.0
    return StitchRebuildReport(
        stitch_count=int(found),
        added_edges=int(added),
        component_count=len(comps),
        component_pairs_used=int(component_pairs_used),
        max_gap_m=max_gap,
        mean_gap_m=mean_gap,
        method="境界最近傍",
    )

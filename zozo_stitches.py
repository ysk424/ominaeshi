# SPDX-License-Identifier: GPL-3.0-or-later
"""ZOZO 用の緩い縫い辺（stitch）を服メッシュ上に再構築する。

マーベラスデザイナー由来の服はパネルが分離したまま着装位置に並んでいることが
多く、Blender 上の loose sewing edge は欠けている。ZOZO Contact Solver は
その緩い辺をステッチとして閉じるため、書き出し時に作り直す。

対象:
  * パネル間縫い: 異なる連結成分の境界最近傍
  * 自己縫い: 同一パネルを筒にする袖下、および内部境界のダーツ閉じ
    （着装後に座標が重なっていても、距離 0 を除外しない）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import bpy
import bmesh
import numpy as np


# 着装済みパネル同士の境界がほぼ接している前提の対応距離。
_DEFAULT_MAX_DIST_M = 0.008
# 同一パネル内の筒閉じ（袖下など）: 着装後に重なる／近接する非隣接境界。
# 距離 0 も含む（MD 着装で位置は一致しているがトポロジが分かれたままの頂点対）。
_SELF_MAX_DIST_M = 0.008
# 境界ループ上の最小ホップ。短いループでは n//4 に落とす。
_SELF_MIN_BOUNDARY_HOPS = 8
# 内部境界ループ（ダーツ穴）とみなす外周以外のループの最大頂点数。
_SELF_INTERNAL_LOOP_MAX_VERTS = 64


@dataclass(frozen=True)
class StitchRebuildReport:
    stitch_count: int
    added_edges: int
    component_count: int
    component_pairs_used: int
    max_gap_m: float
    mean_gap_m: float
    method: str
    self_stitch_count: int = 0

    def summary_ja(self) -> str:
        if self.stitch_count <= 0:
            return "縫い辺 0 本（再構築できず）"
        self_part = (
            f"、自己縫い {self.self_stitch_count}"
            if self.self_stitch_count > 0
            else ""
        )
        return (
            f"縫い辺 {self.stitch_count} 本を再構築"
            f"（追加 {self.added_edges}、パネル {self.component_count}、"
            f"組 {self.component_pairs_used}{self_part}、"
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


def _boundary_adjacency(
    mesh: bpy.types.Mesh, boundary: set[int]
) -> dict[int, set[int]]:
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
    return boundary_adj


def _boundary_loops(
    boundary_adj: dict[int, set[int]], verts: set[int]
) -> list[list[int]]:
    """成分内の境界閉ループ（および開経路）を列挙する。"""
    local: dict[int, list[int]] = defaultdict(list)
    for v in verts:
        if v not in boundary_adj:
            continue
        for nb in boundary_adj[v]:
            if nb in verts:
                local[v].append(nb)

    visited_edges: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    for start in local:
        for nb0 in local[start]:
            e0 = tuple(sorted((start, nb0)))
            if e0 in visited_edges:
                continue
            path = [start]
            prev: int | None = None
            cur = start
            while True:
                opts = [
                    x
                    for x in local[cur]
                    if x != prev and tuple(sorted((cur, x))) not in visited_edges
                ]
                if not opts:
                    break
                nxt = opts[0]
                visited_edges.add(tuple(sorted((cur, nxt))))
                path.append(nxt)
                prev, cur = cur, nxt
                if cur == start and len(path) > 2:
                    break
            if len(path) >= 3:
                if path[0] == path[-1]:
                    path = path[:-1]
                if len(path) >= 3:
                    loops.append(path)
    return loops


def _proximity_pairs_on_loop(
    loop: list[int],
    world: np.ndarray,
    *,
    max_dist_m: float,
    min_hops: int,
) -> list[tuple[int, int, float]]:
    """ループ上で空間的に近い非隣接頂点を対応付ける（筒閉じ）。

    着装済みメッシュでは縫い代同士が同一座標に重なることが多く、距離 0 も有効。
    """
    n = len(loop)
    if n < 4:
        return []
    hop_min = max(3, min(min_hops, n // 4))
    w = world[np.asarray(loop, dtype=np.int32)]
    candidates: list[tuple[float, int, int]] = []
    for i in range(n):
        d = np.linalg.norm(w - w[i], axis=1)
        for j in range(i + 1, n):
            hops = min(j - i, n - (j - i))
            if hops < hop_min:
                continue
            dist = float(d[j])
            if dist > max_dist_m:
                continue
            candidates.append((dist, i, j))
    candidates.sort(key=lambda item: item[0])
    used: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for dist, i, j in candidates:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        pairs.append((int(loop[i]), int(loop[j]), dist))
    return pairs


def _mirror_pairs_on_loop(
    loop: list[int], world: np.ndarray
) -> list[tuple[int, int, float]]:
    """内部境界ループ（ダーツ穴）を折り線まわりで鏡映対応する。

    頂点 apex を折り点とみなし (apex+k) と (apex-k) を縫う。平均距離が最小の
    apex を選ぶ（着装後に両脚が重なっていれば距離はほぼ 0）。
    """
    n = len(loop)
    if n < 6:
        return []
    w = world[np.asarray(loop, dtype=np.int32)]
    best: tuple[float, list[tuple[int, int, float]]] | None = None
    for apex in range(n):
        pairs: list[tuple[int, int, float]] = []
        dists: list[float] = []
        for k in range(1, n // 2):
            i = (apex + k) % n
            j = (apex - k) % n
            if i == j:
                continue
            dist = float(np.linalg.norm(w[i] - w[j]))
            pairs.append((int(loop[i]), int(loop[j]), dist))
            dists.append(dist)
        if not dists:
            continue
        # 平均 + 外れ値ペナルティで、開いた誤対応（外周の半分折り）を避ける
        score = float(np.mean(dists)) + 0.25 * float(np.max(dists))
        if best is None or score < best[0]:
            best = (score, pairs)
    if best is None:
        return []
    # 内部ループ専用のため、開いたダーツ（脚間が広い）も縫い対象として残す。
    return best[1]


def _self_pairs_on_component(
    verts: np.ndarray,
    world: np.ndarray,
    mesh: bpy.types.Mesh,
    boundary: set[int],
    *,
    max_dist_m: float,
) -> list[tuple[int, int, float]]:
    """同一成分内の自己縫い（袖の筒閉じ・ダーツ閉じ）。

    - 外周ループ: 空間近傍の非隣接境界対（着装で重なった袖下など）
    - 内部ループ: 鏡映対応（ダーツ穴の両脚）
    """
    vert_set = {int(v) for v in verts if int(v) in boundary}
    if len(vert_set) < 4:
        return []

    boundary_adj = _boundary_adjacency(mesh, boundary)
    # 成分内に閉じた隣接だけ残す
    local_adj: dict[int, set[int]] = {
        v: {nb for nb in boundary_adj.get(v, ()) if nb in vert_set} for v in vert_set
    }
    loops = _boundary_loops(local_adj, vert_set)
    if not loops:
        return []

    # 最長ループを外周、残りで十分短いものを内部（ダーツ）とみなす
    loops_by_len = sorted(loops, key=len, reverse=True)
    outer = loops_by_len[0]
    internals = [
        lp
        for lp in loops_by_len[1:]
        if 6 <= len(lp) <= _SELF_INTERNAL_LOOP_MAX_VERTS and len(lp) < len(outer)
    ]

    all_pairs: list[tuple[int, int, float]] = []

    for loop in internals:
        all_pairs.extend(_mirror_pairs_on_loop(loop, world))

    for loop in loops:
        all_pairs.extend(
            _proximity_pairs_on_loop(
                loop,
                world,
                max_dist_m=max_dist_m,
                min_hops=_SELF_MIN_BOUNDARY_HOPS,
            )
        )

    # 同一頂点対は最短距離を採用し、頂点は 1 回まで
    best: dict[tuple[int, int], float] = {}
    for a, b, dist in all_pairs:
        if a == b:
            continue
        key = tuple(sorted((int(a), int(b))))
        prev = best.get(key)
        if prev is None or dist < prev:
            best[key] = dist

    ordered = sorted(best.items(), key=lambda item: item[1])
    used: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for (a, b), dist in ordered:
        if a in used or b in used:
            continue
        used.add(a)
        used.add(b)
        pairs.append((a, b, dist))
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
        return StitchRebuildReport(0, 0, 0, 0, 0.0, 0.0, "none", 0)

    mesh = obj.data
    world = _world_vertices(obj)
    comps = _connected_components(mesh)
    boundary = _boundary_vertices(mesh)

    cross_pairs: list[tuple[int, int, float]] = []
    self_pairs: list[tuple[int, int, float]] = []
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
                cross_pairs.extend(pairs)

    if include_self_seams and len(comps) >= 1:
        for comp in comps:
            self_pairs.extend(
                _self_pairs_on_component(
                    comp, world, mesh, boundary, max_dist_m=_SELF_MAX_DIST_M
                )
            )

    # 一意化（クロス優先のあと自己。同一キーは短い方）
    best: dict[tuple[int, int], float] = {}
    self_keys: set[tuple[int, int]] = set()
    for a, b, dist in cross_pairs:
        key = tuple(sorted((int(a), int(b))))
        if key[0] == key[1]:
            continue
        prev = best.get(key)
        if prev is None or dist < prev:
            best[key] = dist
    for a, b, dist in self_pairs:
        key = tuple(sorted((int(a), int(b))))
        if key[0] == key[1]:
            continue
        prev = best.get(key)
        if prev is None or dist < prev:
            best[key] = dist
        self_keys.add(key)

    ordered = sorted(best.items(), key=lambda item: item[1])
    edge_list = [key for key, _ in ordered]
    gaps = [dist for _, dist in ordered]
    self_stitch_count = sum(1 for key in edge_list if key in self_keys)

    added, stitch_keys = _add_loose_edges(obj, edge_list)
    found = _tag_stitch_edges(obj, stitch_keys)

    max_gap = float(max(gaps)) if gaps else 0.0
    mean_gap = float(sum(gaps) / len(gaps)) if gaps else 0.0
    method = "境界最近傍+自己縫い" if self_stitch_count else "境界最近傍"
    return StitchRebuildReport(
        stitch_count=int(found),
        added_edges=int(added),
        component_count=len(comps),
        component_pairs_used=int(component_pairs_used),
        max_gap_m=max_gap,
        mean_gap_m=mean_gap,
        method=method,
        self_stitch_count=int(self_stitch_count),
    )

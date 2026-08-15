# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert a Marvelous Designer OBJ plus Pattern JSON to the HOU contract.

The source object is never modified.  Each face-connected panel is copied to
its own world-space mesh, and the MD seam descriptions are resolved to exact
local vertex pairs in ``housei_sewing_plan_json``.  Koromo and other
downstream tools can therefore consume the result without importing any
Ominaeshi or Housei Python module.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import io
import json
import math
import os
import re

import bpy
import numpy as np
from mathutils import Vector

from . import md_bridge


HOU_SCHEMA = "housei-hou/1.0.0"
PLAN_SCHEMA = "housei-sewing-plan/1.0.0"
PLAN_PROPERTY = "housei_sewing_plan_json"
_OWNER_ROLE = "ominaeshi_hou"
_CUT_SCHEME = 100  # MD OBJ topology; intentionally distinct from Housei cuts.


class HouExportError(RuntimeError):
    """A user-correctable HOU conversion failure."""


@dataclass(frozen=True)
class HouExportResult:
    collection: bpy.types.Collection
    part_count: int
    seam_label_count: int
    pair_count: int


def _npy_to_b64(array: np.ndarray) -> str:
    buffer = io.BytesIO()
    np.save(buffer, np.ascontiguousarray(array))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _unit_scale_m(unit: object) -> float:
    key = str(unit or "mm").strip().lower().replace(" ", "")
    return {
        "mm": 0.001,
        "millimeter": 0.001,
        "millimeters": 0.001,
        "cm": 0.01,
        "centimeter": 0.01,
        "centimeters": 0.01,
        "m": 1.0,
        "meter": 1.0,
        "meters": 1.0,
        "in": 0.0254,
        "inch": 0.0254,
        "inches": 0.0254,
    }.get(key, 0.001)


def _load_pattern_json(source: bpy.types.Object) -> tuple[dict, str]:
    path = str(source.get("tanabata_pattern_json_path", "") or "")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = stream.read()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data, raw
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HouExportError(f"Pattern JSON を読めません: {exc}") from exc

    text_name = str(source.get("ominaeshi_pattern_text", "") or "")
    text = bpy.data.texts.get(text_name) if text_name else None
    if text is not None:
        raw = text.as_string()
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HouExportError(f"保存済み Pattern JSON が壊れています: {exc}") from exc
        if isinstance(data, dict):
            return data, raw

    raise HouExportError(
        "Pattern JSON がありません。MD の 2_send_clothes_BL で OBJ を取り込み直してください。"
    )


def _vertex_uvs(mesh: bpy.types.Mesh) -> list[tuple[float, float]]:
    layer = mesh.uv_layers.active
    if layer is None:
        raise HouExportError("服 OBJ に UVMap がありません。MD の OBJ 書き出し設定を確認してください。")
    values: list[list[tuple[float, float]]] = [[] for _ in mesh.vertices]
    for loop in mesh.loops:
        uv = layer.data[loop.index].uv
        values[loop.vertex_index].append((float(uv.x), float(uv.y)))
    averaged = []
    for items in values:
        if not items:
            averaged.append((0.0, 0.0))
        else:
            averaged.append(
                (
                    sum(item[0] for item in items) / len(items),
                    sum(item[1] for item in items) / len(items),
                )
            )
    return averaged


def _face_components(source: bpy.types.Object) -> list[dict]:
    """Return face-connected components, deliberately ignoring loose edges."""
    mesh = source.data
    if not mesh.polygons:
        raise HouExportError("服 OBJ に面がありません。")
    avg_uv = _vertex_uvs(mesh)

    vertex_polys: list[list[int]] = [[] for _ in mesh.vertices]
    edge_face_count: dict[tuple[int, int], int] = {}
    for poly in mesh.polygons:
        verts = [int(index) for index in poly.vertices]
        for vertex in verts:
            vertex_polys[vertex].append(poly.index)
        for a, b in zip(verts, verts[1:] + verts[:1]):
            key = tuple(sorted((a, b)))
            edge_face_count[key] = edge_face_count.get(key, 0) + 1

    seen: set[int] = set()
    components: list[dict] = []
    for first in range(len(mesh.polygons)):
        if first in seen:
            continue
        stack = [first]
        seen.add(first)
        polygon_indices: list[int] = []
        vertices: set[int] = set()
        while stack:
            polygon_index = stack.pop()
            polygon_indices.append(polygon_index)
            poly = mesh.polygons[polygon_index]
            for vertex in poly.vertices:
                vertex = int(vertex)
                vertices.add(vertex)
                for neighbor in vertex_polys[vertex]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)

        boundary_vertices: set[int] = set()
        boundary_edges: set[tuple[int, int]] = set()
        component_edges: set[tuple[int, int]] = set()
        for polygon_index in polygon_indices:
            verts = [int(index) for index in mesh.polygons[polygon_index].vertices]
            for a, b in zip(verts, verts[1:] + verts[:1]):
                key = tuple(sorted((a, b)))
                component_edges.add(key)
                if edge_face_count.get(key) == 1:
                    boundary_edges.add(key)
                    boundary_vertices.update(key)

        uv_points = [avg_uv[index] for index in vertices]
        world_points = [source.matrix_world @ mesh.vertices[index].co for index in vertices]
        components.append(
            {
                "index": len(components),
                "verts": vertices,
                "boundary_verts": boundary_vertices,
                "boundary_edges": boundary_edges,
                "edges": component_edges,
                "polygons": sorted(polygon_indices),
                "uv": avg_uv,
                "uv_bbox": md_bridge._bbox_2d(uv_points),
                "world_center": sum((point for point in world_points), Vector())
                / max(1, len(world_points)),
            }
        )
    return components


def _safe_label(group_index: int, pair_index: int) -> str:
    return f"MD_G{group_index:04d}_P{pair_index:04d}"


def _resolve_seams(source, pattern, components, shapes, _piece_list, mapping):
    requested: set[tuple[int, int]] = set()
    failures: list[str] = []
    for group_index, group in enumerate(pattern.get("SeamLinePairGroupList", [])):
        for pair_index, pair in enumerate(group.get("PairList", [])):
            sides = []
            for side_name in ("First", "Second"):
                raw = pair.get(side_name) or {}
                shape = shapes.get(str(raw.get("ShapeID") or ""))
                component_index = (
                    mapping.get(shape["piece_id"]) if shape is not None else None
                )
                sides.append(component_index)
            if sides[0] is None or sides[1] is None:
                failures.append(
                    f"{_safe_label(group_index, pair_index)}: 型紙パーツとの対応を解決できません"
                )
                continue
            requested.add(tuple(sorted((int(sides[0]), int(sides[1])))))

    if failures:
        preview = " / ".join(failures[:4])
        suffix = f"（ほか {len(failures) - 4} 件）" if len(failures) > 4 else ""
        raise HouExportError(f"縫い線をHOU化できません: {preview}{suffix}")

    # A simulated MD OBJ represents a stitch as two distinct boundary vertices
    # at the same 3D position.  This is more authoritative than re-approximating
    # MD's curve parameterisation from Pattern JSON.  Pattern JSON is still used
    # to restrict matching to panel pairs that are actually sewn.
    tolerance = 1.0e-5  # 0.01 mm in Blender/HOU metres.
    world = [source.matrix_world @ vertex.co for vertex in source.data.vertices]

    def coincident_pairs(component_a: int, component_b: int):
        vertices_a = sorted(components[component_a]["boundary_verts"])
        vertices_b = sorted(components[component_b]["boundary_verts"])
        grid: dict[tuple[int, int, int], list[int]] = {}
        for vertex in vertices_b:
            point = world[vertex]
            cell = tuple(math.floor(float(point[axis]) / tolerance) for axis in range(3))
            grid.setdefault(cell, []).append(vertex)
        candidates = []
        for vertex_a in vertices_a:
            point = world[vertex_a]
            cell = tuple(math.floor(float(point[axis]) / tolerance) for axis in range(3))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for vertex_b in grid.get(
                            (cell[0] + dx, cell[1] + dy, cell[2] + dz), ()
                        ):
                            if component_a == component_b and vertex_b <= vertex_a:
                                continue
                            distance = (point - world[vertex_b]).length
                            if distance <= tolerance:
                                candidates.append((distance, vertex_a, vertex_b))
        candidates.sort()
        used_a: set[int] = set()
        used_b: set[int] = set()
        used_same: set[int] = set()
        result = []
        for _distance, vertex_a, vertex_b in candidates:
            if component_a == component_b:
                if vertex_a in used_same or vertex_b in used_same:
                    continue
                used_same.update((vertex_a, vertex_b))
            else:
                if vertex_a in used_a or vertex_b in used_b:
                    continue
                used_a.add(vertex_a)
                used_b.add(vertex_b)
            result.append((component_a, vertex_a, component_b, vertex_b))
        return result

    resolved: dict[str, list[tuple[int, int, int, int]]] = {}
    marked: dict[int, dict[str, set[tuple[int, int]]]] = {
        index: {} for index in range(len(components))
    }
    for component_a, component_b in sorted(requested):
        label = f"MD_SEWN_C{component_a:04d}_C{component_b:04d}"
        pairs = coincident_pairs(component_a, component_b)
        if not pairs:
            failures.append(
                f"{label}: 一致する縫製済み境界がありません（MDで縫製シミュレーション後に送信してください）"
            )
            continue
        resolved[label] = pairs
        selected_by_component: dict[int, set[int]] = {}
        for slot_a, vertex_a, slot_b, vertex_b in pairs:
            selected_by_component.setdefault(slot_a, set()).add(vertex_a)
            selected_by_component.setdefault(slot_b, set()).add(vertex_b)
        for component_index, selected in selected_by_component.items():
            edges = {
                edge
                for edge in components[component_index]["edges"]
                if edge[0] in selected and edge[1] in selected
            }
            if edges:
                marked[component_index].setdefault(label, set()).update(edges)

    if failures:
        preview = " / ".join(failures[:4])
        suffix = f"（ほか {len(failures) - 4} 件）" if len(failures) > 4 else ""
        raise HouExportError(f"縫い線をHOU化できません: {preview}{suffix}")
    if pattern.get("SeamLinePairGroupList") and not resolved:
        raise HouExportError("Pattern JSON に縫い線はありますが、頂点ペアを作れませんでした。")
    return resolved, marked


def _collection_name(source: bpy.types.Object) -> str:
    base = re.sub(r"[^0-9A-Za-z_\-\.]+", "_", source.name).strip("_") or "Garment"
    return f"{base}_HOU"


def _output_collection(scene: bpy.types.Scene, source: bpy.types.Object) -> bpy.types.Collection:
    expected = _collection_name(source)
    collection = bpy.data.collections.get(expected)
    if collection is not None and (
        collection.get("ominaeshi_role") != _OWNER_ROLE
        or collection.get("ominaeshi_source_object") != source.name
    ):
        collection = None
    if collection is None:
        collection = bpy.data.collections.new(expected)
        scene.collection.children.link(collection)
    else:
        # Only replace data positively identified as a previous Ominaeshi output.
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
    return collection


def _median_spacing(source: bpy.types.Object, component: dict) -> float:
    lengths = []
    for a, b in component["edges"]:
        pa = source.matrix_world @ source.data.vertices[a].co
        pb = source.matrix_world @ source.data.vertices[b].co
        length = (pa - pb).length
        if math.isfinite(length) and length > 1.0e-10:
            lengths.append(length)
    if not lengths:
        return 0.005
    lengths.sort()
    middle = len(lengths) // 2
    if len(lengths) % 2:
        return float(lengths[middle])
    return float((lengths[middle - 1] + lengths[middle]) * 0.5)


def _copy_component(
    source: bpy.types.Object,
    collection: bpy.types.Collection,
    component: dict,
    *,
    panel_id: str,
    panel_label: str,
    panel_index: int,
    pattern_piece: dict | None,
    unit_scale: float,
    marked_edges: dict[str, set[tuple[int, int]]],
) -> tuple[bpy.types.Object, dict[int, int]]:
    source_mesh = source.data
    global_vertices = sorted(component["verts"])
    local_by_global = {global_index: local for local, global_index in enumerate(global_vertices)}
    vertices = [tuple(source.matrix_world @ source_mesh.vertices[index].co) for index in global_vertices]

    flipped = source.matrix_world.to_3x3().determinant() < 0.0
    faces: list[list[int]] = []
    face_uvs: list[list[tuple[float, float]]] = []
    material_indices: list[int] = []
    source_uv = source_mesh.uv_layers.active
    for polygon_index in component["polygons"]:
        polygon = source_mesh.polygons[polygon_index]
        global_face = [int(index) for index in polygon.vertices]
        uvs = []
        if source_uv is not None:
            for loop_index in polygon.loop_indices:
                uv = source_uv.data[loop_index].uv
                uvs.append((float(uv.x), float(uv.y)))
        if flipped:
            global_face.reverse()
            uvs.reverse()
        faces.append([local_by_global[index] for index in global_face])
        face_uvs.append(uvs)
        material_indices.append(int(polygon.material_index))

    mesh = bpy.data.meshes.new(f"{panel_label}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    for material in source_mesh.materials:
        mesh.materials.append(material)
    for polygon, material_index in zip(mesh.polygons, material_indices):
        polygon.material_index = material_index
    if source_uv is not None:
        uv_layer = mesh.uv_layers.new(name=source_uv.name or "UVMap")
        for polygon, uvs in zip(mesh.polygons, face_uvs):
            for loop_index, uv in zip(polygon.loop_indices, uvs):
                uv_layer.data[loop_index].uv = uv

    if pattern_piece is not None:
        scale_x, scale_y, dx, dy = md_bridge._pattern_to_uv_transform(
            pattern_piece, component
        )
        pattern = np.asarray(
            [
                (
                    ((component["uv"][index][0] - dx) / scale_x) * unit_scale,
                    ((component["uv"][index][1] - dy) / scale_y) * unit_scale,
                    0.0,
                )
                for index in global_vertices
            ],
            dtype=np.float64,
        )
    else:
        pattern = np.asarray(
            [
                (
                    component["uv"][index][0] * unit_scale,
                    component["uv"][index][1] * unit_scale,
                    0.0,
                )
                for index in global_vertices
            ],
            dtype=np.float64,
        )
    for attribute_name in ("housei_pattern_position", "housei_construction_position"):
        attribute = mesh.attributes.new(attribute_name, "FLOAT_VECTOR", "POINT")
        attribute.data.foreach_set("vector", pattern.astype(np.float32, copy=False).ravel())

    for label, source_edges in marked_edges.items():
        wanted = {
            tuple(sorted((local_by_global[a], local_by_global[b])))
            for a, b in source_edges
            if a in local_by_global and b in local_by_global
        }
        if not wanted:
            continue
        attribute = mesh.attributes.new(f"sewing_{label}", "BOOLEAN", "EDGE")
        values = [tuple(sorted(map(int, edge.vertices))) in wanted for edge in mesh.edges]
        attribute.data.foreach_set("value", values)

    obj = bpy.data.objects.new(panel_label, mesh)
    collection.objects.link(obj)
    spacing = _median_spacing(source, component)
    obj["housei_role"] = "part"
    obj["housei_panel_id"] = panel_id
    obj["housei_panel_label"] = panel_label
    obj["housei_panel_instance"] = panel_label
    obj["housei_panel_index"] = panel_index
    obj["housei_mirror_side"] = ""
    obj["housei_ring_closed"] = False
    obj["housei_mesh_spacing_m"] = spacing
    obj["housei_cut_scheme"] = _CUT_SCHEME
    obj["housei_source_svg"] = ""
    obj["housei_collection"] = collection.name
    sewing_labels = sorted(marked_edges)
    hou = {
        "schema": HOU_SCHEMA,
        "role": "part",
        "panel_id": panel_id,
        "panel_label": panel_label,
        "panel_instance": panel_label,
        "panel_index": panel_index,
        "mirror_side": "",
        "ring_closed": False,
        "mesh_spacing_m": spacing,
        "cut_scheme": _CUT_SCHEME,
        "source_svg": "",
        "collection": collection.name,
        "sewing_labels": sewing_labels,
        "pattern_position_npy_b64": _npy_to_b64(pattern),
        "construction_position_npy_b64": _npy_to_b64(pattern),
        "source_application": "Marvelous Designer",
        "source_object": source.name,
        "writer": "Ominaeshi",
    }
    obj["HOU"] = json.dumps(hou, ensure_ascii=False, separators=(",", ":"))
    return obj, local_by_global


def create_hou_collection(context, source: bpy.types.Object | None) -> HouExportResult:
    if source is None or source.type != "MESH":
        raise HouExportError("HOU化する服メッシュを指定してください。")
    pattern, raw_pattern = _load_pattern_json(source)
    shapes, pieces = md_bridge._pattern_shape_maps(pattern)
    components = _face_components(source)
    if not shapes or not pieces:
        raise HouExportError("Pattern JSON に有効な型紙パーツがありません。")
    mapping = md_bridge._assign_pieces_to_components(pieces, components)
    missing = [piece["piece_name"] for piece in pieces if piece["piece_id"] not in mapping]
    if missing:
        raise HouExportError(
            "OBJと対応しない型紙パーツがあります: " + ", ".join(missing[:8])
        )

    seams, marked = _resolve_seams(source, pattern, components, shapes, pieces, mapping)
    collection = _output_collection(context.scene, source)
    unit_scale = _unit_scale_m(pattern.get("Unit"))
    piece_by_component = {component: piece for piece, component in mapping.items()}
    piece_by_id = {piece["piece_id"]: piece for piece in pieces}
    piece_names = {piece["piece_id"]: piece["piece_name"] for piece in pieces}

    objects: list[bpy.types.Object] = []
    local_maps: list[dict[int, int]] = []
    for component_index, component in enumerate(components):
        panel_id = piece_by_component.get(component_index, f"component_{component_index:04d}")
        piece_name = piece_names.get(panel_id, panel_id)
        label_base = re.sub(r"[^0-9A-Za-z_\-]+", "_", piece_name).strip("_")
        panel_label = f"HOU_{component_index:04d}_{label_base or 'part'}"
        obj, local_map = _copy_component(
            source,
            collection,
            component,
            panel_id=panel_id,
            panel_label=panel_label,
            panel_index=component_index,
            pattern_piece=piece_by_id.get(panel_id),
            unit_scale=unit_scale,
            marked_edges=marked.get(component_index, {}),
        )
        objects.append(obj)
        local_maps.append(local_map)

    plan_pairs: dict[str, list[list[int]]] = {}
    for label, pairs in seams.items():
        converted = []
        for slot_a, global_a, slot_b, global_b in pairs:
            try:
                converted.append(
                    [slot_a, local_maps[slot_a][global_a], slot_b, local_maps[slot_b][global_b]]
                )
            except (IndexError, KeyError) as exc:
                raise HouExportError(f"{label} の頂点番号をHOUパーツへ変換できません。") from exc
        if converted:
            plan_pairs[label] = converted

    parts = []
    for index, obj in enumerate(objects):
        parts.append(
            {
                "object": obj.name,
                "instance": str(obj.get("housei_panel_instance", "")),
                "panel_id": str(obj.get("housei_panel_id", obj.name)),
                "panel_index": index,
                "vertices": len(obj.data.vertices),
                "cut_scheme": int(obj["housei_cut_scheme"]),
                "mesh_spacing_m": float(obj["housei_mesh_spacing_m"]),
            }
        )
    pair_count = sum(len(items) for items in plan_pairs.values())
    plan = {
        "schema": PLAN_SCHEMA,
        "collection": collection.name,
        "labels": list(plan_pairs),
        "parts": parts,
        "pairs": plan_pairs,
        "pair_count": pair_count,
        "writer": "Ominaeshi",
        "source": "Marvelous Designer dressed OBJ",
    }
    collection["housei_role"] = "clothes"
    collection["housei_sewing_verified"] = True
    collection["ominaeshi_role"] = _OWNER_ROLE
    collection["ominaeshi_source_object"] = source.name
    collection["ominaeshi_source_pattern_text"] = str(
        source.get("ominaeshi_pattern_text", "") or ""
    )
    collection[PLAN_PROPERTY] = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))

    # Preserve the authoring data in the .blend without pretending it is a
    # Housei pattern document (the schemas are intentionally different).
    text_name = f"Ominaeshi_HOU_Pattern_{source.name}"
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    text.clear()
    text.write(raw_pattern)
    collection["ominaeshi_md_pattern_text"] = text.name
    return HouExportResult(collection, len(objects), len(plan_pairs), pair_count)

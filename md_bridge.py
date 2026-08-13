# SPDX-License-Identifier: GPL-3.0-or-later
"""MD ブリッジ（tanabata 互換・オブジェクト転送 + 静的ボディ ABC）。

MD 側は同梱 md_addon を使う:
  * 1_get_BL_avater  -> export_body_abc (single_frame=True のみ)
  * 2_send_clothes_BL -> import_garment_obj

使わない: 全フレーム ABC、服シミュレーション ABC、ヘアー ABC、storage 設定 UI。
一時ファイルは常に %TEMP%\\tanabata（MD と同じ固定パス）。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import tempfile
import threading
import time
import traceback
import bmesh
import bpy
from mathutils import Vector

HOST = "127.0.0.1"
PORT = 7422
# MD 側 md_addon と同じ固定 temp。storage フォルダは使わない。
TEMP_DIR = os.path.join(tempfile.gettempdir(), "tanabata")
CONFIG_PATH = os.path.join(TEMP_DIR, "config.json")
AVATAR_ONE_ABC = os.path.join(TEMP_DIR, "avatar_one.abc")
ABC_GLOBAL_SCALE = 1.0
_LOG_PATH = os.path.join(os.path.expanduser("~"), "ominaeshi_md_bridge.log")

_server: "_BridgeServer | None" = None
_last_garment_objs: list[str] = []


def _log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _write_config() -> None:
    """MD が読む config。storage_dir は常に空（temp 固定）。"""
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        data = {
            "storage_dir": "",
            "paths": {
                "avatar_one_abc": AVATAR_ONE_ABC.replace("\\", "/"),
                "avatar_all_abc": os.path.join(TEMP_DIR, "avatar_all.abc").replace(
                    "\\", "/"
                ),
                "garment_obj": os.path.join(TEMP_DIR, "garment.obj").replace("\\", "/"),
                "garment_abc": os.path.join(TEMP_DIR, "garment.abc").replace("\\", "/"),
            },
            "bridge": "ominaeshi",
            "version": "0.2.2",
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        _log(f"could not write config {CONFIG_PATH}: {exc}")


def _body_object() -> bpy.types.Object | None:
    """パネルのボディ。タイマー中の context.scene 違いを避けるため全シーンを見る。"""
    scenes = []
    ctx_scene = getattr(bpy.context, "scene", None)
    if ctx_scene is not None:
        scenes.append(ctx_scene)
    scenes.extend(s for s in bpy.data.scenes if s not in scenes)
    for scene in scenes:
        props = getattr(scene, "ominaeshi", None)
        if props is None:
            continue
        obj = getattr(props, "body_object", None)
        if obj is not None and obj.name in bpy.data.objects:
            return obj
    return None


def is_listening() -> bool:
    return _server is not None and _server.thread is not None and _server.thread.is_alive()


def start_listener() -> str:
    global _server
    if is_listening():
        return f"MD ブリッジは既に :{PORT} で待ち受け中"
    os.makedirs(TEMP_DIR, exist_ok=True)
    _write_config()
    server = _BridgeServer()
    try:
        server.start()
    except OSError as exc:
        raise RuntimeError(
            f"ポート {PORT} を開けません（他のリスナーが動いている可能性）: {exc}"
        ) from exc
    _server = server
    return f"MD ブリッジを :{PORT} で開始（temp={TEMP_DIR}）"


def stop_listener() -> str:
    global _server
    if _server is None:
        return "MD ブリッジは停止しています"
    _server.shutdown()
    _server = None
    return "MD ブリッジを停止しました"


def listener_status_ja() -> str:
    if is_listening():
        return f"待ち受け中 :{PORT}"
    return "停止中"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _h_ping(_params):
    return {
        "app": "blender",
        "bridge": "ominaeshi",
        "version": bpy.app.version_string,
    }


def _h_export_body_abc(params):
    """静的ボディ（frame 1）だけ ABC 出力。全フレーム要求は拒否。"""
    single_frame = bool((params or {}).get("single_frame", False))
    if not single_frame:
        raise RuntimeError(
            "女郎花は frame-1 のボディ送信のみ対応です。"
            "MD では 1_get_BL_avater を使ってください（3_get_BL_animation は未対応）。"
        )

    obj = _body_object()
    if obj is None:
        raise RuntimeError(
            "ボディが未設定です。女郎花パネルの「ボディ」にアバターメッシュを指定してください。"
        )
    if obj.type != "MESH":
        raise RuntimeError(f"ボディ '{obj.name}' はメッシュではありません")

    scene = bpy.context.scene
    start = end = scene.frame_start
    fps = scene.render.fps / scene.render.fps_base

    os.makedirs(TEMP_DIR, exist_ok=True)
    _write_config()
    abc_path = AVATAR_ONE_ABC
    sentinel = abc_path + ".done.json"
    for p in (abc_path, sentinel):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    task = {
        "object": obj.name,
        "abc_path": abc_path,
        "sentinel": sentinel,
        "start": start,
        "end": end,
        "fps": fps,
        "unit_scale": scene.unit_settings.scale_length,
    }
    if _server is not None:
        _server.pending.append(task)
    else:
        _run_export_task(task)

    return {
        "accepted": True,
        "abc_path": abc_path.replace("\\", "/"),
        "sentinel": sentinel.replace("\\", "/"),
        "object": obj.name,
        "fps": fps,
        "frame_start": start,
        "frame_end": end,
        "unit_scale": scene.unit_settings.scale_length,
    }


def _run_export_task(task: dict) -> None:
    abc_path = task["abc_path"]
    sentinel = task["sentinel"]
    vl = bpy.context.view_layer
    saved_sel = list(bpy.context.selected_objects)
    saved_active = vl.objects.active
    result: dict = {"ok": False, "error": "unknown"}
    try:
        obj = bpy.data.objects.get(task["object"])
        if obj is None:
            raise RuntimeError(f"object '{task['object']}' is no longer in the scene")
        for o in vl.objects:
            o.select_set(False)
        obj.select_set(True)
        vl.objects.active = obj

        bpy.ops.wm.alembic_export(
            filepath=abc_path,
            start=task["start"],
            end=task["end"],
            xsamples=1,
            gsamples=1,
            sh_open=0.0,
            sh_close=1.0,
            selected=True,
            flatten=False,
            uvs=True,
            packuv=True,
            normals=True,
            vcolors=False,
            orcos=False,
            face_sets=False,
            subdiv_schema=False,
            apply_subdiv=False,
            curves_as_mesh=False,
            use_instancing=True,
            global_scale=ABC_GLOBAL_SCALE,
            triangulate=False,
            export_hair=False,
            export_particles=False,
            export_custom_properties=False,
            as_background_job=False,
            evaluation_mode="RENDER",
            init_scene_frame_range=False,
        )
        result = {
            "ok": True,
            "abc_path": abc_path.replace("\\", "/"),
            "object": task["object"],
            "fps": task["fps"],
            "frame_start": task["start"],
            "frame_end": task["end"],
            "unit_scale": task["unit_scale"],
        }
        _log(f"export done: {abc_path} (frames {task['start']}..{task['end']})")
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
        _log("export task failed:\n" + traceback.format_exc())
    finally:
        try:
            for o in vl.objects:
                o.select_set(False)
            for o in saved_sel:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            vl.objects.active = saved_active
        except Exception:
            pass
    try:
        with open(sentinel, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except OSError as exc:
        _log(f"could not write sentinel {sentinel}: {exc}")


def _h_import_garment_obj(params):
    global _last_garment_objs
    obj_path = params.get("obj_path")
    if not obj_path or not os.path.exists(obj_path):
        raise RuntimeError(f"garment OBJ not found: {obj_path}")
    garment_info_path = params.get("garment_info_path") or ""
    pattern_json_path = params.get("pattern_json_path") or ""

    try:
        scn = bpy.context.scene
        scn.frame_set(scn.frame_start)
    except Exception:  # noqa: BLE001
        pass

    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=obj_path)
    new_objs = [o for o in bpy.data.objects if o not in before]
    mesh_objs = [o for o in new_objs if o.type == "MESH"]
    _last_garment_objs = [o.name for o in mesh_objs]

    garment_info_ok = bool(garment_info_path and os.path.exists(garment_info_path))
    pattern_json_ok = bool(pattern_json_path and os.path.exists(pattern_json_path))
    for o in mesh_objs:
        o["tanabata_garment_obj_path"] = obj_path
        o["tanabata_garment_info_path"] = garment_info_path if garment_info_ok else ""
        o["tanabata_pattern_json_path"] = pattern_json_path if pattern_json_ok else ""
        o["tanabata_pattern_json_ready"] = pattern_json_ok
        o["ominaeshi_from_md"] = True
        try:
            _store_ageha_cloth_seams(o, pattern_json_path if pattern_json_ok else "")
        except Exception as exc:  # noqa: BLE001
            o["ageha_cloth_ready"] = False
            _log(f"could not store Ageha cloth seam metadata on {o.name}: {exc}")
        try:
            _store_blender_cloth_sewing_data(
                o, pattern_json_path if pattern_json_ok else ""
            )
        except Exception as exc:  # noqa: BLE001
            o["tanabata_sewing_ready"] = False
            o["tanabata_sewing_status"] = f"failed: {exc}"
            _log(f"could not store sewing data on {o.name}: {exc}")

    # 女郎花の服入力を自動セット（最大メッシュ）
    if mesh_objs:
        primary = max(mesh_objs, key=lambda o: len(o.data.polygons))
        props = getattr(bpy.context.scene, "ominaeshi", None)
        if props is not None:
            props.clothes_object = primary
            props.parse_status = (
                f"MD から服を取り込み: {primary.name}"
                f"（メッシュ {len(mesh_objs)}）"
            )

    packed = False
    try:
        bpy.ops.file.pack_all()
        packed = True
    except Exception as exc:  # noqa: BLE001
        _log(f"pack_all FAILED: {exc}")

    freed = 0
    if packed:
        freed += _delete_packed_temp_images()
        for p in (obj_path, os.path.splitext(obj_path)[0] + ".mtl"):
            try:
                if os.path.exists(p):
                    os.remove(p)
                    freed += 1
            except OSError:
                pass

    mats = {
        o.name: [ms.material.name if ms.material else None for ms in o.material_slots]
        for o in mesh_objs
    }
    ageha = {
        o.name: {
            "ready": bool(o.get("ageha_cloth_ready")),
            "text": o.get("ageha_cloth_seams_text", ""),
            "seam_groups": int(o.get("ageha_cloth_seam_group_count", 0)),
        }
        for o in mesh_objs
    }
    sewing = {
        o.name: {
            "ready": bool(o.get("tanabata_sewing_ready")),
            "edges": int(o.get("tanabata_sewing_edge_count", 0)),
            "modifier": o.get("tanabata_sewing_modifier", ""),
            "text": o.get("tanabata_sewing_edges_text", ""),
            "status": o.get("tanabata_sewing_status", ""),
        }
        for o in mesh_objs
    }
    _log(f"garment imported {_last_garment_objs}; materials={mats}; packed={packed}")
    return {
        "imported": [o.name for o in new_objs],
        "meshes": _last_garment_objs,
        "materials": mats,
        "ageha_cloth": ageha,
        "sewing": sewing,
        "garment_info_path": garment_info_path if garment_info_ok else "",
        "pattern_json_path": pattern_json_path if pattern_json_ok else "",
        "pattern_json_ready": pattern_json_ok,
        "packed": packed,
        "temp_freed": freed,
        "preserved": False,
        "bridge": "ominaeshi",
    }


HANDLERS = {
    "ping": _h_ping,
    "export_body_abc": _h_export_body_abc,
    "import_garment_obj": _h_import_garment_obj,
}


# --------------------------------------------------------------------------- #
# Ageha seams + optional sewing edges (from tanabata)
# --------------------------------------------------------------------------- #
def _line_length_2d(line):
    pts = [p.get("Position") for p in line.get("PointList", []) if p.get("Position")]
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        try:
            total += (
                (float(a["x"]) - float(b["x"])) ** 2
                + (float(a["y"]) - float(b["y"])) ** 2
            ) ** 0.5
        except Exception:  # noqa: BLE001
            pass
    return total


def _shape_length_2d(shape):
    return sum(_line_length_2d(line) for line in shape.get("LineList", []))


def _build_ageha_cloth_seams(pattern_json_path):
    if not pattern_json_path or not os.path.exists(pattern_json_path):
        return None
    with open(pattern_json_path, "r", encoding="utf-8") as f:
        src = json.load(f)

    shapes = {}
    pieces = []
    for p in src.get("PatternList", []):
        pid = str(p.get("ID") or "")
        if not pid:
            continue
        outer = p.get("ShapeInfo") or {}
        piece = {
            "id": pid,
            "name": str(p.get("Name") or pid),
            "fabric_id": str(p.get("CurrentFabricUUID") or ""),
            "closed": bool(p.get("IsClosed")),
            "outer_length": _shape_length_2d(outer),
            "internal_shapes": [],
        }
        shapes[pid] = {
            "id": pid,
            "piece_id": pid,
            "piece_name": piece["name"],
            "kind": "outer",
            "length": piece["outer_length"],
        }
        for internal in p.get("InternalLineList", []):
            sid = str(internal.get("ID") or "")
            if not sid:
                continue
            length = _shape_length_2d(internal)
            piece["internal_shapes"].append(
                {
                    "id": sid,
                    "kind": "internal",
                    "closed": bool(internal.get("IsClosed")),
                    "length": length,
                }
            )
            shapes[sid] = {
                "id": sid,
                "piece_id": pid,
                "piece_name": piece["name"],
                "kind": "internal",
                "length": length,
            }
        pieces.append(piece)

    def segment(ref):
        sid = str(ref.get("ShapeID") or "")
        length_param = ref.get("LengthParam") or {}
        start = float(length_param.get("fStart", 0.0))
        end = float(length_param.get("fEnd", 0.0))
        shape = shapes.get(
            sid,
            {
                "id": sid,
                "piece_id": "",
                "piece_name": "",
                "kind": "unknown",
                "length": 0.0,
            },
        )
        return {
            "shape_id": sid,
            "piece_id": shape["piece_id"],
            "piece_name": shape["piece_name"],
            "shape_kind": shape["kind"],
            "start": start,
            "end": end,
            "direction": bool(ref.get("Direction")),
            "length": abs(end - start) * float(shape.get("length", 0.0)),
        }

    seams = []
    for i, group in enumerate(src.get("SeamLinePairGroupList", [])):
        pairs = []
        for pair in group.get("PairList", []):
            a = segment(pair.get("First") or {})
            b = segment(pair.get("Second") or {})
            pairs.append(
                {
                    "a": a,
                    "b": b,
                    "length_delta": abs(float(a["length"]) - float(b["length"])),
                }
            )
        seams.append(
            {
                "index": i,
                "name": str(group.get("Name") or f"seam_{i}"),
                "turned": bool(group.get("bIsTurned")),
                "fold_angle": (group.get("FoldData") or {}).get("iAngle"),
                "fold_strength": (group.get("FoldData") or {}).get("iStrength"),
                "pairs": pairs,
            }
        )

    return {
        "schema": "ageha.cloth_seams.v1",
        "unit": str(src.get("Unit") or "unknown"),
        "pieces": pieces,
        "seams": seams,
        "summary": {
            "piece_count": len(pieces),
            "seam_group_count": len(seams),
            "seam_pair_count": sum(len(s["pairs"]) for s in seams),
        },
        "notes": [
            "Solver-neutral cloth seam metadata stored inside the Blender file.",
            "Consumers should not assume a specific garment authoring application.",
        ],
    }


def _store_ageha_cloth_seams(obj, pattern_json_path):
    payload = _build_ageha_cloth_seams(pattern_json_path)
    if not payload:
        obj["ageha_cloth_ready"] = False
        obj["ageha_cloth_seams_text"] = ""
        return None

    text_name = f"Ageha_ClothSeams_{obj.name}"
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    text.clear()
    text.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    obj["ageha_cloth_ready"] = True
    obj["ageha_cloth_seams_text"] = text.name
    obj["ageha_cloth_schema"] = payload["schema"]
    obj["ageha_cloth_unit"] = payload["unit"]
    obj["ageha_cloth_piece_count"] = payload["summary"]["piece_count"]
    obj["ageha_cloth_seam_group_count"] = payload["summary"]["seam_group_count"]
    obj["ageha_cloth_seam_pair_count"] = payload["summary"]["seam_pair_count"]
    return payload


def _shape_points_2d(shape):
    points = []
    for line in shape.get("LineList", []):
        for p in line.get("PointList", []):
            pos = p.get("Position") or {}
            try:
                xy = (float(pos["x"]), float(pos["y"]))
            except Exception:  # noqa: BLE001
                continue
            if (
                points
                and abs(points[-1][0] - xy[0]) < 1.0e-6
                and abs(points[-1][1] - xy[1]) < 1.0e-6
            ):
                continue
            points.append(xy)
    return points


def _bbox_2d(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {
        "min": (min(xs), min(ys)),
        "max": (max(xs), max(ys)),
        "center": ((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5),
        "size": (max(xs) - min(xs), max(ys) - min(ys)),
    }


def _slice_closed_polyline(points, start, end):
    if len(points) < 2:
        return []
    closed_points = list(points) + [points[0]]
    lengths = [0.0]
    for a, b in zip(closed_points, closed_points[1:]):
        lengths.append(
            lengths[-1] + ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        )
    total = lengths[-1]
    if total <= 1.0e-8:
        return []
    a = max(0.0, min(1.0, float(start)))
    b = max(0.0, min(1.0, float(end)))
    reverse = b < a
    lo = min(a, b) * total
    hi = max(a, b) * total
    out = []
    for i in range(len(points)):
        seg_lo = lengths[i]
        seg_hi = lengths[i + 1]
        if seg_hi >= lo and seg_lo <= hi:
            out.append(points[i])
    if reverse:
        out.reverse()
    return out


def _dist_to_segment_2d(p, a, b):
    vx = b[0] - a[0]
    vy = b[1] - a[1]
    wx = p[0] - a[0]
    wy = p[1] - a[1]
    denom = vx * vx + vy * vy
    if denom <= 1.0e-12:
        dx = p[0] - a[0]
        dy = p[1] - a[1]
        return (dx * dx + dy * dy) ** 0.5, 0.0
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    qx = a[0] + vx * t
    qy = a[1] + vy * t
    dx = p[0] - qx
    dy = p[1] - qy
    return (dx * dx + dy * dy) ** 0.5, t


def _closest_polyline_distance_2d(p, points):
    if len(points) < 2:
        return 1.0e30, 0.0
    accum = 0.0
    best_dist = 1.0e30
    best_s = 0.0
    for a, b in zip(points, points[1:]):
        seg_len = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
        dist, t = _dist_to_segment_2d(p, a, b)
        if dist < best_dist:
            best_dist = dist
            best_s = accum + seg_len * t
        accum += seg_len
    return best_dist, best_s


def _resample_vertices_by_s(vertices, count):
    if not vertices:
        return []
    ordered = sorted(vertices, key=lambda item: item[1])
    if count <= 1 or len(ordered) == 1:
        return [ordered[0][0]]
    out = []
    for i in range(count):
        j = round(i * (len(ordered) - 1) / (count - 1))
        out.append(ordered[j][0])
    return out


def _mesh_component_infos(obj):
    mesh = obj.data
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return []

    adjacent = [set() for _ in mesh.vertices]
    edge_face_count = {}
    for poly in mesh.polygons:
        verts = list(poly.vertices)
        for a, b in zip(verts, verts[1:] + verts[:1]):
            key = tuple(sorted((int(a), int(b))))
            edge_face_count[key] = edge_face_count.get(key, 0) + 1
    for e in mesh.edges:
        a, b = e.vertices
        adjacent[a].add(b)
        adjacent[b].add(a)

    boundary_vertices = set()
    for edge, count in edge_face_count.items():
        if count == 1:
            boundary_vertices.update(edge)

    vertex_uvs = [[] for _ in mesh.vertices]
    for loop in mesh.loops:
        uv = uv_layer.data[loop.index].uv
        vertex_uvs[loop.vertex_index].append((float(uv.x), float(uv.y)))
    avg_uv = []
    for values in vertex_uvs:
        if not values:
            avg_uv.append((0.0, 0.0))
            continue
        avg_uv.append(
            (
                sum(v[0] for v in values) / len(values),
                sum(v[1] for v in values) / len(values),
            )
        )

    seen = set()
    infos = []
    for start in range(len(mesh.vertices)):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        verts = []
        while stack:
            v = stack.pop()
            verts.append(v)
            for n in adjacent[v]:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        uv_points = [avg_uv[v] for v in verts]
        world_points = [obj.matrix_world @ mesh.vertices[v].co for v in verts]
        bbox = _bbox_2d(uv_points)
        infos.append(
            {
                "index": len(infos),
                "verts": set(verts),
                "boundary_verts": {v for v in verts if v in boundary_vertices},
                "uv": avg_uv,
                "uv_bbox": bbox,
                "world_center": sum((p for p in world_points), Vector())
                / max(1, len(world_points)),
            }
        )
    return infos


def _pattern_shape_maps(src):
    shapes = {}
    pieces = []
    for pattern in src.get("PatternList", []):
        pid = str(pattern.get("ID") or "")
        if not pid:
            continue
        outer_points = _shape_points_2d(pattern.get("ShapeInfo") or {})
        if len(outer_points) >= 2:
            shapes[pid] = {
                "id": pid,
                "piece_id": pid,
                "piece_name": str(pattern.get("Name") or pid),
                "kind": "outer",
                "closed": bool(pattern.get("IsClosed")),
                "points": outer_points,
                "bbox": _bbox_2d(outer_points),
            }
            pieces.append(shapes[pid])
        for internal in pattern.get("InternalLineList", []):
            sid = str(internal.get("ID") or "")
            pts = _shape_points_2d(internal)
            if sid and len(pts) >= 2:
                shapes[sid] = {
                    "id": sid,
                    "piece_id": pid,
                    "piece_name": str(pattern.get("Name") or pid),
                    "kind": "internal",
                    "closed": bool(internal.get("IsClosed")),
                    "points": pts,
                    "bbox": _bbox_2d(pts),
                }
    return shapes, pieces


def _assign_pieces_to_components(pieces, components):
    remaining = set(range(len(components)))
    mapping = {}

    def take_best(piece, candidates):
        psize = piece["bbox"]["size"]
        best = None
        for ci in list(candidates):
            csize = components[ci]["uv_bbox"]["size"]
            score = abs(psize[0] - csize[0]) + abs(psize[1] - csize[1])
            if best is None or score < best[0]:
                best = (score, ci)
        if best is None:
            return None
        remaining.discard(best[1])
        mapping[piece["piece_id"]] = best[1]
        return best[1]

    for piece in pieces:
        name = piece["piece_name"].lower()
        if "sleeve" not in name:
            continue
        candidates = remaining
        if "_l" in name or "left" in name:
            candidates = {
                i for i in remaining if components[i]["world_center"].x < 0.0
            } or remaining
        elif "_r" in name or "right" in name:
            candidates = {
                i for i in remaining if components[i]["world_center"].x > 0.0
            } or remaining
        take_best(piece, candidates)

    for piece in pieces:
        name = piece["piece_name"].lower()
        if piece["piece_id"] in mapping:
            continue
        candidates = remaining
        if "front" in name:
            candidates = {
                i for i in remaining if components[i]["world_center"].y > 0.0
            } or remaining
        elif "back" in name:
            candidates = {
                i for i in remaining if components[i]["world_center"].y < 0.0
            } or remaining
        take_best(piece, candidates)

    return mapping


def _pattern_to_uv_transform(piece, component):
    pc = piece["bbox"]["center"]
    uc = component["uv_bbox"]["center"]
    return uc[0] - pc[0], uc[1] - pc[1]


def _vertices_near_segment(component, piece, shape, ref, tolerance_mm):
    if shape["kind"] == "outer":
        pattern_points = _slice_closed_polyline(
            shape["points"], ref["start"], ref["end"]
        )
        source_verts = component["boundary_verts"]
    else:
        pattern_points = shape["points"]
        source_verts = component["verts"]
    if len(pattern_points) < 2:
        return []
    dx, dy = _pattern_to_uv_transform(piece, component)
    segment = [(p[0] + dx, p[1] + dy) for p in pattern_points]
    result = []
    for v in source_verts:
        uv = component["uv"][v]
        dist, s = _closest_polyline_distance_2d(uv, segment)
        if dist <= tolerance_mm:
            result.append((v, s, dist))
    if len(result) >= 2:
        return result
    if tolerance_mm < 12.0:
        return _vertices_near_segment(
            component, piece, shape, ref, tolerance_mm * 2.0
        )
    return result


def _add_loose_edges(obj, edge_pairs):
    if not edge_pairs:
        return 0
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    existing = {
        tuple(sorted((e.verts[0].index, e.verts[1].index))) for e in bm.edges
    }
    added = 0
    for a, b in edge_pairs:
        if a == b:
            continue
        key = tuple(sorted((int(a), int(b))))
        if key in existing:
            continue
        try:
            bm.edges.new((bm.verts[key[0]], bm.verts[key[1]]))
            existing.add(key)
            added += 1
        except ValueError:
            pass
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj.update_tag()
    return added


def _install_cloth_sewing_modifier(obj):
    mod = bpy.data.objects[obj.name].modifiers.get("Tanabata Sewing Data")
    if mod is None or mod.type != "CLOTH":
        mod = obj.modifiers.new("Tanabata Sewing Data", "CLOTH")
    mod.show_viewport = False
    mod.show_render = False
    mod.settings.use_sewing_springs = True
    mod.settings.sewing_force_max = 10.0
    return mod.name


def _store_blender_cloth_sewing_data(obj, pattern_json_path):
    if not pattern_json_path or not os.path.exists(pattern_json_path):
        obj["tanabata_sewing_ready"] = False
        obj["tanabata_sewing_status"] = "pattern JSON not found"
        return {"ready": False, "status": obj["tanabata_sewing_status"]}

    with open(pattern_json_path, "r", encoding="utf-8") as f:
        src = json.load(f)
    shapes, pieces = _pattern_shape_maps(src)
    components = _mesh_component_infos(obj)
    if not shapes or not pieces or not components:
        obj["tanabata_sewing_ready"] = False
        obj["tanabata_sewing_status"] = "missing shapes, pieces, components, or UVMap"
        return {"ready": False, "status": obj["tanabata_sewing_status"]}

    component_by_piece = _assign_pieces_to_components(pieces, components)
    piece_by_id = {p["piece_id"]: p for p in pieces}
    edge_pairs = []
    seam_reports = []
    for group in src.get("SeamLinePairGroupList", []):
        group_pairs = 0
        failures = []
        for pair in group.get("PairList", []):
            refs = []
            for side in ("First", "Second"):
                raw = pair.get(side) or {}
                shape = shapes.get(str(raw.get("ShapeID") or ""))
                if shape is None:
                    failures.append(f"{side}: shape missing")
                    refs.append(None)
                    continue
                pid = shape["piece_id"]
                ci = component_by_piece.get(pid)
                piece = piece_by_id.get(pid)
                if ci is None or piece is None:
                    failures.append(f"{side}: component missing")
                    refs.append(None)
                    continue
                length_param = raw.get("LengthParam") or {}
                ref = {
                    "start": float(length_param.get("fStart", 0.0)),
                    "end": float(length_param.get("fEnd", 0.0)),
                }
                verts = _vertices_near_segment(
                    components[ci], piece, shape, ref, 3.0
                )
                refs.append(verts)
            if refs[0] is None or refs[1] is None:
                continue
            count = min(len(refs[0]), len(refs[1]))
            if count < 2:
                failures.append(
                    f"not enough vertices ({len(refs[0])}, {len(refs[1])})"
                )
                continue
            va = _resample_vertices_by_s(refs[0], count)
            vb = _resample_vertices_by_s(refs[1], count)
            for a, b in zip(va, vb):
                edge_pairs.append((a, b))
                group_pairs += 1
        seam_reports.append(
            {
                "name": str(group.get("Name") or ""),
                "pairs": group_pairs,
                "failures": failures[:4],
            }
        )

    unique_pairs = sorted(
        set(tuple(sorted((int(a), int(b)))) for a, b in edge_pairs if a != b)
    )
    added = _add_loose_edges(obj, unique_pairs)
    mod_name = _install_cloth_sewing_modifier(obj) if unique_pairs else ""

    text_name = f"Tanabata_SewingEdges_{obj.name}"
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    payload = {
        "schema": "tanabata.blender_cloth_sewing.v1",
        "source": "MD pattern JSON converted to Blender loose sewing edges",
        "cloth_modifier": mod_name,
        "edge_count": len(unique_pairs),
        "added_edge_count": added,
        "piece_component_map": {
            piece_id: int(component_index)
            for piece_id, component_index in component_by_piece.items()
        },
        "seams": seam_reports,
    }
    text.clear()
    text.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    obj["tanabata_sewing_ready"] = bool(unique_pairs)
    obj["tanabata_sewing_edges_text"] = text.name
    obj["tanabata_sewing_edge_count"] = len(unique_pairs)
    obj["tanabata_sewing_modifier"] = mod_name
    obj["tanabata_sewing_status"] = (
        f"{len(unique_pairs)} loose sewing edges, {added} added"
        if unique_pairs
        else "no sewing edges created"
    )
    return {
        "ready": bool(unique_pairs),
        "edge_count": len(unique_pairs),
        "added_edge_count": added,
        "modifier": mod_name,
        "text": text.name,
        "status": obj["tanabata_sewing_status"],
    }


def _delete_packed_temp_images():
    removed = 0
    root = os.path.normpath(TEMP_DIR)
    for img in list(bpy.data.images):
        try:
            if img.packed_file and img.filepath:
                p = os.path.normpath(bpy.path.abspath(img.filepath))
                if p.startswith(root) and os.path.exists(p):
                    os.remove(p)
                    removed += 1
        except Exception:  # noqa: BLE001
            pass
    return removed


# --------------------------------------------------------------------------- #
# Socket server (main-thread drain via bpy timer)
# --------------------------------------------------------------------------- #
class _BridgeServer:
    def __init__(self):
        self.sock = None
        self.thread = None
        self.stop = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.pending: list[dict] = []
        self._timer_cb = self._drain

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(5)
        s.settimeout(0.5)
        self.sock = s
        self.stop.clear()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        if not bpy.app.timers.is_registered(self._timer_cb):
            bpy.app.timers.register(self._timer_cb, persistent=True)
        _log(f"listener started on {HOST}:{PORT}")

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if bpy.app.timers.is_registered(self._timer_cb):
            bpy.app.timers.unregister(self._timer_cb)
        _log("listener stopped")

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._serve_conn(conn)
            except Exception:
                _log("conn error:\n" + traceback.format_exc())
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _serve_conn(self, conn):
        conn.settimeout(15.0)
        buf = bytearray()
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8").strip()
        if not line:
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"bad json from MD: {exc}")
            self._send(conn, {"id": None, "error": f"bad json: {exc}"})
            return

        req_id = req.get("id")
        method = req.get("method")
        _log(f"MD request method={method!r} id={req_id}")
        if method not in HANDLERS:
            self._send(conn, {"id": req_id, "error": f"unknown method: {method}"})
            return

        job = {
            "req": req,
            "event": threading.Event(),
            "result": None,
            "error": None,
        }
        self.q.put(job)
        if not job["event"].wait(timeout=1800.0):
            self._send(conn, {"id": req_id, "error": "main-thread timeout"})
            return
        if job["error"] is not None:
            self._send(conn, {"id": req_id, "error": job["error"]})
        else:
            self._send(conn, {"id": req_id, "result": job["result"]})

    @staticmethod
    def _send(conn, obj):
        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def _drain(self):
        while True:
            try:
                job = self.q.get_nowait()
            except queue.Empty:
                break
            method = job["req"].get("method")
            params = job["req"].get("params") or {}
            try:
                job["result"] = HANDLERS[method](params)
            except Exception as exc:  # noqa: BLE001
                job["error"] = f"{exc}"
                _log(f"handler '{method}' failed:\n" + traceback.format_exc())
            job["event"].set()
        if self.pending:
            task = self.pending.pop(0)
            _run_export_task(task)
        return 0.05

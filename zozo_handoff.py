# SPDX-License-Identifier: GPL-3.0-or-later
"""服とボディを ZOZO 用に書き出し、MCP まで縫製 (housei) 後半と同じ流れで設定する。

1. 服コピー
2. ZOZO 向け縫い辺（緩いステッチ）を再構築
3. ボディコピー（足首〜首の高さ帯でカット）
4. 自己交差 検査→修理→再検査
5. 三角品質
6. PASS なら MCP 設定
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import bpy
import numpy as np

from .i18n import msg
from .shell_isect_bridge import ShellIsectReport, run_check_and_fix
from .zozo_stitches import StitchRebuildReport, rebuild_zozo_stitches


ZOZO_MCP_PORT = 9633
ZOZO_CONTACT_GAP_M = 0.001
_HANDOFF_COLLECTION_ROLE = "zozo_handoff"
_HANDOFF_CLOTH_ROLE = "zozo_cloth"
_HANDOFF_BODY_ROLE = "zozo_body"

_DEFAULT_SPACING_M = 0.010
_REST_AREA_FLOOR_FRACTION = 1.0e-6
_MAX_REPORT_FACES = 8

# ボディ自動認識の優先名
BODY_NAME_CANDIDATES = (
    "CC_Base_Body",
    "CC_BODY",
    "CC_Body",
    "Body",
    "body",
)
CLOTHES_NAME_CANDIDATES = (
    "output.001",
    "output",
)


class ZozoHandoffError(RuntimeError):
    """現在の状態では ZOZO に安全に渡せない。"""


@dataclass(frozen=True)
class ClothQualityReport:
    checked_faces: int
    area_min_m2: float
    area_floor_m2: float
    edge_min_m: float
    aspect_min: float
    failing_faces: int
    worst: tuple[tuple[int, float, float], ...] = ()

    @property
    def passed(self) -> bool:
        return self.failing_faces == 0

    def summary(self) -> str:
        return msg(
            "quality_summary",
            faces=self.checked_faces,
            area_min=self.area_min_m2,
            floor=self.area_floor_m2,
            edge_mm=self.edge_min_m * 1000.0,
            aspect=self.aspect_min,
            failing=self.failing_faces,
        )

    def error_report(self) -> str:
        text = msg(
            "quality_error",
            failing=self.failing_faces,
            area_min=self.area_min_m2,
            floor=self.area_floor_m2,
        )
        if self.worst:
            shown = ", ".join(
                msg(
                    "quality_worst_item",
                    index=index,
                    area=area,
                    edge_mm=edge * 1000.0,
                )
                for index, area, edge in self.worst[:_MAX_REPORT_FACES]
            )
            text += msg("quality_worst", shown=shown)
            if self.failing_faces > len(self.worst[:_MAX_REPORT_FACES]):
                remaining = self.failing_faces - len(self.worst[:_MAX_REPORT_FACES])
                text += msg("quality_worst_more", n=remaining)
        return text


@dataclass(frozen=True)
class ZozoPreparation:
    collection: bpy.types.Collection
    cloth_object: bpy.types.Object
    body_object: bpy.types.Object | None
    cloth_group_name: str
    body_group_name: str
    project_name: str
    shell_isect: ShellIsectReport
    stitch: StitchRebuildReport
    abort_message: str | None = None
    quality: ClothQualityReport | None = None
    body_faces_kept: int = 0
    body_faces_total: int = 0

    def mcp_configuration(self, scene: bpy.types.Scene) -> dict:
        """縫製 (housei) と同じ MCP 設定項目。"""
        if self.body_object is None:
            raise ZozoHandoffError(msg("zozo_no_body_export"))
        frame_start, frame_count, fps = _sync_scene_timeline_for_zozo(scene)
        step_size = 1.0 / float(fps) / 8.0
        if step_size < 0.001 or step_size > 0.01:
            step_size = 0.005
        return {
            "port": ZOZO_MCP_PORT,
            "cloth_object": self.cloth_object.name,
            "body_object": self.body_object.name,
            "cloth_group": self.cloth_group_name,
            "body_group": self.body_group_name,
            "scene_parameters": {
                "step_size": float(step_size),
                "frame_start": int(frame_start),
                "frame_count": int(frame_count),
                "use_scene_frame_start": False,
                "use_scene_fps": False,
                "frame_rate": int(fps),
                "gravity": [0.0, 0.0, -9.81],
                "inactive_momentum_frames": 5,
                "project_name": self.project_name,
            },
            "cloth_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
                "deformation_damping": 0.005,
                "bending_damping": 0.002,
                "bend_rest_angle_source": "FROM_GEOMETRY",
            },
            "body_properties": {
                "contact_gap": ZOZO_CONTACT_GAP_M,
                "contact_offset": 0.0,
                "enable_soft_constraint": True,
                "soft_constraint_stiffness": 10.0,
            },
            "capture_timeout_seconds": 300.0,
        }


def find_body_object() -> bpy.types.Object | None:
    """シーンからボディ候補を探す（CC_Base_Body 優先）。"""
    for name in BODY_NAME_CANDIDATES:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            return obj
    # 部分一致フォールバック
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        lower = obj.name.lower()
        if lower in {"cc_base_body", "cc_body"} or lower.endswith("_body"):
            if "eye" in lower or "teeth" in lower or "tongue" in lower:
                continue
            return obj
    return None


def find_clothes_object() -> bpy.types.Object | None:
    for name in CLOTHES_NAME_CANDIDATES:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            return obj
    return None


def estimate_body_export_band_cm(
    body: bpy.types.Object | None,
) -> tuple[float, float]:
    """足首〜首の書き出し高さ (cm) をボディの世界 Z から推定。"""
    if body is None or body.type != "MESH" or len(body.data.vertices) == 0:
        return 5.0, 145.0
    mesh = body.data
    n = len(mesh.vertices)
    local = np.empty((n, 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", local.ravel())
    matrix = np.asarray([tuple(row) for row in body.matrix_world], dtype=np.float64)
    world = local @ matrix[:3, :3].T + matrix[:3, 3]
    z_min = float(world[:, 2].min())
    z_max = float(world[:, 2].max())
    height = max(0.5, z_max - z_min)
    # 足首: 足底から約 5 cm 上。首: 全高の約 82%（頭を切る）
    ankle_m = z_min + 0.05
    neck_m = z_min + 0.82 * height
    ankle_cm = max(0.0, ankle_m * 100.0)
    neck_cm = max(ankle_cm + 10.0, neck_m * 100.0)
    return round(ankle_cm, 1), round(neck_cm, 1)


def _scene_fps(scene: bpy.types.Scene) -> int:
    return max(1, int(round(float(scene.render.fps) / float(scene.render.fps_base))))


def _intended_frame_range(scene: bpy.types.Scene) -> tuple[int, int]:
    start = int(scene.frame_start)
    end = int(scene.frame_end)
    if bool(getattr(scene, "use_preview_range", False)):
        p0 = int(scene.frame_preview_start)
        p1 = int(scene.frame_preview_end)
        start = min(start, p0)
        end = max(end, p1)
    try:
        s0 = int(getattr(scene, "simulation_frame_start", start))
        s1 = int(getattr(scene, "simulation_frame_end", end))
        start = min(start, s0)
        end = max(end, s1)
    except (TypeError, ValueError):
        pass
    if end < start:
        end = start
    return start, end


def _sync_scene_timeline_for_zozo(scene: bpy.types.Scene) -> tuple[int, int, int]:
    start, end = _intended_frame_range(scene)
    frame_count = max(10, end - start + 1)
    end = start + frame_count - 1
    fps = _scene_fps(scene)
    scene.frame_start = int(start)
    scene.frame_end = int(end)
    if hasattr(scene, "frame_preview_start"):
        scene.frame_preview_start = int(start)
    if hasattr(scene, "frame_preview_end"):
        scene.frame_preview_end = int(end)
    try:
        if hasattr(scene, "simulation_frame_start"):
            scene.simulation_frame_start = int(start)
        if hasattr(scene, "simulation_frame_end"):
            scene.simulation_frame_end = int(end)
    except (AttributeError, TypeError):
        pass
    return int(start), int(frame_count), int(fps)


def _remove_object_and_owned_mesh(obj: bpy.types.Object) -> None:
    mesh = obj.data if obj.type == "MESH" else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _handoff_collection(context, cloth: bpy.types.Object) -> bpy.types.Collection:
    key = cloth.name
    matches = [
        collection
        for collection in bpy.data.collections
        if collection.get("ominaeshi_role") == _HANDOFF_COLLECTION_ROLE
        and collection.get("ominaeshi_source_cloth") == key
    ]
    handoff = matches[0] if matches else bpy.data.collections.new(f"{key}_ZOZO")
    if not matches:
        context.scene.collection.children.link(handoff)
    handoff["ominaeshi_role"] = _HANDOFF_COLLECTION_ROLE
    handoff["ominaeshi_source_cloth"] = key
    for collection in matches:
        for obj in list(collection.objects):
            if obj.get("ominaeshi_role") in {_HANDOFF_CLOTH_ROLE, _HANDOFF_BODY_ROLE}:
                _remove_object_and_owned_mesh(obj)
    return handoff


def _crop_mesh_to_world_z_slab(
    obj: bpy.types.Object, z_min_m: float, z_max_m: float
) -> tuple[int, int]:
    """足首より下・首より上の三角を落とす（世界 Z 帯）。"""
    mesh = obj.data
    faces_before = len(mesh.polygons)
    if faces_before == 0:
        return 0, 0
    if z_max_m < z_min_m:
        z_min_m, z_max_m = z_max_m, z_min_m

    matrix = obj.matrix_world
    world = np.empty((len(mesh.vertices), 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", world.ravel())
    rot = np.asarray([tuple(row) for row in matrix.to_3x3()], dtype=np.float64)
    loc = np.asarray(matrix.to_translation(), dtype=np.float64)
    world = world @ rot.T + loc
    z = world[:, 2]

    keep_poly: list[int] = []
    for poly in mesh.polygons:
        zs = z[list(poly.vertices)]
        if float(zs.max()) < z_min_m or float(zs.min()) > z_max_m:
            continue
        keep_poly.append(poly.index)
    if len(keep_poly) == faces_before:
        return faces_before, faces_before
    if not keep_poly:
        return faces_before, faces_before

    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        keep_set = set(keep_poly)
        to_delete = [f for f in bm.faces if f.index not in keep_set]
        bmesh.ops.delete(bm, geom=to_delete, context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return len(mesh.polygons), faces_before


def _create_cloth_object(
    handoff: bpy.types.Collection,
    source: bpy.types.Object,
) -> bpy.types.Object:
    if source.type != "MESH":
        raise ZozoHandoffError(msg("zozo_need_clothes"))
    mesh_src = source.data
    mesh_src.calc_loop_triangles()
    if len(mesh_src.loop_triangles) == 0:
        raise ZozoHandoffError(msg("zozo_cloth_no_tris"))

    duplicate = source.copy()
    duplicate.data = mesh_src.copy()
    duplicate.name = f"{source.name}_ZOZO_CLOTH"
    duplicate.data.name = f"{duplicate.name}_MESH"
    # ソースの CLOTH 修飾子は ZOZO に不要
    for mod in list(duplicate.modifiers):
        duplicate.modifiers.remove(mod)
    if "_solver_uuid" in duplicate:
        del duplicate["_solver_uuid"]
    handoff.objects.link(duplicate)
    try:
        duplicate.matrix_world = source.matrix_world.copy()
    except Exception:
        pass

    n = len(duplicate.data.vertices)
    coords = np.empty(n * 3, dtype=np.float64)
    duplicate.data.vertices.foreach_get("co", coords)
    if not np.all(np.isfinite(coords)):
        _remove_object_and_owned_mesh(duplicate)
        raise ZozoHandoffError(msg("zozo_nonfinite"))

    duplicate["ominaeshi_role"] = _HANDOFF_CLOTH_ROLE
    duplicate["ominaeshi_source_cloth"] = source.name
    duplicate["ominaeshi_zozo_contact_gap_m"] = ZOZO_CONTACT_GAP_M
    return duplicate


def _create_body_object(
    handoff: bpy.types.Collection,
    source_cloth_name: str,
    body: bpy.types.Object,
    *,
    z_min_m: float | None = None,
    z_max_m: float | None = None,
) -> tuple[bpy.types.Object, int, int]:
    """ボディをコピーして足首〜首で面を切る。

    縫製 (housei) と同じく **アーマチュア親・修飾子・頂点グループは残す**。
    トポロジだけ帯域外を落とし、ZOZO の STATIC 変形キャプチャが動くようにする。
    静的ベイクはしない（するとアニメが消える）。
    """
    if body.type != "MESH":
        raise ZozoHandoffError(msg("zozo_need_body"))
    body.data.calc_loop_triangles()
    if len(body.data.loop_triangles) == 0:
        raise ZozoHandoffError(msg("zozo_body_no_tris"))

    duplicate = body.copy()
    duplicate.data = body.data.copy()
    duplicate.name = f"{source_cloth_name}_ZOZO_BODY"
    duplicate.data.name = f"{duplicate.name}_MESH"
    # ZOZO UUID はソースと衝突させない
    if "_solver_uuid" in duplicate:
        del duplicate["_solver_uuid"]
    handoff.objects.link(duplicate)
    # object.copy は親を保持する。世界行列をソースに揃えてから Z カットする。
    try:
        duplicate.matrix_world = body.matrix_world.copy()
    except Exception:
        pass
    duplicate["ominaeshi_role"] = _HANDOFF_BODY_ROLE
    duplicate["ominaeshi_source_cloth"] = source_cloth_name
    duplicate["ominaeshi_source_body"] = body.name
    kept, total = len(duplicate.data.polygons), len(duplicate.data.polygons)
    if z_min_m is not None and z_max_m is not None:
        # オブジェクト空間×世界行列で帯を決める（レスト＋オブジェクト変換。
        # アーマチュア変形後の見た目ではないが、直立キャラの足首/首帯には足りる。
        # 頂点グループは残るので修飾子が毎フレーム変形する。）
        kept, total = _crop_mesh_to_world_z_slab(
            duplicate, float(z_min_m), float(z_max_m)
        )
        duplicate["ominaeshi_body_z_min_m"] = float(min(z_min_m, z_max_m))
        duplicate["ominaeshi_body_z_max_m"] = float(max(z_min_m, z_max_m))
        duplicate["ominaeshi_body_faces_kept"] = int(kept)
        duplicate["ominaeshi_body_faces_total"] = int(total)
    duplicate.display_type = "WIRE"
    duplicate.show_in_front = True
    duplicate.hide_render = True
    return duplicate, int(kept), int(total)


def _cloth_quality(cloth: bpy.types.Object) -> ClothQualityReport:
    mesh = cloth.data
    mesh.calc_loop_triangles()
    face_count = len(mesh.loop_triangles)
    vertex_count = len(mesh.vertices)
    if face_count == 0 or vertex_count == 0:
        return ClothQualityReport(0, 0.0, 0.0, 0.0, 0.0, 0)

    positions = np.empty((vertex_count, 3), dtype=np.float64)
    mesh.vertices.foreach_get("co", positions.ravel())
    triangles = np.empty(face_count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    triangles = triangles.reshape(face_count, 3)

    floor = (_DEFAULT_SPACING_M ** 2) * _REST_AREA_FLOOR_FRACTION
    floors = np.full(face_count, floor, dtype=np.float64)

    corners = positions[triangles]
    first = corners[:, 1] - corners[:, 0]
    second = corners[:, 2] - corners[:, 0]
    areas = 0.5 * np.linalg.norm(np.cross(first, second), axis=1)
    lengths = np.stack(
        [
            np.linalg.norm(first, axis=1),
            np.linalg.norm(second, axis=1),
            np.linalg.norm(corners[:, 2] - corners[:, 1], axis=1),
        ],
        axis=1,
    )
    longest = lengths.max(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        aspect = np.where(longest > 0.0, 2.0 * areas / (longest * longest), 0.0)

    failing = np.flatnonzero(areas < floors)
    worst = tuple(
        (int(index), float(areas[index]), float(lengths[index].min()))
        for index in failing[np.argsort(areas[failing])][:_MAX_REPORT_FACES]
    )
    return ClothQualityReport(
        checked_faces=face_count,
        area_min_m2=float(areas.min()),
        area_floor_m2=float(floor),
        edge_min_m=float(lengths.min()),
        aspect_min=float(aspect.min()),
        failing_faces=int(failing.size),
        worst=worst,
    )


def _project_name(cloth_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", cloth_name).strip("_")
    return f"ominaeshi_{value or 'clothes'}"


def prepare_for_zozo(
    context,
    cloth: bpy.types.Object | None,
    body: bpy.types.Object | None,
    *,
    shell_isect_include_body: bool = True,
    body_z_min_m: float = 0.05,
    body_z_max_m: float = 1.45,
) -> ZozoPreparation:
    if cloth is None or cloth.type != "MESH":
        raise ZozoHandoffError(msg("zozo_need_clothes"))
    if body is None or body.type != "MESH":
        raise ZozoHandoffError(msg("zozo_need_body"))
    if cloth == body:
        raise ZozoHandoffError(msg("zozo_same_object"))
    context.view_layer.update()

    handoff = _handoff_collection(context, cloth)
    cloth_copy = _create_cloth_object(handoff, cloth)

    # ZOZO 向け縫い辺をコピー上で再構築（元の服は触らない）
    stitch_report = rebuild_zozo_stitches(cloth_copy)
    cloth_copy["ominaeshi_stitch_count"] = int(stitch_report.stitch_count)
    cloth_copy["ominaeshi_stitch_added"] = int(stitch_report.added_edges)
    cloth_copy["ominaeshi_stitch_summary"] = stitch_report.summary_ja()

    try:
        # ソースボディを直接コピー（親アーマチュア・修飾子を維持）
        body_copy, kept, total = _create_body_object(
            handoff,
            cloth.name,
            body,
            z_min_m=float(body_z_min_m),
            z_max_m=float(body_z_max_m),
        )
    except Exception:
        _remove_object_and_owned_mesh(cloth_copy)
        raise

    body_copy["ominaeshi_source_body"] = body.name

    for selected in context.selected_objects:
        selected.select_set(False)
    cloth_copy.select_set(True)
    context.view_layer.objects.active = cloth_copy
    context.view_layer.update()

    # グループ名は縫製と同型（製品名だけ女郎花）
    cloth_group_name = f"女郎花 {cloth.name} 服"
    body_group_name = f"女郎花 {cloth.name} ボディ"
    cloth_copy["ominaeshi_zozo_group"] = cloth_group_name
    body_copy["ominaeshi_zozo_group"] = body_group_name

    shell_report = run_check_and_fix(
        cloth_copy,
        body_copy,
        include_body=bool(shell_isect_include_body),
    )
    cloth_copy["ominaeshi_shell_isect"] = shell_report.summary()
    cloth_copy["ominaeshi_shell_isect_version"] = shell_report.version
    cloth_copy["ominaeshi_shell_isect_include_body"] = bool(shell_report.include_body)
    cloth_copy["ominaeshi_shell_isect_pipeline"] = shell_report.pipeline_token()
    cloth_copy["ominaeshi_shell_isect_checks_run"] = int(shell_report.checks_run)
    cloth_copy["ominaeshi_shell_isect_fix_attempted"] = bool(shell_report.fix_attempted)
    cloth_copy["ominaeshi_shell_isect_pairs_before"] = int(shell_report.pairs_before)
    cloth_copy["ominaeshi_shell_isect_pairs_after"] = int(shell_report.pairs_after)
    cloth_copy["ominaeshi_shell_isect_fix"] = shell_report.fix_status
    cloth_copy["ominaeshi_shell_isect_cloth_faces"] = int(shell_report.n_cloth_faces)
    if shell_report.pairs:
        cloth_copy["ominaeshi_shell_isect_face_pairs"] = [
            f"{a},{b}" for a, b in shell_report.pairs
        ]
    elif "ominaeshi_shell_isect_face_pairs" in cloth_copy:
        del cloth_copy["ominaeshi_shell_isect_face_pairs"]

    if not shell_report.passed:
        return ZozoPreparation(
            collection=handoff,
            cloth_object=cloth_copy,
            body_object=body_copy,
            cloth_group_name=cloth_group_name,
            body_group_name=body_group_name,
            project_name=_project_name(cloth.name),
            shell_isect=shell_report,
            stitch=stitch_report,
            abort_message=shell_report.error_report(),
            body_faces_kept=kept,
            body_faces_total=total,
        )

    quality = _cloth_quality(cloth_copy)
    cloth_copy["ominaeshi_rest_area_min_m2"] = float(quality.area_min_m2)
    cloth_copy["ominaeshi_rest_area_floor_m2"] = float(quality.area_floor_m2)
    cloth_copy["ominaeshi_edge_min_m"] = float(quality.edge_min_m)
    cloth_copy["ominaeshi_aspect_min"] = float(quality.aspect_min)
    cloth_copy["ominaeshi_quality_failing_faces"] = int(quality.failing_faces)

    return ZozoPreparation(
        collection=handoff,
        cloth_object=cloth_copy,
        body_object=body_copy,
        cloth_group_name=cloth_group_name,
        body_group_name=body_group_name,
        project_name=_project_name(cloth.name),
        shell_isect=shell_report,
        stitch=stitch_report,
        abort_message=None if quality.passed else quality.error_report(),
        quality=quality,
        body_faces_kept=kept,
        body_faces_total=total,
    )

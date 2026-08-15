# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花 N パネル（MD OBJ → HOU）。"""

from __future__ import annotations

import os
import tomllib

import bpy
from bpy.props import PointerProperty, StringProperty
from bpy.types import Collection, Object, Operator, Panel, PropertyGroup

from . import md_bridge
from .hou_export import HouExportError, create_hou_collection
from .i18n import msg


def _version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as stream:
            return str(tomllib.load(stream).get("version", "?"))
    except Exception:  # noqa: BLE001
        return "?"


def _wrap_status_lines(text: str, width: int = 52) -> list[str]:
    raw = (text or "").strip() or msg("ready")
    lines: list[str] = []
    for paragraph in raw.replace("\r\n", "\n").split("\n"):
        paragraph = paragraph.strip()
        while len(paragraph) > width:
            cut = paragraph.rfind(" ", 0, width)
            if cut < width // 2:
                cut = width
            lines.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        if paragraph:
            lines.append(paragraph)
    return lines or [msg("ready")]


def _draw_status_box(layout, props) -> None:
    box = layout.box()
    box.label(text="メッセージ")
    column = box.column(align=True)
    lines = _wrap_status_lines(props.parse_status, width=46)
    while len(lines) < 5:
        lines.append("")
    for line in lines[:12]:
        column.label(text=line if line else " ")


def _mesh_object_poll(_properties, obj: Object) -> bool:
    return obj.type == "MESH"


def _find_body_object() -> Object | None:
    for name in ("CC_Base_Body", "Body", "body"):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            return obj
    candidates = [
        obj
        for obj in bpy.data.objects
        if obj.type == "MESH" and "body" in obj.name.lower()
    ]
    return max(candidates, key=lambda obj: len(obj.data.polygons), default=None)


def _find_clothes_object() -> Object | None:
    imported = [
        obj
        for obj in bpy.data.objects
        if obj.type == "MESH" and bool(obj.get("ominaeshi_from_md", False))
    ]
    if imported:
        return max(imported, key=lambda obj: len(obj.data.polygons))
    for name in ("output.001", "output", "garment", "Garment"):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            return obj
    return None


def _ensure_auto_inputs(props) -> None:
    changed = False
    if props.body_object is None:
        body = _find_body_object()
        if body is not None:
            props.body_object = body
            changed = True
    if props.clothes_object is None:
        clothes = _find_clothes_object()
        if clothes is not None:
            props.clothes_object = clothes
            changed = True
    if changed and (props.parse_status or "").strip() in ("", msg("ready")):
        props.parse_status = msg(
            "auto_set",
            clothes=props.clothes_object.name if props.clothes_object else "（なし）",
            body=props.body_object.name if props.body_object else "（なし）",
        )


class OminaeshiProperties(PropertyGroup):
    parse_status: StringProperty(
        name="状態",
        description="状態と警告",
        default="準備完了",
        options={"TEXTEDIT_UPDATE"},
    )
    clothes_object: PointerProperty(
        name="服 OBJ",
        description="MDから取り込んだ服メッシュ（Pattern JSON付き）",
        type=Object,
        poll=_mesh_object_poll,
    )
    body_object: PointerProperty(
        name="ボディ",
        description="MDへ送るアバターメッシュ",
        type=Object,
        poll=_mesh_object_poll,
    )
    hou_collection: PointerProperty(
        name="HOUコレクション",
        description="最後に女郎花が作成したHOU衣服コレクション",
        type=Collection,
    )


class OMINAESHI_OT_auto_set(Operator):
    bl_idname = "ominaeshi.auto_set"
    bl_label = "自動セット"
    bl_description = "MDから取り込んだ服と CC_Base_Body などを探して設定します"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.ominaeshi
        clothes = _find_clothes_object()
        body = _find_body_object()
        if clothes is not None:
            props.clothes_object = clothes
        if body is not None:
            props.body_object = body
        if clothes is None and body is None:
            props.parse_status = msg("auto_set_none")
            return {"CANCELLED"}
        props.parse_status = msg(
            "auto_set",
            clothes=props.clothes_object.name if props.clothes_object else "（なし）",
            body=props.body_object.name if props.body_object else "（なし）",
        )
        return {"FINISHED"}


class OMINAESHI_OT_md_listener_start(Operator):
    bl_idname = "ominaeshi.md_listener_start"
    bl_label = "MDブリッジ開始"
    bl_description = (
        "Marvelous Designer用TCPリスナーを開始します。"
        "1_get_BL_avater と 2_send_clothes_BL に応答します"
    )
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.ominaeshi
        try:
            props.parse_status = md_bridge.start_listener()
        except Exception as exc:  # noqa: BLE001
            props.parse_status = msg("md_listener_fail", message=str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class OMINAESHI_OT_md_listener_stop(Operator):
    bl_idname = "ominaeshi.md_listener_stop"
    bl_label = "MDブリッジ停止"
    bl_description = "Marvelous Designer用TCPリスナーを停止します"
    bl_options = {"REGISTER"}

    def execute(self, context):
        context.scene.ominaeshi.parse_status = md_bridge.stop_listener()
        return {"FINISHED"}


class OMINAESHI_OT_create_hou(Operator):
    bl_idname = "ominaeshi.create_hou"
    bl_label = "HOU化"
    bl_description = (
        "MDの服OBJをパーツに分け、型紙座標と正確な縫い頂点ペアを持つ"
        "HOUコレクションを作成します。元のOBJは変更しません"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        props = context.scene.ominaeshi
        if props.clothes_object is None:
            props.parse_status = msg("hou_need_clothes")
            return {"CANCELLED"}
        props.parse_status = msg("hou_working")
        try:
            result = create_hou_collection(context, props.clothes_object)
        except HouExportError as exc:
            props.parse_status = msg("hou_failed", message=str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # noqa: BLE001
            props.parse_status = msg("hou_failed", message=f"{type(exc).__name__}: {exc}")
            return {"CANCELLED"}
        props.hou_collection = result.collection
        props.parse_status = msg(
            "hou_done",
            collection=result.collection.name,
            parts=result.part_count,
            labels=result.seam_label_count,
            pairs=result.pair_count,
        )
        return {"FINISHED"}


class OMINAESHI_PT_main(Panel):
    bl_idname = "OMINAESHI_PT_main"
    bl_label = "女郎花"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "女郎花"

    def draw(self, context):
        layout = self.layout
        props = context.scene.ominaeshi
        layout.label(text=f"女郎花 v{_version()}")

        bridge = layout.box()
        bridge.label(text="MD ブリッジ（ボディ→MD / 服OBJ→Blender）")
        bridge.label(text=f"状態: {md_bridge.listener_status_ja()}")
        row = bridge.row(align=True)
        row.operator(OMINAESHI_OT_md_listener_start.bl_idname, text="開始")
        row.operator(OMINAESHI_OT_md_listener_stop.bl_idname, text="停止")
        bridge.label(text="MD: 1_get_BL_avater → 服作成 → 2_send_clothes_BL")

        inputs = layout.box()
        inputs.label(text="入力")
        inputs.prop(props, "clothes_object", text="服 OBJ")
        inputs.prop(props, "body_object", text="MD用ボディ")
        inputs.operator(OMINAESHI_OT_auto_set.bl_idname, text="自動セット")

        output = layout.box()
        output.label(text="HOU")
        output.operator(OMINAESHI_OT_create_hou.bl_idname, text="HOU")
        output.prop(props, "hou_collection", text="出力", emboss=False)
        output.label(text="元OBJを残し、KOROMOで読める複製を作ります")
        _draw_status_box(layout, props)


_classes = (
    OminaeshiProperties,
    OMINAESHI_OT_auto_set,
    OMINAESHI_OT_md_listener_start,
    OMINAESHI_OT_md_listener_stop,
    OMINAESHI_OT_create_hou,
    OMINAESHI_PT_main,
)


def _auto_set_on_load() -> float | None:
    try:
        scene = bpy.context.scene
        if scene is not None and hasattr(scene, "ominaeshi"):
            _ensure_auto_inputs(scene.ominaeshi)
    except Exception:  # noqa: BLE001
        pass
    return None


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ominaeshi = PointerProperty(type=OminaeshiProperties)
    if not bpy.app.timers.is_registered(_auto_set_on_load):
        bpy.app.timers.register(_auto_set_on_load, first_interval=0.1)


def unregister():
    try:
        md_bridge.stop_listener()
    except Exception:  # noqa: BLE001
        pass
    if bpy.app.timers.is_registered(_auto_set_on_load):
        bpy.app.timers.unregister(_auto_set_on_load)
    del bpy.types.Scene.ominaeshi
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

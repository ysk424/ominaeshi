# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花 N パネル（日本語のみ）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import (
    Object,
    Operator,
    Panel,
    PropertyGroup,
)

from .i18n import msg
from .shell_isect_bridge import library_version
from .zozo_handoff import (
    ZOZO_MCP_PORT,
    ZozoHandoffError,
    estimate_body_export_band_cm,
    find_body_object,
    find_clothes_object,
    prepare_for_zozo,
)


_zozo_process: subprocess.Popen[str] | None = None
_zozo_scene_name: str | None = None
_zozo_prepared_summary: str | None = None
_ZOZO_CLIENT_FILENAME = "zozo_mcp_client.py"
_ZOZO_CONFIG_FILENAME = "zozo_mcp_config.json"


def _version() -> str:
    try:
        path = os.path.join(os.path.dirname(__file__), "blender_manifest.toml")
        with open(path, "rb") as f:
            return str(tomllib.load(f).get("version", "?"))
    except Exception:
        return "?"


def _wrap_status_lines(text: str, width: int = 52) -> list[str]:
    ready = msg("ready")
    raw = (text or "").strip() or ready
    lines: list[str] = []
    for paragraph in raw.replace("\r\n", "\n").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > width:
            cut = paragraph.rfind(" ", 0, width)
            if cut < width // 2:
                cut = width
            lines.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        if paragraph:
            lines.append(paragraph)
    return lines or [ready]


def _draw_status_box(layout, props) -> None:
    box = layout.box()
    header = box.row()
    header.label(text="メッセージ")
    col = box.column(align=True)
    col.scale_y = 1.05
    lines = _wrap_status_lines(props.parse_status, width=46)
    while len(lines) < 6:
        lines.append("")
    for line in lines[:14]:
        col.label(text=line if line else " ")


def _mesh_object_poll(_properties, obj: Object) -> bool:
    return obj.type == "MESH"


def _ominaeshi_data_dir() -> str:
    return bpy.utils.user_resource("DATAFILES", path="ominaeshi", create=True)


def _bundled_python() -> str:
    names = (
        ["python.exe"]
        if os.name == "nt"
        else [
            f"python{sys.version_info.major}.{sys.version_info.minor}",
            "python3",
            "python",
        ]
    )
    candidates = [Path(sys.prefix) / "bin" / name for name in names]
    executable = Path(sys.executable)
    if executable.name.lower().startswith("python"):
        candidates.append(executable)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Blender 同梱の Python が見つかりません。")


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    inherited_paths = [path for path in sys.path if isinstance(path, str) and path]
    existing = environment.get("PYTHONPATH")
    if existing:
        inherited_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(inherited_paths))
    return environment


def _set_zozo_status(message: str) -> None:
    if _zozo_scene_name:
        scene = bpy.data.scenes.get(_zozo_scene_name)
        if scene is not None and hasattr(scene, "ominaeshi"):
            scene.ominaeshi.parse_status = message


def _fix_windows_mojibake(text: str) -> str:
    if not text:
        return text

    def _kana_kanji(s: str) -> int:
        return sum(
            1 for c in s if "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff"
        )

    def _hiragana(s: str) -> int:
        return sum(1 for c in s if "\u3040" <= c <= "\u309f")

    candidates: list[tuple[int, str]] = []
    for enc_from, enc_to, weight in (
        ("latin-1", "utf-8", 100),
        ("cp1252", "utf-8", 100),
        ("latin-1", "cp932", 10),
        ("cp1252", "cp932", 10),
    ):
        try:
            fixed = text.encode(enc_from, errors="strict").decode(enc_to, errors="strict")
        except (UnicodeError, LookupError):
            continue
        if "\ufffd" in fixed or fixed == text:
            continue
        score = _kana_kanji(fixed) * weight + _hiragana(fixed) * 50
        if score > 0:
            candidates.append((score, fixed))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    if _kana_kanji(text) > 0:
        return text
    if "10061" in text:
        return (
            f"WinError 10061: 接続拒否 "
            f"(ZOZO MCP ポート {ZOZO_MCP_PORT} で何も待ち受けていません)"
        )
    return text


def _zozo_mcp_port_from_scene(scene, default: int = ZOZO_MCP_PORT) -> int:
    try:
        if hasattr(scene, "zozo_contact_solver"):
            return int(scene.zozo_contact_solver.state.mcp_port) or default
    except Exception:
        pass
    return default


def _ensure_zozo_mcp_server(
    port: int = ZOZO_MCP_PORT, wait_s: float = 3.0
) -> tuple[int, str]:
    """ZOZO の MCP HTTP サーバが無ければ起動する（縫製と同じ）。"""
    import socket
    import time

    def _port_open(p: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(p)), timeout=0.2):
                return True
        except OSError:
            return False

    if _port_open(port):
        return int(port), f"MCP は既に :{port} で稼働中"

    errors: list[str] = []
    try:
        if hasattr(bpy.ops, "mcp") and hasattr(bpy.ops.mcp, "start_server"):
            result = bpy.ops.mcp.start_server()
            if not (result == {"FINISHED"} or "FINISHED" in str(result)):
                errors.append(f"mcp.start_server -> {result}")
        else:
            errors.append("bpy.ops.mcp.start_server が使えません")
    except Exception as exc:
        errors.append(f"ops: {_fix_windows_mojibake(str(exc))}")

    actual = _zozo_mcp_port_from_scene(bpy.context.scene, port)

    if not _port_open(actual) and not _port_open(port):
        started = False
        for mod_name in (
            "bl_ext.user_default.ppf_contact_solver.mcp.mcp_server",
            "ppf_contact_solver.mcp.mcp_server",
        ):
            try:
                mod = __import__(
                    mod_name,
                    fromlist=["start_mcp_server", "is_mcp_running", "get_mcp_server"],
                )
                if not mod.is_mcp_running():
                    mod.start_mcp_server(int(port))
                server = mod.get_mcp_server()
                if server is not None and getattr(server, "port", None):
                    actual = int(server.port)
                started = True
                break
            except Exception as exc:
                errors.append(f"{mod_name}: {_fix_windows_mojibake(str(exc))}")
        if not started:
            errors.append("ZOZO MCP 開始 API が見つかりません（拡張は有効ですか？）")

    actual = _zozo_mcp_port_from_scene(bpy.context.scene, actual)
    deadline = time.time() + max(0.5, float(wait_s))
    while time.time() < deadline:
        if _port_open(actual):
            return actual, msg("mcp_started", port=actual)
        if actual != port and _port_open(port):
            return int(port), msg("mcp_started", port=port)
        time.sleep(0.1)

    detail = "; ".join(errors) if errors else "ポートが開きませんでした"
    raise RuntimeError(msg("mcp_start_fail", port=port, detail=detail))


def _poll_zozo_mcp() -> float | None:
    global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
    process = _zozo_process
    if process is None:
        return None
    if process.poll() is None:
        return 0.2

    stdout, stderr = process.communicate()
    summary = _zozo_prepared_summary or msg("prepared_default")
    try:
        lines = [line for line in stdout.splitlines() if line.strip()]
        result = json.loads(lines[-1]) if lines else {}
        if process.returncode != 0 or result.get("status") != "success":
            diagnostic = _fix_windows_mojibake(
                str(result.get("message") or stderr.strip() or "ZOZO MCP 設定に失敗しました。")
            )
            _set_zozo_status(
                msg("mcp_setup_failed", summary=summary, detail=diagnostic[:200])
            )
        else:
            capture = str(result.get("capture", "不要"))
            connection = str(result.get("connection", "")).strip()
            conn_note = f"; {connection}" if connection else ""
            _set_zozo_status(
                msg(
                    "mcp_ready",
                    summary=summary,
                    capture=capture,
                    conn=conn_note,
                )
            )
    except Exception as exc:
        diagnostic = _fix_windows_mojibake(
            stderr.strip() or stdout.strip() or str(exc)
        )
        _set_zozo_status(
            msg("mcp_response_failed", summary=summary, detail=diagnostic[:200])
        )
    finally:
        _zozo_process = None
        _zozo_scene_name = None
        _zozo_prepared_summary = None
    return None


def _apply_body_band_from_object(props, body: Object | None) -> None:
    if body is None:
        return
    ankle, neck = estimate_body_export_band_cm(body)
    props.body_export_z_min_cm = float(ankle)
    props.body_export_z_max_cm = float(neck)


def _ensure_auto_inputs(props) -> None:
    """未設定なら CC_Base_Body / output.001 を自動セットし、足首〜首を推定。"""
    changed = False
    if props.body_object is None:
        body = find_body_object()
        if body is not None:
            props.body_object = body
            _apply_body_band_from_object(props, body)
            changed = True
    if props.clothes_object is None:
        clothes = find_clothes_object()
        if clothes is not None:
            props.clothes_object = clothes
            changed = True
    if changed and props.body_object and props.clothes_object:
        ankle = float(props.body_export_z_min_cm)
        neck = float(props.body_export_z_max_cm)
        if (props.parse_status or "").strip() in ("", "準備完了", msg("ready")):
            props.parse_status = msg(
                "auto_set",
                clothes=props.clothes_object.name,
                body=props.body_object.name,
                ankle=ankle,
                neck=neck,
            )


def _on_body_update(self, context):
    """ボディ変更時に足首〜首の高さを再推定。"""
    try:
        locked = bool(self["body_band_user_locked"])
    except (KeyError, TypeError, AttributeError):
        locked = False
    if locked:
        return
    _apply_body_band_from_object(self, self.body_object)


class OminaeshiProperties(PropertyGroup):
    parse_status: StringProperty(
        name="状態",
        description="状態と警告（パネルのメッセージ欄のみ）",
        default="準備完了",
        options={"TEXTEDIT_UPDATE"},
    )
    clothes_object: PointerProperty(
        name="服",
        description="マーベラスデザイナーなどで作った服メッシュ",
        type=Object,
        poll=_mesh_object_poll,
    )
    body_object: PointerProperty(
        name="ボディ",
        description="衝突用ボディメッシュ（未指定時は CC_Base_Body を自動）",
        type=Object,
        poll=_mesh_object_poll,
        update=_on_body_update,
    )
    shell_isect_include_body: BoolProperty(
        name="ボディとの交差検査",
        description=(
            "オン（既定）: 布とボディの両方で交差検査（ZOZO と同じ組）。"
            "オフ: 布どうしだけ"
        ),
        default=True,
    )
    body_export_z_min_cm: FloatProperty(
        name="足首 (cm)",
        description=(
            "書き出す ZOZO ボディの世界 Z 下限（cm）。"
            "これより完全に下（足先など）は切り捨て"
        ),
        default=5.0,
        min=0.0,
        max=300.0,
        soft_min=0.0,
        soft_max=50.0,
        step=10,
        precision=1,
    )
    body_export_z_max_cm: FloatProperty(
        name="首 (cm)",
        description=(
            "書き出す ZOZO ボディの世界 Z 上限（cm）。"
            "これより完全に上（首から上の頭など）は切り捨て"
        ),
        default=145.0,
        min=0.0,
        max=300.0,
        soft_min=100.0,
        soft_max=200.0,
        step=10,
        precision=1,
    )


class OMINAESHI_OT_auto_set(Operator):
    bl_idname = "ominaeshi.auto_set"
    bl_label = "自動セット"
    bl_description = (
        "服を output.001 など、ボディを CC_Base_Body にセットし、"
        "足首〜首の書き出し高さをボディから推定します"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.ominaeshi
        body = find_body_object()
        clothes = find_clothes_object()
        if body is not None:
            # 強制再セット時はロック解除して推定し直す
            props["body_band_user_locked"] = False
            props.body_object = body
            _apply_body_band_from_object(props, body)
        if clothes is not None:
            props.clothes_object = clothes
        if body is None and clothes is None:
            props.parse_status = "服もボディも見つかりませんでした。"
            return {"CANCELLED"}
        props.parse_status = msg(
            "auto_set",
            clothes=props.clothes_object.name if props.clothes_object else "（なし）",
            body=props.body_object.name if props.body_object else "（なし）",
            ankle=float(props.body_export_z_min_cm),
            neck=float(props.body_export_z_max_cm),
        )
        return {"FINISHED"}


class OMINAESHI_OT_prepare_zozo(Operator):
    bl_idname = "ominaeshi.prepare_zozo"
    bl_label = "ZOZO用準備"
    bl_description = (
        "服コピー上に ZOZO 向け縫い辺を再構築し、ボディを足首〜首でカットしてコピー、"
        "自己交差の検査→修理→再検査と三角品質を確認し、"
        f"PASS なら ZOZO MCP（:{ZOZO_MCP_PORT}）を縫製と同じ内容で設定します"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def execute(self, context):
        global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
        props = context.scene.ominaeshi
        _ensure_auto_inputs(props)
        if _zozo_process is not None and _zozo_process.poll() is None:
            self.report({"WARNING"}, msg("prepare_mcp_running"))
            return {"CANCELLED"}
        try:
            prepared = prepare_for_zozo(
                context,
                props.clothes_object,
                props.body_object,
                shell_isect_include_body=bool(props.shell_isect_include_body),
                body_z_min_m=float(props.body_export_z_min_cm) * 0.01,
                body_z_max_m=float(props.body_export_z_max_cm) * 0.01,
            )
        except ZozoHandoffError as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            ver = library_version()
            suffix = (
                msg("shell_suffix", ver=ver) if ver else msg("shell_suffix_missing")
            )
            props.parse_status = msg(
                "prepare_stopped", message=message, suffix=suffix
            )
            return {"CANCELLED"}
        except Exception as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            ver = library_version()
            suffix = (
                msg("shell_suffix", ver=ver) if ver else msg("shell_suffix_missing")
            )
            props.parse_status = msg(
                "prepare_failed", message=message, suffix=suffix
            )
            return {"CANCELLED"}

        if prepared.abort_message:
            props.parse_status = msg(
                "prepare_stopped",
                message=prepared.abort_message,
                suffix="",
            )
            return {"CANCELLED"}

        shell_suffix = f" [{prepared.shell_isect.version_suffix()}]"
        quality_note = (
            f"; {prepared.quality.summary()}" if prepared.quality is not None else ""
        )
        summary = msg(
            "prepare_summary",
            stitch=prepared.stitch.summary_ja(),
            kept=prepared.body_faces_kept,
            total=prepared.body_faces_total,
            shell=shell_suffix,
            quality=quality_note,
        )
        try:
            mcp_port, mcp_note = _ensure_zozo_mcp_server(ZOZO_MCP_PORT)
            config = prepared.mcp_configuration(context.scene)
            config["port"] = int(mcp_port)
            config_path = Path(_ominaeshi_data_dir()) / _ZOZO_CONFIG_FILENAME
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            client_path = Path(__file__).with_name(_ZOZO_CLIENT_FILENAME)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            _zozo_process = subprocess.Popen(
                [_bundled_python(), str(client_path), str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=_subprocess_environment(),
            )
            _zozo_scene_name = context.scene.name
            _zozo_prepared_summary = summary
            props.parse_status = msg(
                "prepare_mcp_configuring",
                summary=summary,
                mcp_note=mcp_note,
                port=mcp_port,
            )
            if not bpy.app.timers.is_registered(_poll_zozo_mcp):
                bpy.app.timers.register(_poll_zozo_mcp, first_interval=0.2)
        except Exception as exc:
            message = _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)
            props.parse_status = msg(
                "prepare_mcp_start_fail",
                summary=summary,
                exc=message[:240],
            )
        return {"FINISHED"}


class OMINAESHI_PT_main(Panel):
    bl_idname = "OMINAESHI_PT_main"
    bl_label = "女郎花"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "女郎花"

    def draw(self, context):
        # 注意: draw 中に props を書き換えると N パネルが展開できないことがある。
        layout = self.layout
        props = context.scene.ominaeshi
        layout.label(text=f"女郎花 v{_version()}")
        layout.separator(factor=0.4)
        inputs = layout.column(align=True)
        inputs.label(text="入力")
        inputs.prop(props, "clothes_object", text="服")
        inputs.prop(props, "body_object", text="ボディ")
        inputs.operator(OMINAESHI_OT_auto_set.bl_idname, text="自動セット")
        if props.clothes_object is None or props.body_object is None:
            hint = inputs.box()
            hint.label(text="未設定なら「自動セット」を押してください")
        layout.separator(factor=0.4)
        actions = layout.column(align=True)
        body_band = actions.box()
        body_band.label(text="ボディ切り出し（足首〜首）")
        band_row = body_band.row(align=True)
        band_row.prop(props, "body_export_z_min_cm", text="足首")
        band_row.prop(props, "body_export_z_max_cm", text="首")
        actions.operator(OMINAESHI_OT_prepare_zozo.bl_idname, text="ZOZO用準備")
        actions.prop(props, "shell_isect_include_body", text="ボディとの交差検査")
        layout.separator(factor=0.5)
        _draw_status_box(layout, props)


_classes = (
    OminaeshiProperties,
    OMINAESHI_OT_auto_set,
    OMINAESHI_OT_prepare_zozo,
    OMINAESHI_PT_main,
)


def _auto_set_on_load() -> float | None:
    """登録直後に一度だけ自動セット（draw 外）。"""
    try:
        scene = bpy.context.scene
        if scene is not None and hasattr(scene, "ominaeshi"):
            _ensure_auto_inputs(scene.ominaeshi)
    except Exception:
        pass
    return None


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ominaeshi = PointerProperty(type=OminaeshiProperties)
    if not bpy.app.timers.is_registered(_auto_set_on_load):
        bpy.app.timers.register(_auto_set_on_load, first_interval=0.1)


def unregister():
    global _zozo_process, _zozo_scene_name, _zozo_prepared_summary
    if bpy.app.timers.is_registered(_auto_set_on_load):
        bpy.app.timers.unregister(_auto_set_on_load)
    if bpy.app.timers.is_registered(_poll_zozo_mcp):
        bpy.app.timers.unregister(_poll_zozo_mcp)
    if _zozo_process is not None and _zozo_process.poll() is None:
        _zozo_process.terminate()
    _zozo_process = None
    _zozo_scene_name = None
    _zozo_prepared_summary = None
    del bpy.types.Scene.ominaeshi
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)

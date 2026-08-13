# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花: MD の服 OBJ（frame 1）を Blender へ送る。tanabata 由来。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import traceback
from collections.abc import Mapping, Sequence

try:
    _DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _DIR = r"C:\Users\azoo\git\ominaeshi\md_addon"
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import importlib
import _bridge

importlib.reload(_bridge)
from _bridge import TEMP_DIR, bridge_call, listener_missing, log, notify

TAG = "send-clothes"
TIMEOUT = 120.0
EXPORT_FRAME = 1.0
USE_SILENT_OBJ = False

SAFE_METADATA_CALLS = [
    "GetSeamlinePairGroupCount",
    "GetAllStitchProperty",
    "GetPatternCount",
    "GetFabricCount",
    "GetCurrentAnimationFrame",
    "GetVersion",
]


def _garment_obj_path() -> str:
    return os.path.join(TEMP_DIR, "garment.obj")


def _garment_info_path() -> str:
    return os.path.join(TEMP_DIR, "garment_info.json")


def _pattern_json_path() -> str:
    return os.path.join(TEMP_DIR, "pattern_export.json")


def _make_export_option(**fields):
    try:
        import ApiTypes

        opt = ApiTypes.ImportExportOption()
    except Exception as exc:  # noqa: BLE001
        log(f"ImportExportOption なし ({exc}); ダイアログにフォールバック", tag=TAG)
        return None
    for key, value in fields.items():
        try:
            setattr(opt, key, value)
        except Exception as exc:  # noqa: BLE001
            log(f"WARN option {key}={value!r}: {exc}", tag=TAG)
    return opt


def _to_jsonable(value, depth: int = 0, seen: set[int] | None = None):
    if seen is None:
        seen = set()
    if depth > 4:
        return {"__truncated__": repr(type(value))}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v, depth + 1, seen) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_to_jsonable(v, depth + 1, seen) for v in list(value)[:500]]
    obj_id = id(value)
    if obj_id in seen:
        return {"__cycle__": repr(type(value))}
    seen.add(obj_id)
    attrs = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if isinstance(attr, (bool, int, float, str, list, tuple, dict)):
            attrs[name] = _to_jsonable(attr, depth + 1, seen)
    return {
        "__type__": f"{type(value).__module__}.{type(value).__name__}",
        "repr": repr(value),
        "attributes": attrs,
    }


def _call_metadata(func, args=()):
    try:
        return {"ok": True, "value": _to_jsonable(func(*args))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}


def _metadata_calls(modules: dict):
    rows = []
    for func_name in SAFE_METADATA_CALLS:
        found = False
        for module_name, module in modules.items():
            try:
                func = getattr(module, func_name)
            except Exception:
                continue
            if not callable(func):
                continue
            found = True
            rows.append(
                {
                    "module": module_name,
                    "function": func_name,
                    "args": [],
                    "result": _call_metadata(func),
                }
            )
        if not found:
            rows.append({"function": func_name, "found": False})
    return rows


def _export_garment_metadata() -> dict:
    os.makedirs(TEMP_DIR, exist_ok=True)
    garment_info = _garment_info_path()
    pattern_json = _pattern_json_path()

    modules = {}
    for name in (
        "utility_api",
        "pattern_api",
        "fabric_api",
        "garment_api",
        "sewing_api",
        "stitch_api",
    ):
        try:
            modules[name] = __import__(name)
        except Exception as exc:  # noqa: BLE001
            log(f"WARN metadata module {name} unavailable: {exc}", tag=TAG)

    pattern_export = {"path": pattern_json, "exists": False, "calls": []}
    for module_name, module in modules.items():
        func = getattr(module, "ExportPatternJSON", None)
        if not callable(func):
            continue
        result = _call_metadata(func, (pattern_json,))
        pattern_export["calls"].append(
            {
                "module": module_name,
                "function": "ExportPatternJSON",
                "args": [pattern_json],
                "result": result,
            }
        )
        break
    pattern_export["exists"] = os.path.exists(pattern_json)
    if pattern_export["exists"]:
        try:
            pattern_export["size"] = os.path.getsize(pattern_json)
        except OSError:
            pass

    info = {
        "schema": "ominaeshi.garment_transfer.v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "garment_obj_path": _garment_obj_path(),
        "pattern_json_path": pattern_json,
        "safe_calls": _metadata_calls(modules),
        "pattern_json_export": pattern_export,
        "notes": ["Written by ominaeshi 2_send_clothes_BL."],
    }
    with open(garment_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2, sort_keys=True)
    log(
        f"wrote garment metadata: {garment_info}; "
        f"pattern_json={pattern_export['exists']}",
        tag=TAG,
    )
    return {
        "garment_info_path": garment_info,
        "pattern_json_path": pattern_json if pattern_export["exists"] else "",
        "pattern_json_exists": pattern_export["exists"],
    }


def _find_obj(ret):
    if isinstance(ret, str):
        return ret if ret.lower().endswith(".obj") else None
    if isinstance(ret, (list, tuple)):
        objs = [p for p in ret if isinstance(p, str) and p.lower().endswith(".obj")]
        if objs:
            return objs[0]
        strs = [p for p in ret if isinstance(p, str)]
        return strs[0] if strs else None
    return None


def _export_obj():
    import export_api

    os.makedirs(TEMP_DIR, exist_ok=True)
    garment_obj = _garment_obj_path()
    opt = (
        _make_export_option(
            bExportGarment=True,
            bExportAvatar=False,
            bThin=False,
            scale=1.0,
        )
        if USE_SILENT_OBJ
        else None
    )
    if opt is not None:
        try:
            ret = export_api.ExportOBJW(garment_obj, opt)
            log(f"ExportOBJW(silent) returned: {ret!r}", tag=TAG)
            return _find_obj(ret) or (
                garment_obj if os.path.exists(garment_obj) else None
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ExportOBJW failed ({exc}); dialog へ", tag=TAG)

    log(
        "Export OBJ ダイアログ: GARMENT ONLY / THICK / save texture file paths",
        tag=TAG,
    )
    ret = export_api.ExportOBJ()
    log(f"ExportOBJ(dialog) returned: {ret!r}", tag=TAG)
    return _find_obj(ret)


def _goto_frame_one(utility_api) -> bool:
    before = None
    try:
        before = utility_api.GetCurrentAnimationFrame()
    except Exception:
        pass
    try:
        utility_api.SetCurrentAnimationFrame(EXPORT_FRAME)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN SetCurrentAnimationFrame({EXPORT_FRAME:g}) failed: {exc}", tag=TAG)
        return False
    after = None
    try:
        after = utility_api.GetCurrentAnimationFrame()
    except Exception:
        pass
    log(f"frame move: before={before} -> set {EXPORT_FRAME:g} -> after={after}", tag=TAG)
    if after is not None and abs(float(after) - EXPORT_FRAME) > 0.5:
        notify(
            f"WARNING: MD が frame {EXPORT_FRAME:g} に動きません（現在 {after}）。\n"
            "再生ヘッドを frame 1 にしてから再実行してください。",
            tag=TAG,
        )
        return False
    return True


def run() -> None:
    import utility_api

    _goto_frame_one(utility_api)

    try:
        obj_path = _export_obj()
    except Exception as exc:  # noqa: BLE001
        notify(f"OBJ 出力失敗: {exc}", tag=TAG)
        log(traceback.format_exc(), tag=TAG)
        return

    if not obj_path or not os.path.exists(obj_path):
        notify(
            "OBJ パスが返りませんでした（ダイアログ取消し、または想定外の戻り値）: "
            f"{obj_path!r}",
            tag=TAG,
        )
        return

    try:
        metadata = _export_garment_metadata()
    except Exception as exc:  # noqa: BLE001
        metadata = {"error": repr(exc)}
        log("WARN garment metadata export failed:\n" + traceback.format_exc(), tag=TAG)

    if os.path.normpath(os.path.dirname(obj_path)) != os.path.normpath(TEMP_DIR):
        try:
            os.makedirs(TEMP_DIR, exist_ok=True)
            temp_obj = os.path.join(TEMP_DIR, os.path.basename(obj_path))
            shutil.copy2(obj_path, temp_obj)
            src_mtl = os.path.splitext(obj_path)[0] + ".mtl"
            if os.path.exists(src_mtl):
                shutil.copy2(src_mtl, os.path.splitext(temp_obj)[0] + ".mtl")
            obj_path = temp_obj
            log(f"copied garment OBJ+MTL to {obj_path}", tag=TAG)
        except OSError as exc:
            log(f"WARN could not copy to temp, using original path: {exc}", tag=TAG)

    log(f"sending garment OBJ to Blender: {obj_path}", tag=TAG)
    try:
        result = bridge_call(
            "import_garment_obj",
            {
                "obj_path": obj_path,
                "garment_info_path": metadata.get("garment_info_path", ""),
                "pattern_json_path": metadata.get("pattern_json_path", ""),
            },
            timeout=TIMEOUT,
        )
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        notify(listener_missing(exc), tag=TAG)
        return
    except Exception as exc:  # noqa: BLE001
        notify(f"Blender import_garment_obj 失敗: {exc}", tag=TAG)
        log(traceback.format_exc(), tag=TAG)
        return

    notify(
        "服を Blender へ送りました。\n"
        f"OBJ: {obj_path}\n"
        f"meshes: {result.get('meshes')}\n"
        f"materials: {result.get('materials')}\n"
        f"seam metadata: {result.get('garment_info_path') or 'none'}\n"
        f"sewing: {result.get('sewing')}\n"
        f"textures packed: {result.get('packed')}; temp freed: {result.get('temp_freed')}",
        tag=TAG,
    )


run()

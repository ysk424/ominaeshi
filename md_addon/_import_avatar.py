# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender ボディ（frame 1）を MD アバターとして取り込む。tanabata 由来。"""

from __future__ import annotations

import os
import socket
import time
import traceback

from _bridge import TEMP_DIR, bridge_call, log, notify


TAG = "get-avatar"


def _wait_for_sentinel(path: str, timeout: float = 1800.0, poll: float = 0.5):
    deadline = time.time() + timeout
    waited = 0.0
    while time.time() < deadline:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    import json

                    data = json.load(f)
            except (OSError, ValueError):
                time.sleep(poll)
                continue
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "Blender の ABC 出力に失敗"))
            return data
        time.sleep(poll)
        waited += poll
        if waited % 10 < poll:
            log(f"  ...Blender 出力待ち ({int(waited)}s)", tag=TAG)
    raise TimeoutError(f"{timeout:g}s 待っても {path} が来ません")


def _cleanup(*paths):
    removed = []
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
                log(f"一時ファイル削除: {os.path.basename(path)}", tag=TAG)
        except OSError as exc:
            log(f"削除できず {path}: {exc}", tag=TAG)
    return removed


def _set_timeline_before(blender_frames: int, blender_fps: float) -> None:
    try:
        import utility_api

        fps = blender_fps if blender_fps and blender_fps > 0 else 24.0
        duration_sec = blender_frames / fps
        pre_end = max(duration_sec * 60.0 + 1000.0, 1000.0)
        utility_api.SetStartAnimationFrame(0.0)
        utility_api.SetEndAnimationFrame(pre_end)
        log(f"MD タイムラインを取り込み前に 0..{pre_end:g} へ", tag=TAG)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN タイムライン事前設定失敗: {exc}", tag=TAG)


def _set_timeline_after():
    try:
        import utility_api

        total = float(utility_api.GetTotalEndAnimationFrame())
        if total and total > 0 and total != float("inf"):
            utility_api.SetStartAnimationFrame(0.0)
            utility_api.SetEndAnimationFrame(total)
            try:
                utility_api.SetCurrentAnimationFrame(0.0)
            except Exception:  # noqa: BLE001
                pass
            log(f"MD タイムラインを 0..{total:g} に合わせた", tag=TAG)
            return total
        log(f"WARN GetTotalEndAnimationFrame={total!r}", tag=TAG)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN タイムライン事後設定失敗: {exc}", tag=TAG)
    return None


def run() -> None:
    log(f"Blender に export_body_abc (frame1) を要求 ... temp={TEMP_DIR}", tag=TAG)
    try:
        accepted = bridge_call("export_body_abc", {"single_frame": True})
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        from _bridge import listener_missing

        notify(listener_missing(exc), tag=TAG)
        return
    except Exception as exc:  # noqa: BLE001
        notify(f"Blender との通信に失敗: {exc}", tag=TAG)
        log(traceback.format_exc(), tag=TAG)
        return

    sentinel = accepted.get("sentinel")
    log(
        f"受け付け: object={accepted.get('object')} "
        f"frames {accepted.get('frame_start')}..{accepted.get('frame_end')}",
        tag=TAG,
    )
    try:
        meta = _wait_for_sentinel(sentinel)
    except TimeoutError as exc:
        notify(f"Blender の ABC 出力が終わりませんでした:\n{exc}", tag=TAG)
        return
    except RuntimeError as exc:
        notify(f"Blender の ABC 出力に失敗:\n{exc}", tag=TAG)
        return

    abc = meta.get("abc_path")
    log(f"出力完了: {abc}", tag=TAG)
    if not abc or not os.path.exists(abc):
        notify(f"ABC パスが存在しません:\n{abc}", tag=TAG)
        return

    try:
        blender_frames = (
            int(meta.get("frame_end", 0)) - int(meta.get("frame_start", 0)) + 1
        )
    except (TypeError, ValueError):
        blender_frames = 1
    try:
        blender_fps = float(meta.get("fps") or 24.0)
    except (TypeError, ValueError):
        blender_fps = 24.0
    if blender_frames > 0:
        _set_timeline_before(blender_frames, blender_fps)

    try:
        import import_api

        ok = import_api.ImportFile(abc)
    except Exception as exc:  # noqa: BLE001
        notify(f"MD ImportFile 失敗: {exc}", tag=TAG)
        log(traceback.format_exc(), tag=TAG)
        return

    md_end = _set_timeline_after()
    removed = _cleanup(abc, sentinel) if ok else []
    msg = (
        f"アバター取り込み完了: ImportFile -> {ok}\n"
        f"object '{meta.get('object')}' "
        f"frames {meta.get('frame_start')}..{meta.get('frame_end')} "
        f"@ {meta.get('fps')}fps -> MD 0..{md_end if md_end is not None else '?'}."
    )
    if removed:
        msg += f"\n一時ファイル {len(removed)} 件を削除。"
    notify(msg, tag=TAG)

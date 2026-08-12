# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花のユーザー向けメッセージ（日本語のみ・私用）。"""

from __future__ import annotations

_STATUS: dict[str, str] = {
    "ready": "準備完了",
    "auto_set": "自動セット: 服={clothes} / ボディ={body}（足首 {ankle:.0f} cm〜首 {neck:.0f} cm）",
    "prepare_mcp_running": "ZOZO MCP の設定がすでに実行中です。",
    "prepare_stopped": "ZOZO用準備 中断: {message}{suffix}",
    "prepare_failed": "ZOZO用準備 失敗: {message}{suffix}",
    "prepare_summary": (
        "{stitch}; ボディ面 {kept}/{total}; ZOZO 用コピー完了{shell}{quality}"
    ),
    "prepare_mcp_configuring": "{summary}; {mcp_note}; ZOZO MCP を :{port} で設定中...",
    "prepare_mcp_start_fail": (
        "{summary}; コピーはできましたが MCP を開始できませんでした: {exc}"
    ),
    "mcp_started": "MCP を :{port} で開始しました",
    "mcp_start_fail": (
        "ZOZO MCP を :{port} で開始できませんでした ({detail})。"
        "ZOZO Contact Solver を有効にし MCP Start してから、もう一度準備してください。"
    ),
    "mcp_setup_failed": "{summary}; ZOZO MCP 設定失敗: {detail}",
    "mcp_ready": (
        "{summary}; ZOZO MCP 準備完了 ({capture}){conn}。"
        "Transfer のあと Run Simulation を実行してください。"
    ),
    "mcp_response_failed": "{summary}; ZOZO MCP 応答失敗: {detail}",
    "prepared_default": "ZOZO 引き渡しメッシュを準備しました",
    "shell_unavailable": "交差検査 利用不可",
    "shell_suffix": " [交差検査 {ver}]",
    "shell_suffix_missing": " [交差検査 利用不可]",
    "shell_err_unavailable": (
        "エラー: 自己交差チェックを利用できません ({message}) [{suffix}]"
    ),
    "shell_err_failed": (
        "エラー: 自己交差チェックに失敗しました ({message}) [{suffix}]"
    ),
    "shell_err_pairs": (
        "エラー: 自己交差 (三角×三角の面ペア): {pipeline}"
        "{faces}{pairs} [{suffix}]"
    ),
    "shell_faces_range": " 布面=0..{last}",
    "shell_face_pairs": " 面ペア: {pairs}",
    "shell_mode_both": "布+ボディ",
    "shell_mode_cloth": "布のみ",
    "shell_summary": "交差検査 {version} ({mode}{crop}): {pipeline}",
    "shell_crop": ", ボディ {tested}/{total} 三角",
    "shell_pipeline_clean": "検査1=0 (クリーン; 修理スキップ)",
    "shell_pipeline": "検査1={before} 修理={fix} 検査2={after}",
    "quality_summary": (
        "三角品質: {faces} 面, 最小面積 "
        "{area_min:.2e} m² (下限 {floor:.2e}), "
        "最短辺 {edge_mm:.3f} mm, "
        "最悪アスペクト {aspect:.2e}, "
        "下限未満 {failing}"
    ),
    "quality_error": (
        "エラー: ソルバに渡せないほど面積が小さい三角が {failing} 枚あります。"
        "最小 {area_min:.2e} m² (下限 {floor:.2e} m²)。"
        "シェル要素の剛性は 1/面積 に比例するため、これらは最初の求解で NaN になり"
        "フレーム 0 で止まります"
    ),
    "quality_worst": "。最悪: {shown}",
    "quality_worst_more": ", ... (他 {n} 件)",
    "quality_worst_item": (
        "(面 {index}: 面積 {area:.2e} m², 最短辺 {edge_mm:.4f} mm)"
    ),
    "zozo_need_clothes": "先に服（メッシュ）を指定してください。",
    "zozo_need_body": "ZOZO用準備の前にメッシュのボディを指定してください。",
    "zozo_nonfinite": "布に有限でない頂点座標があります。",
    "zozo_topo_changed": "ZOZO 引き渡しメッシュ作成中にトポロジが変わりました。",
    "zozo_no_body_export": "ZOZO ボディが書き出されていないため MCP を設定できません。",
    "zozo_cloth_no_tris": "服に三角形がありません。",
    "zozo_body_no_tris": "ボディに三角形がありません。",
    "zozo_same_object": "服とボディに同じオブジェクトは使えません。",
    "md_listener_fail": "MD ブリッジ開始失敗: {message}",
}


def msg(key: str, **kwargs) -> str:
    """ユーザー向け状態・エラー文字列を返す（日本語のみ）。"""
    template = _STATUS.get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template

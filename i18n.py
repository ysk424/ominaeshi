# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花のユーザー向けメッセージ（日本語）。"""

from __future__ import annotations

_STATUS: dict[str, str] = {
    "ready": "準備完了",
    "auto_set": "自動セット: 服={clothes} / ボディ={body}",
    "auto_set_none": "服もボディも見つかりませんでした。",
    "md_listener_fail": "MD ブリッジ開始失敗: {message}",
    "hou_need_clothes": "先にMDから取り込んだ服OBJを指定してください。",
    "hou_working": "HOU化しています…",
    "hou_failed": "HOU化 失敗: {message}",
    "hou_done": (
        "HOU化 完了: {collection} / パーツ {parts} / 縫い線 {labels} / 頂点ペア {pairs}"
    ),
}


def msg(key: str, **kwargs) -> str:
    template = _STATUS.get(key) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:  # noqa: BLE001
            return template
    return template

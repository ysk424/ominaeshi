# SPDX-License-Identifier: GPL-3.0-or-later
"""女郎花: Blender のボディ（frame 1）を MD アバターとして取り込む。

MD: Plugins ▸ Plug-in Manager でこのファイルを登録。
Blender: 女郎花パネルでボディ指定 → MD ブリッジ開始。
"""
from __future__ import annotations

import os
import sys

try:
    _DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _DIR = r"C:\Users\azoo\git\ominaeshi\md_addon"
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import importlib
import _bridge
import _import_avatar

importlib.reload(_bridge)
importlib.reload(_import_avatar)

_import_avatar.run()

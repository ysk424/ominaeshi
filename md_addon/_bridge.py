# SPDX-License-Identifier: GPL-3.0-or-later
"""MD プラグイン共通（TCP / ログ / 通知）。tanabata md_addon 由来。"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time

HOST = "127.0.0.1"
PORT = 7422
TEMP_DIR = os.path.join(tempfile.gettempdir(), "tanabata")
LOG_PATH = os.path.join(os.path.expanduser("~"), "ominaeshi_md.log")
APP_NAME = "女郎花"


def log(msg: str, tag: str = "md") -> None:
    line = f"{time.strftime('%H:%M:%S')} [{tag}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(msg: str, tag: str = "md") -> None:
    log(msg, tag=tag)
    try:
        import utility_api

        for args in ((msg,), (APP_NAME, msg)):
            try:
                utility_api.DisplayMessageBox(*args)
                return
            except Exception:
                continue
    except Exception:
        pass


def listener_missing(exc: BaseException) -> str:
    return (
        "Blender の女郎花リスナー（127.0.0.1:7422）に繋がりません。\n"
        "Blender の N パネル「女郎花」でボディを指定し、"
        "「MD ブリッジ → 開始」を押してから再実行してください。\n"
        f"({type(exc).__name__}: {exc})"
    )


def bridge_call(method: str, params: dict | None = None, timeout: float = 300.0):
    req = {"id": 1, "method": method, "params": params or {}}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((HOST, PORT))
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not buf:
        raise RuntimeError("Blender リスナーから空の応答")
    resp = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
    if resp.get("error"):
        raise RuntimeError(f"Blender 側エラー: {resp['error']}")
    return resp.get("result")

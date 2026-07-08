#!/usr/bin/env python3
"""OBS 配信レイアウトの土台を整える。

配信画面（1920x1080）の構成:
- デスクトップ（画面キャプチャ）を **左上に縮小配置**（kai はアプリを全画面で使うので、
  縮小は OBS 側で行う）。**サイズ・位置は OBS 上で手動調整する**（このスクリプトは
  既定では触らない。下帯・右余白の広さは好みで変わるため）
- **下**に字幕エリア（字幕オーバーレイを最前面に置く。字幕は overlay ページ側で
  キャンバス下部の帯に出る。style.css の --subtitle-band-h を下帯に合わせる）
- **右・下**の余白は暗い背景ソース（kai-bg）で埋める（配信で活用予定）

このスクリプトの役割: **背景（kai-bg）を最背面に用意し、字幕オーバーレイを最前面にする**
土台づくり。デスクトップのサイズ変更は OBS 上で行う（--desktop で初期値を入れることも可）。

使い方（VM 上、OBS 起動中）:
  python3 obs-layout.py            # 背景 + 字幕の重ね順を整える（デスクトップは触らない）
  python3 obs-layout.py --desktop  # デスクトップを既定値で左上に縮小（初期セット用）
  python3 obs-layout.py --reset    # デスクトップを全面に戻す
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import obsws  # noqa: E402  connect() を再利用

SCENE = "シーン"
CANVAS_W, CANVAS_H = 1920, 1080
BG_NAME = "kai-bg"
BG_RGB = (24, 24, 38)  # 暗いスレート

# デスクトップ配置（左上・縮小）。下に字幕帯、右に余白を残す。
DESK_X, DESK_Y = 16, 12
DESK_SCALE = 0.775  # 1920*0.775=1488, 1080*0.775=837 → 右416px・下231px の余白


def _req(ws, rtype, data=None):
    ws.send(json.dumps({"op": 6, "d": {"requestType": rtype, "requestId": "r",
                                       "requestData": data or {}}}))
    while True:
        m = json.loads(ws.recv())
        if m["op"] == 7 and m["d"]["requestId"] == "r":
            return m["d"]["requestStatus"], (m["d"].get("responseData") or {})


def _items(ws):
    _st, rd = _req(ws, "GetSceneItemList", {"sceneName": SCENE})
    return {it["sourceName"]: it for it in rd.get("sceneItems", [])}


def _set_transform(ws, item_id, x, y, sx, sy):
    _req(ws, "SetSceneItemTransform", {
        "sceneName": SCENE, "sceneItemId": item_id,
        "sceneItemTransform": {
            "positionX": x, "positionY": y, "scaleX": sx, "scaleY": sy,
            "boundsType": "OBS_BOUNDS_NONE", "cropLeft": 0, "cropRight": 0,
            "cropTop": 0, "cropBottom": 0, "rotation": 0,
        },
    })


def _find_desktop(items):
    for it in items.values():
        if it.get("inputKind") == "xshm_input" or "キャプチャ" in it["sourceName"]:
            return it
    return None


def apply_layout(ws, scale_desktop=False):
    items = _items(ws)
    # 1) 背景 color source（無ければ作る）
    if BG_NAME not in items:
        r, g, b = BG_RGB
        color = (0xFF << 24) | (b << 16) | (g << 8) | r
        _req(ws, "CreateInput", {
            "sceneName": SCENE, "inputName": BG_NAME, "inputKind": "color_source_v3",
            "inputSettings": {"color": color, "width": CANVAS_W, "height": CANVAS_H},
        })
        items = _items(ws)
    # 背景を最背面・全面
    bg = items[BG_NAME]
    _req(ws, "SetSceneItemIndex",
         {"sceneName": SCENE, "sceneItemId": bg["sceneItemId"], "sceneItemIndex": 0})
    _set_transform(ws, bg["sceneItemId"], 0, 0, 1.0, 1.0)
    # 2) デスクトップを左上に縮小（--desktop の時だけ。通常は OBS 上で手動調整するので触らない）
    if scale_desktop:
        desk = _find_desktop(items)
        if desk:
            _set_transform(ws, desk["sceneItemId"], DESK_X, DESK_Y, DESK_SCALE, DESK_SCALE)
    # 3) 字幕オーバーレイを最前面・全面
    ov = items.get("kai-overlay-browser")
    if ov:
        top = len(items) - 1
        _req(ws, "SetSceneItemIndex",
             {"sceneName": SCENE, "sceneItemId": ov["sceneItemId"], "sceneItemIndex": top})
        _set_transform(ws, ov["sceneItemId"], 0, 0, 1.0, 1.0)


def reset_layout(ws):
    items = _items(ws)
    desk = _find_desktop(items)
    if desk:
        _set_transform(ws, desk["sceneItemId"], 0, 0, 1.0, 1.0)


def main():
    ws = obsws.connect()
    try:
        if "--reset" in sys.argv:
            reset_layout(ws)
            print("layout reset（デスクトップ全面）")
        else:
            apply_layout(ws, scale_desktop="--desktop" in sys.argv)
            print("layout applied（背景 + 字幕を整えた"
                  + ("・デスクトップも左上に縮小" if "--desktop" in sys.argv else "") + "）")
    finally:
        ws.close()


if __name__ == "__main__":
    main()

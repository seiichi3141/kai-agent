#!/usr/bin/env python3
"""kai 配信オーバーレイの表示ウィンドウ（WebKitGTK ラッパー）。

index.html（字幕オーバーレイ）を、X11 デスクトップに
  - 背景透過（RGBA visual + WebView の透明背景）
  - 枠なし（decorated=False）・最前面（keep_above）・タスクバー非表示
  - クリックスルー（入力シェイプを空にする = マウスイベントが下のウィンドウへ抜ける）
で全画面に重ねて表示する。デスクトップ上の見た目そのままが OBS の
画面キャプチャ（XSHM）に乗る。

なぜ Chromium ではないのか: snap 版 Chromium は `--enable-transparent-visuals`
`--default-background-color=00000000` を与えても ARGB ウィンドウの背景が
白く塗られ透過しない（GNOME/Mutter + X11、GPU 有効/無効とも実機で確認）。
WebKitGTK は GTK の RGBA visual に素直に従い、クリックスルー（XShape の
入力領域制御）も GTK API で確実に設定できるため、こちらを正典とする。

使い方:
  DISPLAY=:0 python3 overlay-window.py               # 同じディレクトリの index.html を表示
  OVERLAY_URL=file:///path/to/index.html?sse=... \
    DISPLAY=:0 python3 overlay-window.py             # URL を上書き

依存（Ubuntu）: python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
設計: docs/kai/design/00-system.md §4。SSE の供給元は kai-services/speechd/。
"""

from __future__ import annotations

import os
import pathlib
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
import cairo  # noqa: E402  (gi.require_version の後に import する慣習)
from gi.repository import Gdk, Gtk, WebKit2  # noqa: E402

WIDTH = int(os.environ.get("OVERLAY_WIDTH", "1920"))
HEIGHT = int(os.environ.get("OVERLAY_HEIGHT", "1080"))


def default_url() -> str:
    html = pathlib.Path(__file__).resolve().parent / "index.html"
    return html.as_uri()


def main() -> int:
    url = os.environ.get("OVERLAY_URL") or default_url()

    win = Gtk.Window(title="kai overlay")
    win.set_decorated(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_above(True)
    win.set_accept_focus(False)
    win.set_app_paintable(True)
    win.set_default_size(WIDTH, HEIGHT)
    win.move(0, 0)

    # RGBA visual（コンポジタ有効時のみ得られる。無い場合は透過なしで続行）
    screen = win.get_screen()
    rgba = screen.get_rgba_visual()
    if rgba is not None:
        win.set_visual(rgba)
    else:
        print("[kai-overlay] WARN: RGBA visual なし（コンポジタ無効?）。不透明で表示します", file=sys.stderr)

    webview = WebKit2.WebView()
    settings = webview.get_settings()
    # file:// で開いた index.html から http://127.0.0.1:8900/events(SSE) へ
    # 接続するため、file オリジンからのアクセス制限を緩める（ローカル専用ページ）
    settings.set_allow_file_access_from_file_urls(True)
    settings.set_allow_universal_access_from_file_urls(True)
    # VM は GPU なし（llvmpipe）。GL 合成を避けて安定させる
    settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
    webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))
    win.add(webview)

    def on_realize(widget: Gtk.Widget) -> None:
        gdk_win = widget.get_window()
        if gdk_win is not None:
            # 入力シェイプを空にする = クリックスルー
            # （字幕は表示だけ。マウス/クリックは下のデスクトップに抜ける）
            gdk_win.input_shape_combine_region(cairo.Region(), 0, 0)

    win.connect("realize", on_realize)
    win.connect("destroy", Gtk.main_quit)

    webview.load_uri(url)
    win.show_all()
    print(f"[kai-overlay] showing {url} ({WIDTH}x{HEIGHT}, click-through)")

    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

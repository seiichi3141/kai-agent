#!/usr/bin/env python3
"""stream-browser — 配信画面に見えるブラウザを kai が操作するための CLI（Issue #11）。

kai（hermes）の browser_* ツールは headless（配信に映らない）。本 CLI は
デスクトップ上に headed Chromium を 1 枚起動し、CDP（Chrome DevTools Protocol）
でナビゲート・スクロールする。ブラウザは OBS の画面キャプチャに乗るので、
GitHub Issue や PR を「視聴者に見せながら」確認できる。

kai はターミナルからこの CLI を呼ぶ（コマンドは kai_director のコマンドログにも
映る）。使い方:
  stream-browser.py open <url>     # 起動（未起動なら）してURLを開く
  stream-browser.py scroll [down|up|top|bottom]  # スクロール（既定 down）
  stream-browser.py back           # 戻る
  stream-browser.py status         # 起動状態と現在URL
  stream-browser.py close          # ブラウザを閉じる

依存: chromium（snap）と python3-websocket（obsws.py と同じ）。CDP のみ使用。

配信運用: --remote-debugging-port=9222 で待ち受け、翻訳ポップアップ・初回
ダイアログ・更新通知を抑止するフラグで起動する（配信画面をきれいに保つ）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket  # python3-websocket（obsws.py と同じ）

CDP_PORT = int(os.environ.get("STREAM_BROWSER_PORT", "9222"))
CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"
UNIT = "kai-stream-browser"
DISPLAY = os.environ.get("DISPLAY", ":0")

# 配信向け起動フラグ: 翻訳バー・初回実行・既定ブラウザ確認・更新通知・
# インフォバー・パスワード保存を抑止し、配信画面をきれいに保つ。
CHROMIUM_FLAGS = [
    f"--remote-debugging-port={CDP_PORT}",
    "--remote-allow-origins=*",
    "--disable-gpu",  # llvmpipe（GPU なし）。PoC で静的ページは CPU ほぼ 0
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,TranslateUI",
    "--disable-translate",
    "--disable-infobars",
    "--password-store=basic",
    "--start-maximized",
]


def _cdp_targets() -> list[dict]:
    with urllib.request.urlopen(f"{CDP_HTTP}/json", timeout=3) as r:
        return json.loads(r.read())


def _browser_running() -> bool:
    try:
        _cdp_targets()
        return True
    except Exception:
        return False


def _page_target() -> dict | None:
    for t in _cdp_targets():
        if t.get("type") == "page":
            return t
    return None


class _CDP:
    """1 ページに接続する最小 CDP クライアント（同期・1 コマンド 1 応答）。"""

    def __init__(self, ws_url: str) -> None:
        self._ws = websocket.create_connection(ws_url, timeout=10)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == self._id:
                return msg

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _connect_page() -> _CDP:
    page = _page_target()
    if not page or "webSocketDebuggerUrl" not in page:
        raise RuntimeError("ブラウザのページが見つかりません（先に open してください）")
    return _CDP(page["webSocketDebuggerUrl"])


def _launch() -> None:
    subprocess.run(["systemctl", "--user", "reset-failed", UNIT],
                   check=False, capture_output=True)
    subprocess.run(
        ["systemd-run", "--user", f"--unit={UNIT}", "--collect",
         f"--setenv=DISPLAY={DISPLAY}", "chromium", *CHROMIUM_FLAGS,
         "--new-window", "about:blank"],
        check=True, capture_output=True,
    )
    # CDP が応答するまで待つ（最大 30 秒）
    for _ in range(60):
        if _browser_running():
            return
        time.sleep(0.5)
    raise RuntimeError("ブラウザ起動後 CDP に接続できませんでした")


def cmd_open(url: str) -> None:
    if not url:
        print("使い方: stream-browser.py open <url>", file=sys.stderr)
        sys.exit(2)
    if not _browser_running():
        _launch()
    cdp = _connect_page()
    try:
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": url})
        print(f"開いた: {url}")
    finally:
        cdp.close()


def cmd_scroll(direction: str) -> None:
    exprs = {
        "down": "window.scrollBy({top: window.innerHeight*0.85, behavior: 'smooth'})",
        "up": "window.scrollBy({top: -window.innerHeight*0.85, behavior: 'smooth'})",
        "top": "window.scrollTo({top: 0, behavior: 'smooth'})",
        "bottom": "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})",
    }
    expr = exprs.get(direction or "down")
    if expr is None:
        print("使い方: stream-browser.py scroll [down|up|top|bottom]", file=sys.stderr)
        sys.exit(2)
    cdp = _connect_page()
    try:
        cdp.call("Runtime.evaluate", {"expression": expr})
        print(f"スクロール: {direction or 'down'}")
    finally:
        cdp.close()


def cmd_back() -> None:
    cdp = _connect_page()
    try:
        cdp.call("Runtime.evaluate", {"expression": "history.back()"})
        print("戻った")
    finally:
        cdp.close()


def cmd_status() -> None:
    if not _browser_running():
        print("stream-browser: 停止")
        return
    cdp = _connect_page()
    try:
        r = cdp.call("Runtime.evaluate",
                     {"expression": "location.href", "returnByValue": True})
        url = (r.get("result") or {}).get("result", {}).get("value", "?")
        print(f"stream-browser: 起動中 — {url}")
    finally:
        cdp.close()


def cmd_close() -> None:
    subprocess.run(["systemctl", "--user", "stop", UNIT],
                   check=False, capture_output=True)
    print("stream-browser: 閉じた")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd == "open":
        cmd_open(arg)
    elif cmd == "scroll":
        cmd_scroll(arg)
    elif cmd == "back":
        cmd_back()
    elif cmd == "status":
        cmd_status()
    elif cmd == "close":
        cmd_close()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import urllib.parse
import urllib.request

CDP_PORT = int(os.environ.get("STREAM_BROWSER_PORT", "9222"))
CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"
UNIT = "kai-stream-browser"
DISPLAY = os.environ.get("DISPLAY", ":0")

# 配信向け起動フラグ: 翻訳バー・初回実行・既定ブラウザ確認・更新通知・
# インフォバー・パスワード保存を抑止し、配信画面をきれいに保つ。
CHROMIUM_FLAGS = [
    f"--remote-debugging-port={CDP_PORT}",
    # CDP WebSocket の Origin 検査を無効化する "*" は使わない（Issue #77 M-a）。
    # 外部由来 URL（Issue リンク先）を開く運用なので、訪問先ページが CDP に
    # 接続してブラウザを完全制御（Runtime.evaluate・セッション読取）する余地を
    # 断つ。本 CLI の websocket クライアントは Origin を送らないため影響なし。
    "--remote-allow-origins=http://localhost",
    "--disable-gpu",  # llvmpipe（GPU なし）。PoC で静的ページは CPU ほぼ 0
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,TranslateUI",
    "--disable-translate",
    "--disable-infobars",
    "--password-store=basic",
    "--start-maximized",
]


# 配信ブラウザで開いてよい URL（Issue #77 M-a）。外部由来 URL（Issue 本文の
# リンク等）を無制限に開くと、悪意ページ経由の攻撃面（CSRF・CDP 接続・偽装表示）
# になるため、https の信頼ドメインと自ホストに限定する。追加が必要なら
# STREAM_BROWSER_ALLOW にカンマ区切りでドメインを足す。
_ALLOWED_DOMAINS = {"github.com", "githubusercontent.com"}
_ALLOWED_DOMAINS.update(
    d.strip().lower() for d in os.environ.get("STREAM_BROWSER_ALLOW", "").split(",") if d.strip())
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _url_allowed(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if parts.scheme in ("http", "https") and host in _LOOPBACK_HOSTS:
        return True  # 自前サービス（overlay 等）の確認用
    if parts.scheme != "https":
        return False
    return any(host == d or host.endswith("." + d) for d in _ALLOWED_DOMAINS)


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
        import websocket  # python3-websocket（obsws.py と同じ。接続時のみ必要）
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


# --- ウィンドウ前面化・最大化（Issue #95）---------------------------------------
#
# 第5回リハーサルで「操作対象が見えにくい」（ブラウザが小さい/他ウィンドウに隠れる）
# 指摘を受け、open 実行のたびに対象の Chromium ウィンドウを最大化・前面化する。
# wmctrl は broadcast.sh の OBS 終了処理で既に使っている依存（kai-vm には導入済み）。
# wmctrl 不在・X 不在（配信外の開発機等）では例外を握りつぶして何もしない
# （配信装飾の失敗で本処理=URL を開く、を止めない）。
_BROWSER_WM_CLASS = "chromium"


def _wmctrl_find_window(class_substr: str) -> str | None:
    """wmctrl -lx から WM_CLASS に class_substr（小文字部分一致）を含む先頭ウィンドウの ID。"""
    try:
        r = subprocess.run(["wmctrl", "-lx"], capture_output=True, timeout=3, text=True)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    needle = class_substr.lower()
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        if needle in parts[2].lower():
            return parts[0]
    return None


def _raise_and_maximize(class_substr: str) -> None:
    """class_substr にマッチするウィンドウを最大化＋前面化する（best-effort）。"""
    try:
        win_id = _wmctrl_find_window(class_substr)
        if not win_id:
            return
        subprocess.run(
            ["wmctrl", "-i", "-r", win_id, "-b", "add,maximized_vert,maximized_horz"],
            capture_output=True, timeout=3)
        subprocess.run(["wmctrl", "-i", "-a", win_id], capture_output=True, timeout=3)
    except Exception:
        pass


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
    if not _url_allowed(url):
        print(f"開けない URL: {url}\n"
              f"（https の信頼ドメイン {sorted(_ALLOWED_DOMAINS)} と自ホストのみ。"
              "追加は STREAM_BROWSER_ALLOW）", file=sys.stderr)
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
    _raise_and_maximize(_BROWSER_WM_CLASS)


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

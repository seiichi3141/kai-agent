"""kai_director: 配信演出プラグイン — 編集アクションを VSCode へリアルタイム通知。

設計: docs/kai/design/00-system.md ADR-1 と同じ hook 流儀（Issue #8）。
kai（hermes）の編集はエディタを介さずファイルへ直接行われるため、配信画面の
VSCode には何も映らない。本プラグインは編集系ツール（write_file / patch）の
post_tool_call から対象ファイルパスを抽出し、VM 内の VSCode 拡張
kai-typewriter（kai-services/streaming/vm/vscode/）へ HTTP 通知する。
拡張側が差分を計算し、タイピング風のアニメーションで再生する。

実装上の絶対ルール（ADR-1 と同じ）: hook はエージェントのターンスレッド上で
同期実行されるため、hook 内ではキューに積んで即 return する。HTTP 送出は
背景スレッドが行う。拡張・VSCode 不在（配信していない時）は黙って落とす。

設定（config.yaml）:
  plugins.entries.kai_director.enabled       通知の有効化（既定 true）
  plugins.entries.kai_director.endpoint      kai-typewriter の URL（既定 http://127.0.0.1:8920）
"""

from __future__ import annotations

import json
import queue
import re
import threading
import urllib.request
from typing import Any

_PLUGIN_ID = "kai_director"

# apply_patch 形式（*** Update File: / *** Add File:）から対象パスを抜く
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)


def _plugin_cfg() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        entry = cfg_get(cfg, "plugins", "entries", _PLUGIN_ID, default={})
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def extract_edited_files(tool_name: str, args: Any) -> list[str]:
    """編集系ツールの引数から対象ファイルパスを抽出する。

    args は dict のことも JSON 文字列のこともある（トレース実測）。
    抽出できなければ空リスト（通知しないだけで作業に影響なし）。
    """
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []

    if tool_name == "write_file":
        path = args.get("path") or args.get("file_path")
        return [str(path)] if path else []

    if tool_name == "patch":
        patch_text = args.get("patch") or ""
        if not isinstance(patch_text, str):
            return []
        return [m.strip() for m in _PATCH_FILE_RE.findall(patch_text)]

    return []


class _Director:
    """編集通知の背景ワーカー。hook からはキュー積みのみ。"""

    def __init__(self, start_thread: bool = True) -> None:
        cfg = _plugin_cfg()
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.endpoint: str = str(cfg.get("endpoint") or "http://127.0.0.1:8920")
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        if start_thread:  # テストでは False にして _notify を直接呼ぶ
            thread = threading.Thread(target=self._run, name="kai-director", daemon=True)
            thread.start()

    def push_edit(self, files: list[str], tool: str) -> None:
        if not (self.enabled and files):
            return
        try:
            self._q.put_nowait({"files": files, "tool": tool})
        except queue.Full:
            pass  # 溢れたら捨てる（演出は best-effort）

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                self._notify(item)
            except Exception:
                pass  # VSCode/拡張 不在（配信外）は黙って落とす

    def _notify(self, item: dict, timeout: float = 2.0) -> None:
        body = json.dumps(item, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint.rstrip("/") + "/edit",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()


_director: _Director | None = None


def _on_post_tool_call(tool_name: str = "", args: Any = None, status: str = "",
                       **_: Any) -> None:
    if _director is None or tool_name not in ("write_file", "patch"):
        return
    if status and status not in ("ok", "success"):
        return  # 失敗した編集は再生しない
    files = extract_edited_files(tool_name, args)
    if files:
        _director.push_edit(files, tool_name)


def register(ctx) -> None:
    """hermes plugin エントリポイント。"""
    global _director
    if _director is None:
        _director = _Director()
    ctx.register_hook("post_tool_call", _on_post_tool_call)

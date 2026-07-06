"""kai_ide: kai が配信画面の VSCode を高速に操作するためのツール群（Issue #49）。

設計: docs/kai/design/vscode-bridge.md。judgement = Codex(gpt-5.5) / 実行 = hermes。
本 plugin は VSCode 拡張「kai VSCode ブリッジ」（kai-typewriter、127.0.0.1:8920）を
叩くツールを hermes に登録し、kai が VSCode の状態を読み、ファイルを開く/閉じる
などの操作を computer_use を使わず高速に行えるようにする。

PR-1（本ファイル）: 状態系ツール（vscode_state / vscode_open / vscode_close_tab）。
terminal / write_file / patch の override は後続 PR（設計 §10）。

配信外（拡張不在）でもツールは失敗しない。ブリッジ不達時は「ブリッジ未接続」を
返し、kai の作業は止めない（演出は best-effort）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_PLUGIN_ID = "kai_ide"
_DEFAULT_BRIDGE = "http://127.0.0.1:8920"


def _plugin_cfg() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        entry = cfg_get(cfg, "plugins", "entries", _PLUGIN_ID, default={})
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _bridge_url() -> str:
    return str(_plugin_cfg().get("bridge_url") or _DEFAULT_BRIDGE).rstrip("/")


def _bridge_request(method: str, path: str, body: dict | None = None,
                    timeout: float = 3.0) -> dict:
    """ブリッジへ HTTP。不達は RuntimeError（呼び出し側が縮退メッセージにする）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        _bridge_url() + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"VSCode ブリッジに接続できません（配信外か拡張未起動）: {e}") from e


# --- ツール handler ------------------------------------------------------------


def handle_vscode_state(args: dict | None = None, **_: Any) -> str:
    """開いているタブ・アクティブファイル+行・dirty を返す。"""
    try:
        state = _bridge_request("GET", "/state")
    except RuntimeError as e:
        return str(e)
    tabs = state.get("tabs") or []
    active = state.get("active")
    lines = [f"開いているタブ {len(tabs)} 個:"]
    for t in tabs:
        mark = "●" if t.get("active") else " "
        dirty = "*" if t.get("dirty") else " "
        lines.append(f"  {mark}{dirty} {t.get('path')}")
    if active:
        lines.append(f"アクティブ: {active.get('path')} :{active.get('line')} 行目"
                     + ("（未保存）" if active.get("dirty") else ""))
    else:
        lines.append("アクティブなエディタなし")
    return "\n".join(lines)


def handle_vscode_open(args: dict | None = None, **_: Any) -> str:
    """ファイルを VSCode で開く（視聴者に見せる）。line 指定でその行へスクロール。"""
    args = args or {}
    path = str(args.get("path") or "")
    line = args.get("line")
    if not path:
        return "エラー: path が必要です"
    body: dict = {"path": path}
    if isinstance(line, (int, float)) and line:
        body["line"] = int(line)
    try:
        _bridge_request("POST", "/open", body)
    except RuntimeError as e:
        return str(e)
    where = f"（{int(line)} 行目）" if body.get("line") else ""
    return f"VSCode で開いた: {path}{where}"


def handle_vscode_close_tab(args: dict | None = None, **_: Any) -> str:
    """タブを閉じる。close_all=true で全て、または path 指定でそのファイルのタブ。"""
    args = args or {}
    path = str(args.get("path") or "")
    close_all = bool(args.get("close_all"))
    if not path and not close_all:
        return "エラー: path か close_all のどちらかが必要です"
    body = {"all": True} if close_all else {"path": path}
    try:
        res = _bridge_request("POST", "/close", body)
    except RuntimeError as e:
        return str(e)
    if close_all:
        return "すべてのタブを閉じた"
    return f"タブを閉じた: {path}（{res.get('count', 0)} 個）"


# --- スキーマ ------------------------------------------------------------------

_STATE_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_OPEN_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "開くファイルの絶対パス"},
        "line": {"type": "integer", "description": "スクロールする行（1 始まり、省略可）"},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_CLOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "閉じるタブのファイル絶対パス"},
        "close_all": {"type": "boolean", "description": "true で全タブを閉じる"},
    },
    "additionalProperties": False,
}

_TOOLS = (
    ("vscode_state", _STATE_SCHEMA, handle_vscode_state, "🪟",
     "配信画面の VSCode の状態（開いているタブ・アクティブファイルと行・未保存）を取得する。"
     "どのタブを閉じる/開くかを判断するのに使う。"),
    ("vscode_open", _OPEN_SCHEMA, handle_vscode_open, "📂",
     "ファイルを配信画面の VSCode で開いて視聴者に見せる。line 指定でその行へスクロール。"),
    ("vscode_close_tab", _CLOSE_SCHEMA, handle_vscode_close_tab, "🗙",
     "VSCode のタブを閉じる（path 指定 or close_all）。不要なタブを片付けるのに使う。"),
)


def register(ctx) -> None:
    """hermes plugin エントリポイント。"""
    for name, schema, handler, emoji, description in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="kai_ide",
            schema=schema,
            handler=handler,
            description=description,
            emoji=emoji,
        )

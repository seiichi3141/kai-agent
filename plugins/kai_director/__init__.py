"""kai_director: 配信演出プラグイン — 編集とコマンド実行を配信画面に映す。

設計: docs/kai/design/00-system.md ADR-1 と同じ hook 流儀（Issue #8 / #30）。
kai（hermes）はツールをエディタやターミナルを介さず内部実行するため、配信画面
には何も映らない。本プラグインは 2 つの演出を担う:

1. 編集（Issue #8）: 編集系ツール（write_file / patch）の post_tool_call から
   対象ファイルパスを抽出し、VM 内の VSCode 拡張 kai-typewriter へ HTTP 通知
   する。拡張側が差分を計算しタイピング風に再生する。
2. コマンド実行の可視化（Issue #30）: terminal ツールの pre_tool_call で実行
   コマンドを、post_tool_call で結果（exit/所要時間）をコマンドログファイルに
   追記する。配信ステージの tmux ログペインが `tail -f` して映す。

実装上の絶対ルール（ADR-1 と同じ）: hook はエージェントのターンスレッド上で
同期実行される。HTTP 送出は背景スレッドが行う。コマンドログはローカルの
1 行追記なので hook 内で直接書く（速い）。拡張・VSCode 不在（配信外）は黙って
落とす。秘匿情報は書き出す前に必ずマスクする。

設定（config.yaml）:
  plugins.entries.kai_director.enabled       通知の有効化（既定 true）
  plugins.entries.kai_director.endpoint      kai-typewriter の URL（既定 http://127.0.0.1:8920）
  plugins.entries.kai_director.command_log   コマンドログのパス（既定 ~/.config/kai/command-log）
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any

_PLUGIN_ID = "kai_director"

# apply_patch 形式（*** Update File: / *** Add File:）から対象パスと種別を抜く
_PATCH_EDIT_RE = re.compile(r"^\*\*\* (Update|Add) File: (.+)$", re.MULTILINE)

# --- 秘匿マスク（kai_trace / speechd / narrator と同方針。plugin 単体で完結）----

_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[posur]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
]


def _collect_env_secrets() -> list[str]:
    vals: set[str] = set()
    for k, v in os.environ.items():
        if not v or len(v) < 8:
            continue
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|PAT|CREDENTIAL)", k, re.IGNORECASE):
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


_ENV_SECRETS = _collect_env_secrets()


def _mask(text: str) -> str:
    if not text:
        return text
    for secret in _ENV_SECRETS:
        if secret in text:
            text = text.replace(secret, "«redacted»")
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("«redacted»", text)
    return text


def _plugin_cfg() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        entry = cfg_get(cfg, "plugins", "entries", _PLUGIN_ID, default={})
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def extract_edits(tool_name: str, args: Any, new_paths: set[str] | None = None) -> list[dict]:
    """編集系ツールの引数から [{path, action}] を抽出する（Issue #32）。

    action は "add"（新規作成）か "update"（既存編集）。kai-typewriter は
    新規作成のとき全文をタイピング再生し、更新のとき差分だけ再生する。
    - patch: `*** Add File:` → add、`*** Update File:` → update
    - write_file: new_paths（pre_tool_call で「書き込み前に存在しなかった」と
      判定したパス集合）に含まれれば add、なければ update
    """
    new_paths = new_paths or set()
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []

    if tool_name == "write_file":
        path = args.get("path") or args.get("file_path")
        if not path:
            return []
        path = str(path)
        return [{"path": path, "action": "add" if path in new_paths else "update"}]

    if tool_name == "patch":
        patch_text = args.get("patch") or ""
        if not isinstance(patch_text, str):
            return []
        return [
            {"path": m.group(2).strip(), "action": "add" if m.group(1) == "Add" else "update"}
            for m in _PATCH_EDIT_RE.finditer(patch_text)
        ]

    return []


def write_target_path(args: Any) -> str:
    """write_file の対象パス（pre_tool_call で存在判定するため）。無ければ空。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ""
    if isinstance(args, dict):
        return str(args.get("path") or args.get("file_path") or "")
    return ""


def extract_command(args: Any) -> str:
    """terminal ツールの引数から実行コマンド文字列を取り出す（無ければ空）。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args.strip()
    if isinstance(args, dict):
        cmd = args.get("command") or args.get("cmd") or ""
        return str(cmd).strip()
    return ""


class _Director:
    """編集通知の背景ワーカー。hook からはキュー積みのみ。"""

    def __init__(self, start_thread: bool = True) -> None:
        cfg = _plugin_cfg()
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.endpoint: str = str(cfg.get("endpoint") or "http://127.0.0.1:8920")
        default_log = str(Path(os.path.expanduser("~/.config/kai/command-log")))
        self.command_log: Path = Path(str(cfg.get("command_log") or default_log))
        self._log_lock = threading.Lock()
        # write_file の pre_tool_call で「書き込み前に存在しなかった」パスを覚える
        # （post_tool_call では既にファイルが存在するので pre で判定する）
        self._new_paths: set[str] = set()
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        if start_thread:  # テストでは False にして _notify を直接呼ぶ
            thread = threading.Thread(target=self._run, name="kai-director", daemon=True)
            thread.start()

    def mark_write_target(self, path: str) -> None:
        """write_file 実行前に、対象がまだ無ければ「新規」として覚える（Issue #32）。"""
        if path and not os.path.exists(path):
            self._new_paths.add(path)

    def push_edits(self, edits: list[dict], tool: str) -> None:
        if not (self.enabled and edits):
            return
        # 消費したら new_paths から外す（次回の同名編集は update になる）
        for e in edits:
            self._new_paths.discard(e.get("path"))
        try:
            self._q.put_nowait({"edits": edits, "tool": tool})
        except queue.Full:
            pass  # 溢れたら捨てる（演出は best-effort）

    def log_command(self, command: str) -> None:
        """実行コマンドを `$ <command>` 形式でコマンドログへ追記する（Issue #30）。

        配信ステージの tmux ログペインが tail -f して映す。秘匿マスクを必ず通す。
        ローカルの 1 行追記なので hook スレッド上で直接書く（速い）。
        """
        if not (self.enabled and command):
            return
        self._append_log("$ " + _mask(command).replace("\n", " ⏎ "))

    def log_result(self, status: str = "", duration_ms: Any = None) -> None:
        """直前コマンドの結果（失敗・長時間のみ）を控えめに追記する。"""
        if not self.enabled:
            return
        parts = []
        if status and status not in ("ok", "success"):
            parts.append(f"✗ {status}")
        if isinstance(duration_ms, (int, float)) and duration_ms >= 3000:
            parts.append(f"{duration_ms / 1000:.0f}s")
        if parts:
            self._append_log("  → " + " ".join(parts))

    def _append_log(self, line: str) -> None:
        try:
            with self._log_lock:
                self.command_log.parent.mkdir(parents=True, exist_ok=True)
                with self.command_log.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # ログ書き込み失敗は作業を止めない

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


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> None:
    # 観測のみ（block ディレクティブは返さない）
    if _director is None:
        return
    if tool_name == "terminal":
        _director.log_command(extract_command(args))  # コマンドをログに映す（#30）
    elif tool_name == "write_file":
        _director.mark_write_target(write_target_path(args))  # 新規判定（#32）


def _on_post_tool_call(tool_name: str = "", args: Any = None, status: str = "",
                       duration_ms: Any = None, **_: Any) -> None:
    if _director is None:
        return
    if tool_name == "terminal":
        _director.log_result(status=status, duration_ms=duration_ms)
        return
    if tool_name not in ("write_file", "patch"):
        return
    if status and status not in ("ok", "success"):
        return  # 失敗した編集は再生しない
    edits = extract_edits(tool_name, args, _director._new_paths)
    if edits:
        _director.push_edits(edits, tool_name)


def register(ctx) -> None:
    """hermes plugin エントリポイント。"""
    global _director
    if _director is None:
        _director = _Director()
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)

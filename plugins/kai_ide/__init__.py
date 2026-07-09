"""kai_ide: kai が配信画面の VSCode を高速に操作するためのツール群（Issue #49）。

設計: docs/kai/design/vscode-bridge.md。judgement = Codex(gpt-5.5) / 実行 = hermes。
本 plugin は VSCode 拡張「kai VSCode ブリッジ」（kai-typewriter、127.0.0.1:8920）を
叩くツールを hermes に登録し、kai が VSCode の状態を読み、ファイルを開く/閉じる
などの操作を computer_use を使わず高速に行えるようにする。

PR-1: 状態系ツール（vscode_state / vscode_open / vscode_close_tab）。
PR-2: terminal の override（配信画面の tmux ペインで実際に実行し出力を捕捉）。

配信外（拡張・tmux 不在）でもツールは失敗しない。ブリッジ不達時は縮退メッセージ、
terminal は built-in へフォールバックし、kai の作業は止めない（演出は best-effort・
実処理の戻り値は必ず保証）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_PLUGIN_ID = "kai_ide"
_DEFAULT_BRIDGE = "http://127.0.0.1:8920"
_DEFAULT_TERM_TARGET = "kai-term"
# terminal override の可視実行で使う固定パス（hermes は直列実行なので衝突しない）
_TERM_OUT = "/tmp/kai-term.out"
_TERM_DONE = "/tmp/kai-term.done"
_TERM_READY = "/tmp/kai-term.ready"

# 秘匿マスク（kai_trace / narrator と同方針。Codex へ返す出力に適用）
_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[posur]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
]


def _mask(text: str) -> str:
    if not text:
        return text
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


# --- ウィンドウ前面化（Issue #95）-----------------------------------------------
#
# 第5回リハーサルで「VSCode を操作しているのにブラウザが前面で見えない」指摘を
# 受け、VSCode 系ツール実行時に VSCode ウィンドウを前面化する（最大化はしない。
# 最大化は stream-browser.py 側の要件）。実装は stream-browser.py の同名ヘルパー
# と同じだが、plugin 単体完結の原則（plugins/kai_ide と kai-services 間で import
# しない。kai_narrator の三層マスクと同じ流儀）によりコピーしている。
# wmctrl 不在・X 不在（配信外）では例外を握りつぶして何もしない
# （演出の失敗で本処理＝ファイル書込/タブ操作を止めない）。
_VSCODE_WM_CLASS = "code"


def _raise_vscode_window() -> None:
    try:
        r = subprocess.run(["wmctrl", "-lx"], capture_output=True, timeout=3, text=True)
        if r.returncode != 0:
            return
        needle = _VSCODE_WM_CLASS.lower()
        win_id = None
        for line in r.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            if needle in parts[2].lower():
                win_id = parts[0]
                break
        if not win_id:
            return
        subprocess.run(["wmctrl", "-i", "-a", win_id], capture_output=True, timeout=3)
    except Exception:
        pass


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
    _raise_vscode_window()
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
    _raise_vscode_window()
    if close_all:
        return "すべてのタブを閉じた"
    return f"タブを閉じた: {path}（{res.get('count', 0)} 個）"


# --- terminal override（配信画面の tmux ペインで実際に実行）--------------------


def _term_target() -> str:
    return str(_plugin_cfg().get("terminal_target") or _DEFAULT_TERM_TARGET)


# --- タイプライター演出（Issue #96: kai-term のコマンド実行も1文字ずつ見せる）----
#
# 「人間がタイプしている」ように見せるため、実行前に send-keys -l を1文字ずつ
# 送ってから Enter を送る。実行そのものは既存どおり1回（Enter は最後に1度だけ）。
# 長いコマンドで配信のテンポが崩れないよう、総タイプ時間の上限
# （_TYPEWRITER_CAP_S）を設け、間隔を詰めても最低間隔（_TYPEWRITER_MIN_STEP_S）を
# 下回るなら演出そのものを諦めて一括送信にフォールバックする。
_DEFAULT_TYPEWRITER_S = 0.04
_TYPEWRITER_CAP_S = 2.5
_TYPEWRITER_MIN_STEP_S = 0.005


def _typewriter_interval_s() -> float:
    """1文字あたりのタイプ演出間隔（秒）。既定 0.04、0（以下）で演出オフ。"""
    try:
        return float(_plugin_cfg().get("typewriter_command_s", _DEFAULT_TYPEWRITER_S))
    except (TypeError, ValueError):
        return _DEFAULT_TYPEWRITER_S


def _tmux_target_exists(target: str) -> bool:
    """送信先の tmux セッションが存在するか（無ければ built-in へフォールバック）。"""
    session = target.split(":", 1)[0].split(".", 1)[0]
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# 配管（tee/marker）をペインに映さず、視聴者には `kai '<command>'` だけ見せるための
# ヘルパー関数。ペインに一度だけ定義する（配信前に流れる・以降は見えない）。
# 出力は tee でペイン表示 + ファイル捕捉。PIPESTATUS[0] で本体の exit を取る。
def _helper_def() -> str:
    return (
        f"kai(){{ {{ eval \"$1\"; }} 2>&1 | tee {_TERM_OUT}; "
        f"printf 'KAI_EXIT:%s\\n' \"${{PIPESTATUS[0]}}\" > {_TERM_DONE}; }}; "
        f"clear; touch {_TERM_READY}"
    )


def _tmux_send_literal(target: str, literal: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", literal],
                   check=True, capture_output=True, timeout=5)


def _tmux_send_enter(target: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                   check=True, capture_output=True, timeout=5)


def _tmux_send(target: str, literal: str) -> None:
    """literal を一括で（演出なしで）送ってから Enter を送る。"""
    _tmux_send_literal(target, literal)
    _tmux_send_enter(target)


def _tmux_send_typed(target: str, literal: str, interval: float) -> None:
    """literal を1文字ずつ送ってから Enter を送る（タイプライター演出）。

    実行は既存どおり1回（Enter は最後に1度だけ）。演出は「入力の見え方」だけの
    変更で、送信内容そのものは変えない。次の場合は演出せず一括送信する:
    - interval が 0 以下（演出オフ）
    - literal が空
    - literal に改行を含む（ヒアドキュメント等の複数行コマンド。tmux 側の解釈が
      複雑になるため演出しない）
    - 上限時間（_TYPEWRITER_CAP_S）に収まるよう間隔を詰めても、なお最低間隔
      （_TYPEWRITER_MIN_STEP_S）を下回るほど長いコマンド（演出の意味が薄く、
      配信のテンポを崩すだけなので諦めて一括送信にする）
    """
    if interval <= 0 or not literal or "\n" in literal:
        _tmux_send(target, literal)
        return
    step = interval
    if len(literal) * step > _TYPEWRITER_CAP_S:
        step = _TYPEWRITER_CAP_S / len(literal)
    if step < _TYPEWRITER_MIN_STEP_S:
        _tmux_send(target, literal)
        return
    for ch in literal:
        _tmux_send_literal(target, ch)
        time.sleep(step)
    _tmux_send_enter(target)


def _ensure_helper(target: str) -> None:
    """kai ヘルパー関数がペインに未定義なら定義する（配管を隠すため。冪等）。"""
    if os.path.exists(_TERM_READY):
        return
    _tmux_send(target, _helper_def())
    for _ in range(20):
        if os.path.exists(_TERM_READY):
            return
        time.sleep(0.1)


def _run_visible(command: str, target: str, timeout: float) -> dict | None:
    """配信画面の tmux ペインでコマンドを実際に実行し、出力と exit code を捕捉する。

    視聴者には `kai '<command>'` だけが見える（tee/marker の配管はヘルパー関数の
    中に隠す）。tmux 不在・送信失敗なら None を返し built-in へフォールバック
    （二重実行しない）。hermes はコマンドを直列実行するため固定パスで衝突しない。
    """
    if not _tmux_target_exists(target):
        return None
    try:
        _ensure_helper(target)
        for f in (_TERM_OUT, _TERM_DONE):
            try:
                os.remove(f)
            except OSError:
                pass
        # command 内の単一引用符をエスケープして kai '...' で渡す
        escaped = command.replace("'", "'\\''")
        _tmux_send_typed(target, f"kai '{escaped}'", _typewriter_interval_s())
    except Exception:
        return None  # 送信失敗 → フォールバック
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(_TERM_DONE):
            break
        time.sleep(0.2)
    output = ""
    exit_code: Any = None
    try:
        if os.path.exists(_TERM_OUT):
            output = Path(_TERM_OUT).read_text(encoding="utf-8", errors="replace")
        if os.path.exists(_TERM_DONE):
            m = re.search(r"KAI_EXIT:(-?\d+)", Path(_TERM_DONE).read_text(encoding="utf-8"))
            if m:
                exit_code = int(m.group(1))
    except OSError:
        pass
    if exit_code is None:
        # マーカー未出現＝タイムアウト。捕捉できた分を返す（二重実行を避けフォールバックしない）
        note = f"（{int(timeout)} 秒でまだ完了していない。ペインで実行継続中）"
        return {"output": _mask(output) + "\n" + note, "exit_code": None, "timed_out": True}
    return {"output": _mask(output), "exit_code": exit_code}


def _fallback_terminal(args: dict, kw: dict) -> str:
    """built-in terminal ツールへ委譲（配信外・複雑モード・tmux 不在時）。"""
    try:
        from tools.terminal_tool import _handle_terminal
        return _handle_terminal(args, **kw)
    except Exception as e:
        return json.dumps({"output": "", "exit_code": None,
                          "error": f"terminal 実行に失敗: {e}"}, ensure_ascii=False)


def handle_terminal(args: dict | None = None, **kw: Any) -> str:
    """terminal の override。配信中の見える端末で実行、それ以外は built-in。

    可視実行の対象は「単発の前景コマンド」に限る。background / pty / watch_patterns
    などの高度モードや、tmux ペイン不在（配信外）は built-in に委譲する
    （設計 vscode-bridge.md §5.2 / §6）。
    """
    args = args or {}
    command = str(args.get("command") or "")
    enabled = bool(_plugin_cfg().get("enabled", True))
    complex_mode = bool(args.get("background") or args.get("pty")
                        or args.get("watch_patterns") or args.get("notify_on_complete"))
    if not command or not enabled or complex_mode:
        return _fallback_terminal(args, kw)
    timeout = float(args.get("timeout") or 180)
    result = _run_visible(command, _term_target(), timeout)
    if result is None:
        return _fallback_terminal(args, kw)  # tmux 不在・送信失敗 → built-in
    return json.dumps(result, ensure_ascii=False)


# --- write_file / patch override（ディスク書込 + VSCode タイプ表示）------------

# apply_patch 形式（*** Update File: / *** Add File:）から対象パスと種別を抜く
_PATCH_EDIT_RE = re.compile(r"^\*\*\* (Update|Add) File: (.+)$", re.MULTILINE)


def _notify_edit(edits: list[dict]) -> None:
    """ブリッジ /edit へ編集通知（タイプ再生）。best-effort、失敗は握りつぶす。

    拡張の /edit は絶対パス（`/` 始まり）しか受け付けないため、相対パスを
    プロセス cwd 基準で絶対化してから渡す（#62: 相対パスだと通知が捨てられ、
    タイプライターが出なかった）。
    """
    if not edits:
        return
    abs_edits = []
    for e in edits:
        p = str(e.get("path") or "")
        if p and not p.startswith("/"):
            p = os.path.abspath(p)
        if p:
            abs_edits.append({"path": p, "action": e.get("action", "update")})
    if not abs_edits:
        return
    try:
        _bridge_request("POST", "/edit", {"edits": abs_edits}, timeout=2.0)
    except RuntimeError:
        pass  # 拡張不在（配信外）は演出をスキップ
    _raise_vscode_window()


def _extract_patch_edits(patch_text: Any) -> list[dict]:
    """patch から [{path, action}] を抽出（Add→add / Update→update）。"""
    if not isinstance(patch_text, str):
        return []
    return [
        {"path": m.group(2).strip(), "action": "add" if m.group(1) == "Add" else "update"}
        for m in _PATCH_EDIT_RE.finditer(patch_text)
    ]


def _builtin_file_handler(name: str):
    """built-in の write_file / patch handler を取得（フォールバック・実書込用）。"""
    from tools.file_tools import _handle_patch, _handle_write_file
    return _handle_write_file if name == "write_file" else _handle_patch


def handle_write_file(args: dict | None = None, **kw: Any) -> str:
    """write_file の override: ディスクへ実書込 + VSCode にタイプ表示。"""
    args = args or {}
    path = str(args.get("path") or args.get("file_path") or "")
    # 書込前に新規判定（post では存在してしまうため）
    is_new = bool(path) and not os.path.exists(path)
    result = _builtin_file_handler("write_file")(args, **kw)
    if path and "error" not in str(result).lower()[:200]:
        _notify_edit([{"path": path, "action": "add" if is_new else "update"}])
    return result


def handle_patch(args: dict | None = None, **kw: Any) -> str:
    """patch の override: ディスクへ実書込 + VSCode に差分をタイプ表示。

    built-in patch は 2 モード:
    - mode='replace'（既定）: path + old_string + new_string で 1 ファイルを編集
    - mode='patch': V4A の patch テキスト（*** Update File: ...）で複数ファイル
    replace モードでは patch テキストが無いので path から編集対象を取る
    （ここを取りこぼすとタイプライターが出ない。#62）。
    """
    args = args or {}
    result = _builtin_file_handler("patch")(args, **kw)
    if "error" not in str(result).lower()[:200]:
        mode = str(args.get("mode") or "replace")
        if mode == "patch":
            edits = _extract_patch_edits(args.get("patch"))
        else:  # replace（既定）
            path = str(args.get("path") or args.get("file_path") or "")
            edits = [{"path": path, "action": "update"}] if path else []
        _notify_edit(edits)
    return result


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
    # built-in の override（allow_tool_override: true が無ければスキップ）。config で
    # 許可されていない環境でも状態系ツールだけで動くよう、失敗は握りつぶす。
    import logging
    _log = logging.getLogger(__name__)
    try:
        from tools.terminal_tool import TERMINAL_SCHEMA
        ctx.register_tool(
            name="terminal", toolset="kai_ide", schema=TERMINAL_SCHEMA,
            handler=handle_terminal, override=True, emoji="⌨️",
            description="配信画面の統合ターミナルで実際にコマンドを実行し出力を捕捉する"
                        "（見える実行）。高度モード・配信外は built-in へフォールバック。",
        )
    except Exception as e:
        _log.info("kai_ide: terminal override をスキップ（%s）", e)
    try:
        from tools.file_tools import PATCH_SCHEMA, WRITE_FILE_SCHEMA
        ctx.register_tool(
            name="write_file", toolset="kai_ide", schema=WRITE_FILE_SCHEMA,
            handler=handle_write_file, override=True, emoji="✍️",
            description="ファイルをディスクに書き込み、配信画面の VSCode にタイプ表示する。",
        )
        ctx.register_tool(
            name="patch", toolset="kai_ide", schema=PATCH_SCHEMA,
            handler=handle_patch, override=True, emoji="🔧",
            description="差分をディスクに適用し、配信画面の VSCode に変更をタイプ表示する。",
        )
    except Exception as e:
        _log.info("kai_ide: write_file/patch override をスキップ（%s）", e)

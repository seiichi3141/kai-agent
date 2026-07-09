"""Tests for the kai_ide plugin (plugins/kai_ide/).

VSCode ブリッジ（127.0.0.1:8920）への HTTP をスタブして、状態系ツール
（vscode_state / vscode_open / vscode_close_tab）の handler を検証する。
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "kai_ide"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kai_ide", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kai_ide"
    sys.modules["hermes_plugins.kai_ide"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ide(monkeypatch):
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_plugin_cfg", lambda: {})
    return mod


def _stub_bridge(ide, monkeypatch, response=None, fail=False):
    """_bridge_request をスタブし、送信内容を記録する。"""
    sent = {}

    def _fake(method, path, body=None, timeout=3.0):
        sent["method"] = method
        sent["path"] = path
        sent["body"] = body
        if fail:
            raise RuntimeError("VSCode ブリッジに接続できません（配信外か拡張未起動）: boom")
        return response or {}

    monkeypatch.setattr(ide, "_bridge_request", _fake)
    return sent


# --- vscode_state ---------------------------------------------------------------


def test_vscode_state_formats_tabs_and_active(ide, monkeypatch):
    state = {
        "tabs": [
            {"path": "/a.py", "active": True, "dirty": False},
            {"path": "/b.md", "active": False, "dirty": True},
        ],
        "active": {"path": "/a.py", "line": 12, "dirty": False},
    }
    _stub_bridge(ide, monkeypatch, response=state)
    out = ide.handle_vscode_state({})
    assert "開いているタブ 2 個" in out
    assert "/a.py" in out and "/b.md" in out
    assert "12 行目" in out


def test_vscode_state_bridge_unavailable(ide, monkeypatch):
    _stub_bridge(ide, monkeypatch, fail=True)
    out = ide.handle_vscode_state({})
    assert "ブリッジに接続できません" in out  # 縮退メッセージ、例外は投げない


# --- vscode_open ----------------------------------------------------------------


def test_vscode_open_sends_path_and_line(ide, monkeypatch):
    sent = _stub_bridge(ide, monkeypatch, response={"opened": "/x.py"})
    raised = {}
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: raised.setdefault("called", True))
    out = ide.handle_vscode_open({"path": "/x.py", "line": 42})
    assert sent["method"] == "POST" and sent["path"] == "/open"
    assert sent["body"] == {"path": "/x.py", "line": 42}
    assert "/x.py" in out and "42 行目" in out
    assert raised.get("called")  # Issue #95: 開いたら VSCode を前面化する


def test_vscode_open_requires_path(ide, monkeypatch):
    _stub_bridge(ide, monkeypatch)
    assert "path が必要" in ide.handle_vscode_open({"path": ""})


def test_vscode_open_bridge_unavailable(ide, monkeypatch):
    _stub_bridge(ide, monkeypatch, fail=True)
    raised = {}
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: raised.setdefault("called", True))
    assert "ブリッジに接続できません" in ide.handle_vscode_open({"path": "/x.py"})
    assert "called" not in raised  # ブリッジ不達なら前面化もしない


# --- vscode_close_tab -----------------------------------------------------------


def test_vscode_close_tab_by_path(ide, monkeypatch):
    sent = _stub_bridge(ide, monkeypatch, response={"closed": "/x.py", "count": 1})
    raised = {}
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: raised.setdefault("called", True))
    out = ide.handle_vscode_close_tab({"path": "/x.py"})
    assert sent["body"] == {"path": "/x.py"}
    assert "/x.py" in out
    assert raised.get("called")  # Issue #95: タブ操作でも VSCode を前面化する


def test_vscode_close_tab_all(ide, monkeypatch):
    sent = _stub_bridge(ide, monkeypatch, response={"closed": "all"})
    out = ide.handle_vscode_close_tab({"close_all": True})
    assert sent["body"] == {"all": True}
    assert "すべてのタブ" in out


def test_vscode_close_tab_requires_target(ide, monkeypatch):
    _stub_bridge(ide, monkeypatch)
    assert "どちらかが必要" in ide.handle_vscode_close_tab({})


# --- terminal override（#48 PR-2）----------------------------------------------


def test_terminal_falls_back_when_no_tmux(ide, monkeypatch):
    monkeypatch.setattr(ide, "_tmux_target_exists", lambda t: False)
    called = {}

    def _fb(args, kw):
        called["fb"] = args
        return "FALLBACK"

    monkeypatch.setattr(ide, "_fallback_terminal", _fb)
    out = ide.handle_terminal({"command": "ls"})
    assert out == "FALLBACK"
    assert called["fb"]["command"] == "ls"


def test_terminal_complex_mode_falls_back(ide, monkeypatch):
    monkeypatch.setattr(ide, "_fallback_terminal", lambda args, kw: "FALLBACK")
    # background/pty/watch_patterns は built-in へ（可視実行の対象外）
    assert ide.handle_terminal({"command": "sleep 100", "background": True}) == "FALLBACK"
    assert ide.handle_terminal({"command": "python", "pty": True}) == "FALLBACK"


def _wire_visible(ide, monkeypatch, tmp_path, output_text, typewriter_command_s=0.0):
    """可視実行の副作用（ヘルパー ready・出力/マーカー生成）を tmp_path でスタブする。

    `tmux send-keys` は subprocess.run 呼び出し列として記録するので、タイプ演出
    （1文字ずつの -l 送信・最後の Enter）の検証にそのまま使える。Enter が送られた
    時点で「実行完了」とみなして出力/マーカーを生成する（実際の tmux ペインで
    Enter が入力を確定させるのと同じタイミング）。二重に Enter が送られたら
    （＝二重実行）assert で落とす。
    """
    out = tmp_path / "out"
    done = tmp_path / "done"
    ready = tmp_path / "ready"
    monkeypatch.setattr(ide, "_TERM_OUT", str(out))
    monkeypatch.setattr(ide, "_TERM_DONE", str(done))
    monkeypatch.setattr(ide, "_TERM_READY", str(ready))
    monkeypatch.setattr(ide, "_tmux_target_exists", lambda t: True)
    monkeypatch.setattr(ide, "_plugin_cfg",
                        lambda: {"typewriter_command_s": typewriter_command_s})
    ready.write_text("")  # ヘルパー定義済み扱い

    calls = []

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[-1] == "Enter":
            assert not done.exists(), "Enter（実行確定）が複数回送られた＝二重実行"
            out.write_text(output_text)
            done.write_text("KAI_EXIT:0\n")
        return _FakeCompleted()

    monkeypatch.setattr(ide.subprocess, "run", _fake_run)
    monkeypatch.setattr(ide.time, "sleep", lambda s: None)
    return calls


def _reconstruct_literal(calls, target):
    """calls（subprocess.run 引数列）から -l で送られた文字列を連結して復元する。"""
    prefix = ["tmux", "send-keys", "-t", target, "-l"]
    return "".join(c[5] for c in calls if c[:5] == prefix)


def test_terminal_visible_run_captures_output(ide, monkeypatch, tmp_path):
    calls = _wire_visible(ide, monkeypatch, tmp_path, "hello\nworld\n",
                          typewriter_command_s=0.04)
    out = ide.handle_terminal({"command": "echo hello"})
    data = __import__("json").loads(out)
    assert data["exit_code"] == 0
    assert "hello" in data["output"] and "world" in data["output"]
    # 視聴者には kai '...' だけが見える（配管が露出しない）。1文字ずつ送られていても
    # 復元すれば同じ文字列で、Enter は最後の1回だけ（＝実行は1回だけ）
    target = ide._DEFAULT_TERM_TARGET
    assert _reconstruct_literal(calls, target) == "kai 'echo hello'"
    assert calls[-1] == ["tmux", "send-keys", "-t", target, "Enter"]
    assert sum(1 for c in calls if c[-1] == "Enter") == 1
    assert not any("tee" in str(c) for c in calls)


def test_terminal_visible_masks_secrets(ide, monkeypatch, tmp_path):
    _wire_visible(ide, monkeypatch, tmp_path, "token=ghp_ABCDEFGHIJKLMNOPQRSTUV\n",
                  typewriter_command_s=0.04)
    out = ide.handle_terminal({"command": "cat .netrc"})
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV" not in out
    assert "«redacted»" in out


def test_terminal_visible_escapes_single_quotes(ide, monkeypatch, tmp_path):
    calls = _wire_visible(ide, monkeypatch, tmp_path, "ok\n", typewriter_command_s=0.04)
    ide.handle_terminal({"command": "echo 'hi there'"})
    # 単一引用符は '\'' にエスケープして kai '...' に包む
    target = ide._DEFAULT_TERM_TARGET
    assert _reconstruct_literal(calls, target) == "kai 'echo '\\''hi there'\\'''"


# --- terminal のタイプライター演出（Issue #96）----------------------------------


def test_terminal_typewriter_disabled_sends_bulk(ide, monkeypatch, tmp_path):
    # typewriter_command_s=0 は演出オフ：1回の -l 一括送信 + Enter
    calls = _wire_visible(ide, monkeypatch, tmp_path, "ok\n", typewriter_command_s=0)
    ide.handle_terminal({"command": "echo hello"})
    target = ide._DEFAULT_TERM_TARGET
    assert calls == [
        ["tmux", "send-keys", "-t", target, "-l", "kai 'echo hello'"],
        ["tmux", "send-keys", "-t", target, "Enter"],
    ]


def test_tmux_send_typed_char_by_char(ide, monkeypatch):
    calls = []
    monkeypatch.setattr(ide.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeCompleted())
    sleeps = []
    monkeypatch.setattr(ide.time, "sleep", lambda s: sleeps.append(s))
    ide._tmux_send_typed("kai-term", "echo hi", 0.04)
    literal_calls = calls[:-1]
    assert calls[-1] == ["tmux", "send-keys", "-t", "kai-term", "Enter"]
    assert len(literal_calls) == len("echo hi")
    assert all(c[:5] == ["tmux", "send-keys", "-t", "kai-term", "-l"] for c in literal_calls)
    assert all(len(c[5]) == 1 for c in literal_calls)  # 1文字ずつ
    assert "".join(c[5] for c in literal_calls) == "echo hi"
    assert len(sleeps) == len("echo hi")
    assert all(s == pytest.approx(0.04) for s in sleeps)


def test_tmux_send_typed_interval_zero_is_bulk(ide, monkeypatch):
    calls = []
    monkeypatch.setattr(ide.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeCompleted())
    ide._tmux_send_typed("kai-term", "echo hi", 0.0)
    assert calls == [
        ["tmux", "send-keys", "-t", "kai-term", "-l", "echo hi"],
        ["tmux", "send-keys", "-t", "kai-term", "Enter"],
    ]


def test_tmux_send_typed_multiline_sends_bulk(ide, monkeypatch):
    # ヒアドキュメント等の複数行コマンドは演出せず一括送信
    calls = []
    monkeypatch.setattr(ide.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeCompleted())
    sleeps = []
    monkeypatch.setattr(ide.time, "sleep", lambda s: sleeps.append(s))
    ide._tmux_send_typed("kai-term", "echo a\necho b", 0.04)
    assert calls == [
        ["tmux", "send-keys", "-t", "kai-term", "-l", "echo a\necho b"],
        ["tmux", "send-keys", "-t", "kai-term", "Enter"],
    ]
    assert sleeps == []


def test_tmux_send_typed_caps_total_time_for_long_command(ide, monkeypatch):
    # 0.04*200=8s > 上限2.5s のはずなので、間隔を詰めて上限内に収める
    calls = []
    sleeps = []
    monkeypatch.setattr(ide.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeCompleted())
    monkeypatch.setattr(ide.time, "sleep", lambda s: sleeps.append(s))
    command = "x" * 200
    ide._tmux_send_typed("kai-term", command, 0.04)
    # まだ1文字ずつ送っている（間隔を詰めるだけで一括送信への切替は起きない）
    assert len(sleeps) == len(command)
    assert all(s < 0.04 for s in sleeps)  # 間隔が詰まっている
    assert sum(sleeps) <= ide._TYPEWRITER_CAP_S + 1e-9
    assert calls[-1] == ["tmux", "send-keys", "-t", "kai-term", "Enter"]


def test_tmux_send_typed_extremely_long_switches_to_bulk(ide, monkeypatch):
    # 間隔を詰めても最低間隔を下回るほど長いコマンドは一括送信に切り替える
    calls = []
    monkeypatch.setattr(ide.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)) or _FakeCompleted())
    sleeps = []
    monkeypatch.setattr(ide.time, "sleep", lambda s: sleeps.append(s))
    command = "x" * 5000
    ide._tmux_send_typed("kai-term", command, 0.04)
    assert calls == [
        ["tmux", "send-keys", "-t", "kai-term", "-l", command],
        ["tmux", "send-keys", "-t", "kai-term", "Enter"],
    ]
    assert sleeps == []


def test_typewriter_interval_reads_config(ide, monkeypatch):
    monkeypatch.setattr(ide, "_plugin_cfg", lambda: {"typewriter_command_s": 0.02})
    assert ide._typewriter_interval_s() == 0.02


def test_typewriter_interval_defaults_when_unset(ide, monkeypatch):
    monkeypatch.setattr(ide, "_plugin_cfg", lambda: {})
    assert ide._typewriter_interval_s() == ide._DEFAULT_TYPEWRITER_S


# --- write_file / patch override（#49 PR-3）------------------------------------


def test_extract_patch_edits(ide):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /a/broadcast.sh\n@@\n-old\n+new\n"
        "*** Add File: /a/new.md\n+content\n*** End Patch"
    )
    edits = ide._extract_patch_edits(patch)
    assert edits == [
        {"path": "/a/broadcast.sh", "action": "update"},
        {"path": "/a/new.md", "action": "add"},
    ]


def test_write_file_writes_and_notifies_update(ide, monkeypatch, tmp_path):
    existing = tmp_path / "a.py"
    existing.write_text("old")
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"success": true}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    out = ide.handle_write_file({"path": str(existing), "content": "new"})
    assert "success" in out
    assert notified["e"] == [{"path": str(existing), "action": "update"}]


def test_write_file_new_file_notifies_add(ide, monkeypatch, tmp_path):
    newp = tmp_path / "brand-new.py"  # 存在しない
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"success": true}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    ide.handle_write_file({"path": str(newp), "content": "x"})
    assert notified["e"] == [{"path": str(newp), "action": "add"}]


def test_write_file_no_notify_on_error(ide, monkeypatch, tmp_path):
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"error": "permission denied"}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    out = ide.handle_write_file({"path": str(tmp_path / "x.py"), "content": "x"})
    assert "error" in out
    assert "e" not in notified  # 書込失敗時はタイプ表示しない


def test_patch_applies_and_notifies(ide, monkeypatch):
    patch = "*** Begin Patch\n*** Update File: /a/x.py\n@@\n-a\n+b\n*** End Patch"
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"success": true}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    ide.handle_patch({"mode": "patch", "patch": patch})
    assert notified["e"] == [{"path": "/a/x.py", "action": "update"}]


def test_patch_replace_mode_notifies_path(ide, monkeypatch):
    # 既定の replace モード（path + old/new）でも /edit 通知が飛ぶ（#62 タイプライター）
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"success": true}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    ide.handle_patch({"path": "/a/doc.md", "old_string": "x", "new_string": "y"})
    assert notified["e"] == [{"path": "/a/doc.md", "action": "update"}]


def test_patch_replace_default_mode_notifies(ide, monkeypatch):
    # mode 省略時は replace 扱い
    monkeypatch.setattr(ide, "_builtin_file_handler",
                        lambda name: lambda args, **kw: '{"ok": true}')
    notified = {}
    monkeypatch.setattr(ide, "_notify_edit", lambda edits: notified.setdefault("e", edits))
    ide.handle_patch({"path": "/a/z.py", "old_string": "a", "new_string": "b"})
    assert notified.get("e") == [{"path": "/a/z.py", "action": "update"}]


def test_notify_edit_swallows_bridge_failure(ide, monkeypatch):
    def _boom(method, path, body=None, timeout=3.0):
        raise RuntimeError("bridge down")
    monkeypatch.setattr(ide, "_bridge_request", _boom)
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: None)
    ide._notify_edit([{"path": "/a.py", "action": "update"}])  # raise しないこと


def test_notify_edit_raises_vscode_window(ide, monkeypatch):
    # Issue #95: write_file/patch で編集通知したら VSCode を前面化する
    monkeypatch.setattr(ide, "_bridge_request", lambda m, p, body=None, timeout=3.0: {})
    raised = {}
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: raised.setdefault("called", True))
    ide._notify_edit([{"path": "/a.py", "action": "update"}])
    assert raised.get("called")


def test_notify_edit_raises_vscode_window_even_if_bridge_fails(ide, monkeypatch):
    # ブリッジ（拡張）が不在でも VSCode ウィンドウ自体は前面化を試みてよい
    # （前面化は wmctrl 側で best-effort に失敗するので、ここで止める必要はない）
    def _boom(method, path, body=None, timeout=3.0):
        raise RuntimeError("bridge down")
    monkeypatch.setattr(ide, "_bridge_request", _boom)
    raised = {}
    monkeypatch.setattr(ide, "_raise_vscode_window", lambda: raised.setdefault("called", True))
    ide._notify_edit([{"path": "/a.py", "action": "update"}])
    assert raised.get("called")


def test_notify_edit_absolutizes_relative_path(ide, monkeypatch):
    # 拡張の /edit は絶対パスしか受けない → 相対パスは絶対化して送る（#62）
    sent = {}
    monkeypatch.setattr(ide, "_bridge_request",
                        lambda m, p, body=None, timeout=3.0: sent.update(body or {}))
    ide._notify_edit([{"path": "README.md", "action": "update"}])
    assert sent["edits"][0]["path"].startswith("/")
    assert sent["edits"][0]["path"].endswith("/README.md")


def test_notify_edit_keeps_absolute_path(ide, monkeypatch):
    sent = {}
    monkeypatch.setattr(ide, "_bridge_request",
                        lambda m, p, body=None, timeout=3.0: sent.update(body or {}))
    ide._notify_edit([{"path": "/home/kai/x.py", "action": "add"}])
    assert sent["edits"] == [{"path": "/home/kai/x.py", "action": "add"}]


# --- _raise_vscode_window（#95: ウィンドウ前面化）-------------------------------


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_raise_vscode_window_activates_matching_window(ide, monkeypatch):
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["wmctrl", "-lx"]:
            return _FakeCompleted(
                stdout="0x01 0 firefox.Firefox hostname\n"
                       "0x02 0 code.Code hostname\n")
        return _FakeCompleted()

    monkeypatch.setattr(ide.subprocess, "run", _fake_run)
    ide._raise_vscode_window()
    assert ["wmctrl", "-i", "-a", "0x02"] in calls
    # 最大化はしない（要求は raise のみ）
    assert not any("-b" in c for c in calls)


def test_raise_vscode_window_no_match_does_nothing(ide, monkeypatch):
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["wmctrl", "-lx"]:
            return _FakeCompleted(stdout="0x01 0 firefox.Firefox hostname\n")
        return _FakeCompleted()

    monkeypatch.setattr(ide.subprocess, "run", _fake_run)
    ide._raise_vscode_window()
    assert calls == [["wmctrl", "-lx"]]  # -a は呼ばれない


def test_raise_vscode_window_no_wmctrl_is_silent(ide, monkeypatch):
    def _fake_run(cmd, **kw):
        raise FileNotFoundError("wmctrl not found")

    monkeypatch.setattr(ide.subprocess, "run", _fake_run)
    ide._raise_vscode_window()  # 例外を投げないこと


def test_raise_vscode_window_wmctrl_error_is_silent(ide, monkeypatch):
    monkeypatch.setattr(ide.subprocess, "run", lambda cmd, **kw: _FakeCompleted(returncode=1))
    ide._raise_vscode_window()  # 例外を投げないこと


# --- register -------------------------------------------------------------------


def test_register_registers_state_tools_and_terminal_override(ide, monkeypatch):
    registered = []

    class _Ctx:
        def register_tool(self, **kwargs):
            registered.append((kwargs["name"], kwargs.get("override", False)))

    ide.register(_Ctx())
    names = {n for n, _ in registered}
    assert {"vscode_state", "vscode_open", "vscode_close_tab"} <= names
    # terminal/write_file/patch は override=True で登録（schema が import できる環境のみ）
    for tool in ("terminal", "write_file", "patch"):
        ov = [o for n, o in registered if n == tool]
        if ov:  # import 可能な環境では override 登録される
            assert ov[0] is True

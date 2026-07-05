"""Tests for the kai_director plugin (plugins/kai_director/).

編集系ツール（write_file / patch）の対象ファイル抽出と、hook → キュー →
HTTP 通知の流れ（HTTP はスタブ）を検証する。
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "kai_director"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kai_director",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kai_director"
    sys.modules["hermes_plugins.kai_director"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def director_mod(monkeypatch):
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_plugin_cfg", lambda: {})
    return mod


# --- extract_edited_files --------------------------------------------------------


def test_extract_write_file_dict(director_mod):
    files = director_mod.extract_edited_files("write_file", {"path": "/tmp/a.py", "content": "x"})
    assert files == ["/tmp/a.py"]


def test_extract_write_file_json_string(director_mod):
    # トレース実測: args は JSON 文字列で渡ってくることがある
    files = director_mod.extract_edited_files("write_file", '{"path": "/tmp/b.sh"}')
    assert files == ["/tmp/b.sh"]


def test_extract_patch_update_and_add(director_mod):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /home/kai/kai-agent/kai-services/streaming/vm/broadcast.sh\n"
        "@@\n-old\n+new\n"
        "*** Add File: /home/kai/kai-agent/docs/kai/new.md\n"
        "+content\n"
        "*** End Patch"
    )
    files = director_mod.extract_edited_files("patch", {"mode": "patch", "patch": patch})
    assert files == [
        "/home/kai/kai-agent/kai-services/streaming/vm/broadcast.sh",
        "/home/kai/kai-agent/docs/kai/new.md",
    ]


def test_extract_ignores_other_tools_and_garbage(director_mod):
    assert director_mod.extract_edited_files("terminal", {"command": "ls"}) == []
    assert director_mod.extract_edited_files("write_file", "not json") == []
    assert director_mod.extract_edited_files("patch", {"patch": 123}) == []


# --- hook → queue ----------------------------------------------------------------


def test_hook_pushes_edit(director_mod, monkeypatch):
    d = director_mod._Director(start_thread=False)
    monkeypatch.setattr(director_mod, "_director", d)
    director_mod._on_post_tool_call(
        tool_name="write_file", args={"path": "/tmp/a.py"}, status="ok")
    item = d._q.get_nowait()
    assert item == {"files": ["/tmp/a.py"], "tool": "write_file"}


def test_hook_skips_failed_edit_and_other_tools(director_mod, monkeypatch):
    d = director_mod._Director(start_thread=False)
    monkeypatch.setattr(director_mod, "_director", d)
    director_mod._on_post_tool_call(
        tool_name="write_file", args={"path": "/tmp/a.py"}, status="error")
    director_mod._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, status="ok")
    assert d._q.empty()


def test_disabled_by_config(director_mod, monkeypatch):
    monkeypatch.setattr(director_mod, "_plugin_cfg", lambda: {"enabled": False})
    d = director_mod._Director(start_thread=False)
    d.push_edit(["/tmp/a.py"], "write_file")
    assert d._q.empty()


# --- コマンドログ（Issue #30）----------------------------------------------------


def test_extract_command(director_mod):
    assert director_mod.extract_command({"command": "git status"}) == "git status"
    assert director_mod.extract_command('{"command": "ls -la"}') == "ls -la"
    assert director_mod.extract_command({"path": "a.py"}) == ""


def test_log_command_writes_masked_line(director_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(director_mod, "_plugin_cfg",
                        lambda: {"command_log": str(tmp_path / "cmdlog")})
    d = director_mod._Director(start_thread=False)
    d.log_command("gh auth login --with-token ghp_ABCDEFGHIJKLMNOPQRSTUV")
    content = (tmp_path / "cmdlog").read_text()
    assert content.startswith("$ gh auth login")
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV" not in content  # マスク済み
    assert "«redacted»" in content


def test_log_result_only_on_failure_or_slow(director_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(director_mod, "_plugin_cfg",
                        lambda: {"command_log": str(tmp_path / "cmdlog")})
    d = director_mod._Director(start_thread=False)
    d.log_result(status="ok", duration_ms=100)  # 何も書かない
    assert not (tmp_path / "cmdlog").exists()
    d.log_result(status="error")
    d.log_result(status="ok", duration_ms=5000)
    lines = (tmp_path / "cmdlog").read_text().splitlines()
    assert any("✗ error" in ln for ln in lines)
    assert any("5s" in ln for ln in lines)


def test_pre_hook_logs_terminal_command(director_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(director_mod, "_plugin_cfg",
                        lambda: {"command_log": str(tmp_path / "cmdlog")})
    d = director_mod._Director(start_thread=False)
    monkeypatch.setattr(director_mod, "_director", d)
    director_mod._on_pre_tool_call(tool_name="terminal", args={"command": "npm test"})
    director_mod._on_pre_tool_call(tool_name="read_file", args={"path": "a.py"})  # 非terminalは無視
    content = (tmp_path / "cmdlog").read_text()
    assert "$ npm test" in content
    assert "a.py" not in content


def test_command_log_disabled_by_config(director_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(director_mod, "_plugin_cfg",
                        lambda: {"enabled": False, "command_log": str(tmp_path / "cmdlog")})
    d = director_mod._Director(start_thread=False)
    d.log_command("ls")
    assert not (tmp_path / "cmdlog").exists()


# --- notify（HTTP スタブ）---------------------------------------------------------


def test_notify_posts_json(director_mod, monkeypatch):
    sent = {}

    class _Resp:
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["body"] = req.data
        return _Resp()

    monkeypatch.setattr(director_mod.urllib.request, "urlopen", _fake_urlopen)
    d = director_mod._Director(start_thread=False)
    d._notify({"files": ["/tmp/a.py"], "tool": "patch"})
    assert sent["url"] == "http://127.0.0.1:8920/edit"
    assert b"/tmp/a.py" in sent["body"]

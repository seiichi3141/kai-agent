"""Issue #95: stream-browser で開くウィンドウの最大化・前面化の回帰。

wmctrl/xdotool 呼び出しは subprocess.run をスタブして検証する（実 X 不要）。
wmctrl 不在・失敗時は静かに何もしない（配信装飾の失敗で open 自体は止めない）。
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sb(monkeypatch):
    monkeypatch.delenv("STREAM_BROWSER_ALLOW", raising=False)
    return _load("stream_browser_window_test", "kai-services/streaming/vm/stream-browser.py")


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


# --- _wmctrl_find_window ---------------------------------------------------------


def test_find_window_matches_chromium(sb, monkeypatch):
    def _fake_run(cmd, **kw):
        assert cmd == ["wmctrl", "-lx"]
        return _FakeCompleted(
            stdout="0x01 0 code.Code host\n"
                   "0x02 0 chromium.Chromium host\n")

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    assert sb._wmctrl_find_window("chromium") == "0x02"


def test_find_window_no_match_returns_none(sb, monkeypatch):
    monkeypatch.setattr(
        sb.subprocess, "run",
        lambda cmd, **kw: _FakeCompleted(stdout="0x01 0 code.Code host\n"))
    assert sb._wmctrl_find_window("chromium") is None


def test_find_window_wmctrl_missing_returns_none(sb, monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("wmctrl not found")

    monkeypatch.setattr(sb.subprocess, "run", _boom)
    assert sb._wmctrl_find_window("chromium") is None  # 例外を投げないこと


def test_find_window_wmctrl_error_returns_none(sb, monkeypatch):
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **kw: _FakeCompleted(returncode=1))
    assert sb._wmctrl_find_window("chromium") is None


# --- _raise_and_maximize ---------------------------------------------------------


def test_raise_and_maximize_activates_and_maximizes(sb, monkeypatch):
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["wmctrl", "-lx"]:
            return _FakeCompleted(stdout="0x02 0 chromium.Chromium host\n")
        return _FakeCompleted()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    sb._raise_and_maximize("chromium")
    assert ["wmctrl", "-i", "-r", "0x02", "-b", "add,maximized_vert,maximized_horz"] in calls
    assert ["wmctrl", "-i", "-a", "0x02"] in calls


def test_raise_and_maximize_no_window_is_noop(sb, monkeypatch):
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _FakeCompleted(stdout="")

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    sb._raise_and_maximize("chromium")
    assert calls == [["wmctrl", "-lx"]]  # -r / -a は呼ばれない


def test_raise_and_maximize_silent_when_wmctrl_missing(sb, monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("wmctrl not found")

    monkeypatch.setattr(sb.subprocess, "run", _boom)
    sb._raise_and_maximize("chromium")  # 例外を投げないこと


# --- cmd_open がウィンドウ前面化・最大化を呼ぶこと --------------------------------


class _FakeCDP:
    def __init__(self):
        self.calls = []
        self.closed = False

    def call(self, method, params=None):
        self.calls.append((method, params))
        return {}

    def close(self):
        self.closed = True


def test_cmd_open_raises_and_maximizes_browser(sb, monkeypatch, capsys):
    monkeypatch.setattr(sb, "_browser_running", lambda: True)
    fake_cdp = _FakeCDP()
    monkeypatch.setattr(sb, "_connect_page", lambda: fake_cdp)
    raised = {}
    monkeypatch.setattr(sb, "_raise_and_maximize", lambda cls: raised.setdefault("class", cls))

    sb.cmd_open("https://github.com/HyuCode/kai-agent/issues/95")

    assert raised.get("class") == sb._BROWSER_WM_CLASS
    assert fake_cdp.closed
    assert "開いた" in capsys.readouterr().out


def test_cmd_open_denied_url_does_not_raise_window(sb, monkeypatch):
    raised = {}
    monkeypatch.setattr(sb, "_raise_and_maximize", lambda cls: raised.setdefault("class", cls))
    with pytest.raises(SystemExit):
        sb.cmd_open("https://evil.example.com/phish")
    assert "class" not in raised

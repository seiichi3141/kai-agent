"""Tests for the kai_narrator plugin (plugins/kai_narrator/).

Covers:
  * secret masking (_mask)
  * response → speech text conversion (_speechify_response)
  * tool-event digests (_digest_event)
  * worker behaviours: response speech, dedup, stale-event clearing,
    narration rate limiting and priority (all with stubbed HTTP / LLM)
  * hook callbacks enqueue and return immediately
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_plugin():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "kai_narrator"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kai_narrator",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kai_narrator"
    sys.modules["hermes_plugins.kai_narrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def narrator_mod(monkeypatch):
    mod = _load_plugin()
    monkeypatch.setattr(mod, "_plugin_cfg", lambda: {})
    return mod


@pytest.fixture()
def narrator(narrator_mod):
    return narrator_mod._Narrator(start_thread=False)


# --- masking -----------------------------------------------------------------


def test_mask_token_patterns(narrator_mod):
    masked = narrator_mod._mask("key is sk-abcdefghijklmnopqrstuvwx and ghp_ABCDEFGHIJKLMNOPQRSTUV")
    assert "sk-abcdefghijklmnop" not in masked
    assert "ghp_" not in masked
    assert "«redacted»" in masked


def test_mask_env_secret(narrator_mod, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "supersecretvalue123")
    monkeypatch.setattr(narrator_mod, "_ENV_SECRETS", narrator_mod._collect_env_secrets())
    assert "supersecretvalue123" not in narrator_mod._mask("value: supersecretvalue123")


# --- response speechification --------------------------------------------------


def test_speechify_strips_code_and_markdown(narrator_mod):
    text = "テストを直したよ。\n\n```python\nprint('secret code')\n```\n\n- `pytest` は **green** です"
    out = narrator_mod._speechify_response(text, 280)
    assert "print" not in out
    assert "```" not in out
    assert "**" not in out
    assert "テストを直したよ。" in out
    assert "pytest" in out


def test_speechify_truncates_at_sentence_boundary(narrator_mod):
    text = "一文目です。" * 100
    out = narrator_mod._speechify_response(text, 60)
    assert len(out) <= 60
    assert out.endswith("。")


def test_speechify_empty(narrator_mod):
    assert narrator_mod._speechify_response("", 100) == ""


# --- event digests -------------------------------------------------------------


def test_digest_event_terminal_command(narrator_mod):
    d = narrator_mod._digest_event({
        "tool": "terminal",
        "args": {"command": "pytest tests/agent -x"},
        "status": "ok",
    })
    assert d.startswith("terminal")
    assert "pytest tests/agent -x" in d
    assert "status=" not in d  # ok は省略


def test_digest_event_error_and_masking(narrator_mod):
    d = narrator_mod._digest_event({
        "tool": "terminal",
        "args": {"command": "curl -H 'Authorization: Bearer abcdef123456789xyz'"},
        "status": "error",
        "error_message": "boom",
    })
    assert "status=error" in d
    assert "error: boom" in d
    assert "abcdef123456789xyz" not in d


# --- worker: response speech ---------------------------------------------------


def test_handle_response_posts_to_speechd(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator._handle_response({"kind": "response", "text": "PR を作ったよ。", "session_id": "s1"})
    assert len(sent) == 1
    assert sent[0]["text"] == "PR を作ったよ。"
    assert sent[0]["source"] == "agent_response"
    assert sent[0]["priority"] == "normal"
    assert sent[0]["session_id"] == "s1"


def test_handle_response_dedup(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator._handle_response({"kind": "response", "text": "同じ発話"})
    narrator._handle_response({"kind": "response", "text": "同じ発話"})
    assert len(sent) == 1


def test_handle_response_clears_pending_events(narrator_mod, narrator, monkeypatch):
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: None)
    narrator.push_tool_event({"tool": "terminal", "args": {}})
    narrator._handle_response({"kind": "response", "text": "完了"})
    assert len(narrator._events) == 0


def test_say_swallows_speechd_failure(narrator_mod, narrator, monkeypatch):
    def _boom(url, payload, timeout=3.0):
        raise OSError("unreachable")
    monkeypatch.setattr(narrator_mod, "_post_say", _boom)
    narrator._handle_response({"kind": "response", "text": "配信なしでも落ちない"})  # raise しないこと
    assert narrator._last_text == ""  # 送れていないので dedup 状態は更新されない


# --- worker: narration ----------------------------------------------------------


def test_maybe_narrate_generates_and_posts_low_priority(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events: "テストを実行中だよ")
    narrator.push_tool_event({"tool": "terminal", "args": {"command": "pytest"}, "session_id": "s1"})
    narrator._maybe_narrate()
    assert len(sent) == 1
    assert sent[0]["source"] == "narrator"
    assert sent[0]["priority"] == "low"
    assert sent[0]["session_id"] == "s1"
    assert len(narrator._events) == 0  # 消費済み


def test_maybe_narrate_respects_min_interval(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events: "一回目")
    narrator.push_tool_event({"tool": "a", "args": {}})
    narrator._maybe_narrate()
    # 直後はインターバル未経過なので実況しない
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events: "二回目")
    narrator.push_tool_event({"tool": "b", "args": {}})
    narrator._maybe_narrate()
    assert len(sent) == 1


def test_maybe_narrate_noop_without_events(narrator_mod, narrator, monkeypatch):
    called = []
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events: called.append(1) or "x")
    narrator._maybe_narrate()
    assert not called


def test_maybe_narrate_disabled(narrator_mod, monkeypatch):
    monkeypatch.setattr(narrator_mod, "_plugin_cfg", lambda: {"narration_enabled": False})
    n = narrator_mod._Narrator(start_thread=False)
    called = []
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events: called.append(1) or "x")
    n.push_tool_event({"tool": "a", "args": {}})
    n._maybe_narrate()
    assert not called


def test_maybe_narrate_swallows_llm_failure(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))

    def _boom(events):
        raise RuntimeError("llm down")
    monkeypatch.setattr(narrator_mod, "_generate_narration", _boom)
    narrator.push_tool_event({"tool": "a", "args": {}})
    narrator._maybe_narrate()  # raise しないこと
    assert not sent


# --- atexit drain ----------------------------------------------------------------


def test_drain_at_exit_sends_pending_response(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator.push_response("最後の完了報告", session_id="s1")
    narrator._drain_at_exit()
    assert len(sent) == 1
    assert sent[0]["text"] == "最後の完了報告"
    assert sent[0]["source"] == "agent_response"


def test_drain_at_exit_noop_when_empty(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator._drain_at_exit()
    assert not sent


# --- hooks ----------------------------------------------------------------------


def test_hooks_enqueue(narrator_mod, monkeypatch):
    n = narrator_mod._Narrator(start_thread=False)
    monkeypatch.setattr(narrator_mod, "_narrator", n)
    narrator_mod._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1")
    assert len(n._events) == 1
    narrator_mod._on_post_llm_call(session_id="s1", assistant_response="やったよ")
    item = n._q.get_nowait()
    assert item["kind"] == "response"
    assert item["text"] == "やったよ"
    # 空応答は積まない
    narrator_mod._on_post_llm_call(session_id="s1", assistant_response="")
    assert n._q.empty()


def test_session_start_clears_events(narrator_mod, monkeypatch):
    n = narrator_mod._Narrator(start_thread=False)
    monkeypatch.setattr(narrator_mod, "_narrator", n)
    n.push_tool_event({"tool": "a", "args": {}})
    narrator_mod._on_session_start(session_id="new")
    assert len(n._events) == 0

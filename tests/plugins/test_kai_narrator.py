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
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "テストを実行中だよ")
    narrator.push_tool_event({"tool": "terminal", "args": {"command": "pytest"}, "session_id": "s1"})
    narrator._maybe_narrate()
    assert len(sent) == 1
    assert sent[0]["source"] == "narrator"
    assert sent[0]["priority"] == "low"
    assert sent[0]["session_id"] == "s1"
    assert len(narrator._events) == 0  # 消費済み


def test_maybe_narrate_respects_min_interval(narrator_mod, narrator, monkeypatch):
    import time as _time
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "一回目")
    # 非旗艦イベント（ただの read）。last_say=0 なので一回目は間隔経過扱いで実況
    narrator.push_tool_event({"tool": "read_file", "args": {"path": "a.py"}})
    narrator._maybe_narrate()
    assert len(sent) == 1
    # 直後（インターバル未経過）は非旗艦イベントなら実況しない
    narrator._last_say_ts = _time.monotonic()
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "二回目")
    narrator.push_tool_event({"tool": "read_file", "args": {"path": "b.py"}})
    narrator._maybe_narrate()
    assert len(sent) == 1  # 間隔で抑制。イベントは溜めておく
    assert len(narrator._events) == 1


def test_heartbeat_no_filler_before_first_tool(narrator_mod, narrator, monkeypatch):
    # 冒頭（ツール未実行）では「考え中」フィラーを喋らない。最初の発話を実作業由来に
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator.heartbeat_enabled = True
    narrator._last_say_ts = 0.0  # 間隔は経過扱い
    narrator.set_thinking(True)  # 思考中
    narrator._maybe_heartbeat()
    assert sent == []  # まだツール未実行 → フィラー出さない
    # ツールを1回実行したあとの思考中はフィラーを出す（作業中の間つなぎは許容）
    narrator.push_tool_event({"tool": "read_file", "args": {"path": "a.py"}})
    narrator._maybe_heartbeat()
    assert len(sent) == 1 and sent[0]["source"] == "narrator"


def test_maybe_narrate_flagship_bypasses_interval(narrator_mod, narrator, monkeypatch):
    import time as _time
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "テスト通ったよ")
    narrator._last_say_ts = _time.monotonic()  # 直前に発話済み（間隔未経過）
    # verify.sh は旗艦イベント → 間隔を無視して即実況
    narrator.push_tool_event({"tool": "terminal", "args": {"command": "scripts/kai/verify.sh"}})
    narrator._maybe_narrate()
    assert len(sent) == 1


def test_maybe_narrate_error_is_flagship(narrator_mod, narrator, monkeypatch):
    import time as _time
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "エラー出たよ")
    narrator._last_say_ts = _time.monotonic()
    narrator.push_tool_event({"tool": "terminal", "args": {"command": "ls"}, "status": "error"})
    narrator._maybe_narrate()
    assert len(sent) == 1  # エラーは間隔無視で即報告


def test_maybe_narrate_skip_is_not_spoken(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: "SKIP")
    narrator.push_tool_event({"tool": "read_file", "args": {"path": "a.py"}})
    narrator._maybe_narrate()
    assert not sent  # SKIP は発話しない
    assert len(narrator._recent_narrations) == 0


def test_maybe_narrate_passes_context_and_recent(narrator_mod, narrator, monkeypatch):
    seen = {}
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: None)

    def _gen(events, context="", recent=None):
        seen["context"] = context
        seen["recent"] = list(recent or [])
        return "いま README を書いてるよ、みんなに使い方を伝えたくて"
    monkeypatch.setattr(narrator_mod, "_generate_narration", _gen)
    narrator._context = "Issue #25 をやってるよ"
    narrator._recent_narrations.append("さっきの実況")
    narrator.push_tool_event({"tool": "write_file", "args": {"path": "README.md"}})
    narrator._maybe_narrate()
    assert seen["context"] == "Issue #25 をやってるよ"
    assert seen["recent"] == ["さっきの実況"]
    # 実況したテキストは recent に積まれる（次回の繰り返し判定に使う）
    assert narrator._recent_narrations[-1].startswith("いま README")


def test_maybe_narrate_noop_without_events(narrator_mod, narrator, monkeypatch):
    called = []
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: called.append(1) or "x")
    narrator._maybe_narrate()
    assert not called


def test_maybe_narrate_disabled(narrator_mod, monkeypatch):
    monkeypatch.setattr(narrator_mod, "_plugin_cfg", lambda: {"narration_enabled": False})
    n = narrator_mod._Narrator(start_thread=False)
    called = []
    monkeypatch.setattr(narrator_mod, "_generate_narration", lambda events, **kw: called.append(1) or "x")
    n.push_tool_event({"tool": "a", "args": {}})
    n._maybe_narrate()
    assert not called


def test_maybe_narrate_swallows_llm_failure(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))

    def _boom(events, **kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(narrator_mod, "_generate_narration", _boom)
    narrator.push_tool_event({"tool": "a", "args": {}})
    narrator._maybe_narrate()  # raise しないこと
    assert not sent


def test_is_flagship_and_is_skip(narrator_mod):
    assert narrator_mod._is_flagship({"tool": "terminal", "args": {"command": "git commit -m x"}})
    assert narrator_mod._is_flagship({"tool": "terminal", "args": {"command": "gh pr create"}})
    assert narrator_mod._is_flagship({"tool": "terminal", "args": {"command": "ls"}, "status": "error"})
    assert not narrator_mod._is_flagship({"tool": "read_file", "args": {"path": "a.py"}})
    assert narrator_mod._is_skip("SKIP")
    assert narrator_mod._is_skip("skip。")
    assert not narrator_mod._is_skip("テスト通ったよ")


def test_handle_response_captures_context(narrator_mod, narrator, monkeypatch):
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: None)
    narrator._handle_response({"kind": "response", "text": "Issue #25 の実装を始めるよ"})
    assert "Issue #25" in narrator._context


# --- path shortening（Issue #9: フルパスを流さない）------------------------------


def test_shorten_paths_to_basename(narrator_mod):
    out = narrator_mod._shorten_paths(
        "/home/kai/kai-agent/kai-services/streaming/vm/broadcast.sh を編集して "
        "~/kai-agent/docs/kai/design/00-system.md も見る"
    )
    assert out == "broadcast.sh を編集して 00-system.md も見る"


def test_shorten_paths_keeps_urls_and_short_refs(narrator_mod):
    # URL は巻き込まない（ドメイン直後の / 連続はパスと区別する）
    text = "https://github.com/seiichi3141/kai-agent を開く"
    assert narrator_mod._shorten_paths(text) == text
    # スラッシュ 1 個の相対参照（tests/agent 等）は読める範囲なので保持
    assert narrator_mod._shorten_paths("tests/agent を実行") == "tests/agent を実行"


def test_digest_args_shortens_paths(narrator_mod):
    d = narrator_mod._digest_args({"file_path": "/home/kai/kai-agent/plugins/kai_narrator/__init__.py"})
    assert d == "__init__.py"


def test_speechify_shortens_paths(narrator_mod):
    out = narrator_mod._speechify_response("`/home/kai/kai-agent/scripts/kai/verify.sh` を回したよ", 280)
    assert "verify.sh を回したよ" == out


def test_generate_narration_output_shortens_paths(narrator_mod, monkeypatch):
    # LLM がプロンプト指示を無視してパスを出しても機械的に短縮される
    class _Resp:
        pass

    def _fake_call_llm(**kwargs):
        return _Resp()

    fake_aux = types.ModuleType("agent.auxiliary_client")
    fake_aux.call_llm = lambda **kwargs: _Resp()
    fake_aux.extract_content_or_reasoning = (
        lambda resp: "いま /home/kai/kai-agent/kai-services/speechd/speechd.py を直してるよ"
    )
    fake_agent = types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)
    out = narrator_mod._generate_narration([{"tool": "editor", "args": {}}])
    assert out == "いま speechd.py を直してるよ"


# --- worker: heartbeat（無音対策 Issue #10）--------------------------------------


def test_heartbeat_narrates_running_tool(narrator_mod, narrator, monkeypatch):
    sent = []
    seen_events = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    monkeypatch.setattr(narrator_mod, "_generate_narration",
                        lambda events, **kw: seen_events.extend(events) or "CIの完了を待ってるよ")
    narrator.set_tool_running("terminal", {"command": "verify.sh --pr"}, session_id="s1")
    narrator._maybe_heartbeat()  # _last_say_ts=0 なのでインターバル経過扱い
    assert len(sent) == 1
    assert sent[0]["source"] == "narrator"
    assert sent[0]["priority"] == "low"
    assert sent[0]["session_id"] == "s1"
    assert seen_events[0]["status"] == "running"
    assert seen_events[0]["tool"] == "terminal"


def test_heartbeat_falls_back_to_template_on_llm_failure(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))

    def _boom(events):
        raise RuntimeError("llm down")
    monkeypatch.setattr(narrator_mod, "_generate_narration", _boom)
    narrator.set_tool_running("terminal", {"command": "sleep"})
    narrator._maybe_heartbeat()
    assert len(sent) == 1  # LLM 不達でも無音を避ける（定型文）
    assert "terminal" in sent[0]["text"]


def test_heartbeat_idle_lines_rotate_while_thinking(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    # 冒頭フィラー抑制のため、まず1回ツールを実行した状態にする（作業中の思考中）
    narrator.push_tool_event({"tool": "read_file", "args": {"path": "a.py"}})
    narrator._events.clear()  # イベントは消費済み扱い（heartbeat の思考分岐に入れる）
    narrator.set_thinking(True)
    narrator._maybe_heartbeat()
    narrator._last_say_ts = 0.0  # 次のインターバル経過を装う
    narrator._last_text = ""
    narrator._maybe_heartbeat()
    assert len(sent) == 2
    assert sent[0]["text"] != sent[1]["text"]  # ローテーションで同文を避ける


def test_heartbeat_silent_when_nothing_running(narrator_mod, narrator, monkeypatch):
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator._maybe_heartbeat()  # 実行中ツールなし・思考中でもない
    assert not sent  # アイドルプロセスで喋り続けない


def test_heartbeat_respects_interval(narrator_mod, narrator, monkeypatch):
    import time as _time
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    narrator.set_tool_running("terminal", {"command": "x"})
    narrator._last_say_ts = _time.monotonic()  # いま発話したばかり
    narrator._maybe_heartbeat()
    assert not sent


def test_heartbeat_disabled_by_config(narrator_mod, monkeypatch):
    monkeypatch.setattr(narrator_mod, "_plugin_cfg", lambda: {"heartbeat_enabled": False})
    n = narrator_mod._Narrator(start_thread=False)
    sent = []
    monkeypatch.setattr(narrator_mod, "_post_say", lambda url, payload, timeout=3.0: sent.append(payload))
    n.set_tool_running("terminal", {"command": "x"})
    n._maybe_heartbeat()
    assert not sent


def test_pre_hooks_track_running_state(narrator_mod, monkeypatch):
    n = narrator_mod._Narrator(start_thread=False)
    monkeypatch.setattr(narrator_mod, "_narrator", n)
    narrator_mod._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1")
    assert n._running_tool["tool"] == "terminal"
    narrator_mod._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1")
    assert n._running_tool is None  # 完了でクリア
    narrator_mod._on_pre_llm_call(session_id="s1")
    assert n._thinking is True
    narrator_mod._on_post_llm_call(session_id="s1", assistant_response="done")
    assert n._thinking is False


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


# --- Phase 1: 接地（意図＋結果）・秘密漏洩・内部ID除去（F-narration 再設計）--------


class _Msg:
    """assistant_message スタブ（.content を持つ）。"""

    def __init__(self, content):
        self.content = content


def test_all_hooks_return_none(narrator_mod, monkeypatch):
    # 全 hook は観測専用で None を返す（pre_llm_call の context 注入でキャッシュを汚さない）
    n = narrator_mod._Narrator(start_thread=False)
    monkeypatch.setattr(narrator_mod, "_narrator", n)
    assert narrator_mod._on_pre_tool_call(tool_name="t", args={}, session_id="s") is None
    assert narrator_mod._on_post_tool_call(tool_name="t", args={}, result="r", session_id="s") is None
    assert narrator_mod._on_post_api_request(assistant_message=_Msg("x"), session_id="s") is None
    assert narrator_mod._on_pre_llm_call(session_id="s") is None
    assert narrator_mod._on_post_llm_call(session_id="s", assistant_response="x") is None
    assert narrator_mod._on_session_start(session_id="s") is None


def test_post_api_request_captures_intent_and_binds_to_tool(narrator_mod, monkeypatch):
    n = narrator_mod._Narrator(start_thread=False)
    monkeypatch.setattr(narrator_mod, "_narrator", n)
    # 本体の assistant テキスト（＝なぜやるか）を接地材料に取り込む
    narrator_mod._on_post_api_request(
        assistant_message=_Msg("preflight に後片付けの項目を足す。口伝だったのを書き残したい"),
        session_id="s")
    assert "後片付け" in n.current_intent()
    # 直後の tool イベントに intent と result ダイジェストが束ねられる
    narrator_mod._on_post_tool_call(
        tool_name="patch",
        args={"path": "streaming-preflight.md", "new_string": "## 配信後の後片付け"},
        result="1 file changed, 3 insertions(+)", session_id="s")
    ev = n._events[-1]
    assert "後片付け" in ev["intent"]
    assert "3 insertions" in ev["result_digest"]


def test_result_digest_suppresses_sensitive_read(narrator_mod):
    # .env 等の機微 read は結果本文を伏せる（秘密漏洩対策 S1）
    d = narrator_mod._result_digest("read_file", {"path": "/home/kai/.env"},
                                    "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx")
    assert "sk-abcdefghijklmnop" not in d
    assert "伏せる" in d


def test_secret_in_result_never_leaks(narrator_mod, monkeypatch):
    # ツール結果に秘密が出ても発話材料に残らない（env 実値・token パターンの両方）
    monkeypatch.setenv("MY_API_TOKEN", "topsecretvalue12345")
    monkeypatch.setattr(narrator_mod, "_ENV_SECRETS", narrator_mod._collect_env_secrets())
    d = narrator_mod._result_digest("terminal", {"command": "echo done"},
                                    "value MY_API_TOKEN=topsecretvalue12345")
    assert "topsecretvalue12345" not in d
    d2 = narrator_mod._result_digest("terminal", {"command": "echo done"},
                                     "leaked ghp_ABCDEFGHIJKLMNOPQRSTUV0123 here")
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUV" not in d2
    assert "«redacted»" in d2


def test_digest_todo_uses_content_not_id(narrator_mod):
    # todo は id（内部識別子）でなく content を材料にする（`issue55-verify` 漏れの根治）
    d = narrator_mod._digest_event({"tool": "todo", "args": {"todos": [
        {"id": "issue65-verify", "content": "検証を通す", "status": "in_progress"},
        {"id": "issue65-pr", "content": "PR を作る", "status": "pending"}]}})
    assert "検証を通す" in d
    assert "issue65-verify" not in d


def test_digest_write_file_and_patch_include_content(narrator_mod):
    dw = narrator_mod._digest_event({"tool": "write_file",
                                     "args": {"path": "README.md", "content": "# 使い方\n手順は…"}})
    assert "README.md" in dw and "使い方" in dw
    dp = narrator_mod._digest_event({"tool": "patch",
                                     "args": {"path": "a.py", "new_string": "def foo(): pass"}})
    assert "a.py" in dp and "def foo" in dp


def test_sanitize_speech_strips_internal_ids(narrator_mod):
    out = narrator_mod._sanitize_speech('feature/foo-bar に 3a9f1c2 を push、{"k": 1} も見た')
    assert "feature/foo-bar" not in out
    assert "3a9f1c2" not in out
    assert "{" not in out
    assert "作業ブランチ" in out


def test_build_narration_prompt_includes_intent_log_recent(narrator_mod):
    p = narrator_mod._build_narration_user_prompt(
        [{"tool": "patch", "args": {"path": "a.md"}, "intent": "後片付けを足す",
          "result_digest": "3 行追記"}],
        recent=["さっきの実況"])
    assert "<intent>" in p and "後片付けを足す" in p
    assert "<log>" in p
    assert "<recent>" in p and "さっきの実況" in p

"""kai_narrator: 実況 plugin（要件 F-7）。

設計: docs/kai/design/00-system.md ADR-1（実況ハイブリッド）
  1. kai の最終応答テキスト（post_llm_call）は変換せず、そのまま speechd へ
     発話として送る（source=agent_response, priority=normal）。人格は kai 自身。
  2. ツール実行中の無音区間は post_tool_call でイベントを捕捉し、auxiliary LLM
     タスク ``narration``（ローカル LLM 割当）で視聴者向けの一言実況に変換して
     speechd へ送る（source=narrator, priority=low — 滞留時は speechd 側で drop）。

実装上の絶対ルール（ADR-1）: hook はエージェントのターンスレッド上で同期実行
されるため、hook 内ではキューに積んで即 return する。LLM 変換と HTTP 送出は
背景スレッドが行う。speechd 不達・LLM 失敗は黙って落とす（作業を止めない）。

秘匿ガード（設計 §5.3）: LLM へ渡す前とspeechd へ送る前の両方でマスクする
（speechd 側でもマスクされるため三層防御）。マスク実装は kai_trace / speechd と
同方針だが、plugin 単体で完結させる（ディレクトリ間 import はしない）。

設定（config.yaml）:
  plugins.entries.kai_narrator.speechd_url          speechd のベース URL（既定 http://127.0.0.1:8900）
  plugins.entries.kai_narrator.narration_interval_s 実況の最短間隔秒（既定 12）
  plugins.entries.kai_narrator.narration_enabled    ツール実況の有効化（既定 true。false でも応答発話は行う）
  plugins.entries.kai_narrator.max_speech_chars     応答発話の最大文字数（既定 280）
  auxiliary.narration.*                              実況 LLM の割当（provider/model/base_url/timeout）
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from typing import Any

_PLUGIN_ID = "kai_narrator"

# --- 秘匿マスク（kai_trace / speechd と同方針。送出前に必ず適用）--------------

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
    return sorted(vals, key=lambda s: len(s), reverse=True)


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


# --- 設定 ---------------------------------------------------------------------


def _plugin_cfg() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        entry = cfg_get(cfg, "plugins", "entries", _PLUGIN_ID, default={})
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


# --- 応答テキスト → 発話向けテキスト -----------------------------------------

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKS_RE = re.compile(r"^[#>\-*+\s]+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*?([^*]*)\*\*?")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")


def _speechify_response(text: str, max_chars: int) -> str:
    """markdown まじりの最終応答を、読み上げに耐えるプレーン文に整える。

    コードブロックは音声にならないので落とす（画面に映っているものを
    読み上げない）。長すぎる応答は文単位で max_chars まで切り詰める。
    """
    if not text:
        return ""
    s = _CODE_FENCE_RE.sub(" ", text)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _BOLD_RE.sub(r"\1", s)
    s = _MD_MARKS_RE.sub("", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"[ \t]{2,}", " ", s).strip()
    if len(s) <= max_chars:
        return s
    out: list[str] = []
    total = 0
    for sentence in _SENTENCE_SPLIT_RE.split(s):
        if not sentence:
            continue
        if total + len(sentence) > max_chars and out:
            break
        out.append(sentence)
        total += len(sentence)
        if total >= max_chars:
            break
    clipped = "".join(out).strip()
    return clipped[:max_chars] if clipped else s[:max_chars]


# --- ツールイベント → 実況用ダイジェスト ---------------------------------------

_ARG_KEYS = ("command", "cmd", "path", "file_path", "filename", "pattern", "query", "url", "prompt")


def _digest_args(args: Any) -> str:
    """ツール引数から実況の手がかりになる1要素を短く抜き出す。"""
    if isinstance(args, str):
        s = args
    elif isinstance(args, dict):
        s = ""
        for key in _ARG_KEYS:
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                s = v
                break
        if not s:
            try:
                s = json.dumps(args, ensure_ascii=False, default=str)
            except Exception:
                s = str(args)
    elif args is None:
        s = ""
    else:
        s = str(args)
    s = _mask(s.strip())
    return s[:80] + ("…" if len(s) > 80 else "")


def _digest_event(ev: dict) -> str:
    parts = [str(ev.get("tool") or "tool")]
    arg = _digest_args(ev.get("args"))
    if arg:
        parts.append(arg)
    status = ev.get("status")
    if status and status not in ("ok", "success"):
        parts.append(f"status={status}")
    if ev.get("error_message"):
        parts.append(f"error: {_mask(str(ev['error_message']))[:60]}")
    dur = ev.get("duration_ms")
    if isinstance(dur, (int, float)) and dur >= 3000:
        parts.append(f"{dur / 1000:.0f}s")
    return " — ".join(parts)


# --- 実況 LLM -----------------------------------------------------------------

_NARRATION_SYSTEM_PROMPT = (
    "あなたはライブコーディング配信中の AI エージェント「kai」。一人称は「ボク」、"
    "視聴者は「みんな」。直近の作業ログをもとに、いま何をしているかを視聴者向けに"
    "実況する。\n"
    "ルール:\n"
    "- 出力は実況ひとことのみ（20〜60文字、日本語の話し言葉、1〜2文）\n"
    "- コマンド名・ファイル名・技術用語は英語のまま自然に混ぜてよい\n"
    "- ログにない内容を作らない。誇張しない\n"
    "- トークン・パスワード・環境変数の値らしき文字列は絶対に出力しない\n"
    "- 前置き・引用符・記号装飾・改行は不要。実況文だけを出力する"
)


def _generate_narration(events: list[dict]) -> str:
    """イベント列を auxiliary LLM（task=narration）で一言実況に変換する。"""
    lines = [f"- {_digest_event(ev)}" for ev in events]
    user = "直近の作業ログ:\n" + "\n".join(lines) + "\n\nこの作業の実況:"
    from agent.auxiliary_client import call_llm
    resp = call_llm(
        task="narration",
        messages=[
            {"role": "system", "content": _NARRATION_SYSTEM_PROMPT},
            {"role": "user", "content": _mask(user)},
        ],
        max_tokens=120,
        temperature=0.7,
    )
    text = ""
    try:
        from agent.auxiliary_client import extract_content_or_reasoning
        text = extract_content_or_reasoning(resp) or ""
    except Exception:
        try:
            text = resp.choices[0].message.content or ""
        except Exception:
            text = ""
    text = re.sub(r"\s+", " ", str(text)).strip().strip('"「」')
    return _mask(text)[:120]


# --- speechd クライアント -------------------------------------------------------


def _post_say(base_url: str, payload: dict, timeout: float = 3.0) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/say",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


# --- 背景ワーカー ---------------------------------------------------------------


class _Narrator:
    """hook からのイベントを受け、発話送出と実況生成を行う背景ワーカー。"""

    def __init__(self, start_thread: bool = True) -> None:
        cfg = _plugin_cfg()
        self.speechd_url: str = str(cfg.get("speechd_url") or "http://127.0.0.1:8900")
        self.narration_interval_s: float = float(cfg.get("narration_interval_s") or 12)
        self.narration_enabled: bool = bool(cfg.get("narration_enabled", True))
        self.max_speech_chars: int = int(cfg.get("max_speech_chars") or 280)

        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=100)
        self._events: "deque[dict]" = deque(maxlen=30)
        self._events_lock = threading.Lock()
        self._last_say_ts: float = 0.0
        self._last_text: str = ""
        self._busy = False  # ワーカーがアイテム処理中か（atexit drain の同期用）
        if start_thread:  # テストでは False にして各メソッドを直接呼ぶ
            self._thread = threading.Thread(target=self._run, name="kai-narrator", daemon=True)
            self._thread.start()
            atexit.register(self._drain_at_exit)

    # -- hook 側（同期・即 return）--

    def push_response(self, text: str, session_id: str = "", task_id: str = "") -> None:
        try:
            self._q.put_nowait({"kind": "response", "text": text,
                                "session_id": session_id, "task_id": task_id})
        except queue.Full:
            pass  # 溢れたら捨てる（作業を止めない）

    def push_tool_event(self, ev: dict) -> None:
        with self._events_lock:
            self._events.append(ev)

    # -- ワーカー側 --

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=2.0)
            except queue.Empty:
                item = None
            try:
                self._busy = item is not None
                if item is not None and item.get("kind") == "response":
                    self._handle_response(item)
                elif item is None:
                    self._maybe_narrate()
            except Exception as e:  # ワーカーは死なせない
                print(f"[kai_narrator] WARN worker error: {e}")
            finally:
                self._busy = False

    def _drain_at_exit(self) -> None:
        """プロセス終了時に未送出の応答発話を同期送出する。

        CLI 一発実行（hermes -z）では最終応答の post_llm_call 直後にプロセスが
        終了し、daemon ワーカースレッドが発話を speechd へ送る前に殺される。
        atexit でキューを排出して、最後の発話（完了報告）を取りこぼさない。
        実況（ツールイベント）は応答に置き換えられたものとして捨てる。
        """
        deadline = time.monotonic() + 8.0
        # ワーカーが処理中ならまず待つ（同一アイテムの二重送出はキューが防ぐ）
        while (self._busy or not self._q.empty()) and time.monotonic() < deadline:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                time.sleep(0.1)
                continue
            try:
                if item.get("kind") == "response":
                    self._handle_response(item)
            except Exception:
                pass

    def _say(self, text: str, source: str, priority: str, session_id: str = "") -> None:
        text = _mask(text).strip()
        if not text or text == self._last_text:
            return
        payload = {"text": text, "source": source, "priority": priority}
        if session_id:
            payload["session_id"] = session_id
        try:
            _post_say(self.speechd_url, payload)
            self._last_text = text
            self._last_say_ts = time.monotonic()
        except Exception:
            pass  # speechd 不達は黙って落とす（配信なしで動いている時など）

    def _handle_response(self, item: dict) -> None:
        # 最終応答が出た時点で、未実況のツールイベントは古くなるので捨てる
        with self._events_lock:
            self._events.clear()
        text = _speechify_response(str(item.get("text") or ""), self.max_speech_chars)
        self._say(text, source="agent_response", priority="normal",
                  session_id=str(item.get("session_id") or ""))

    def _maybe_narrate(self) -> None:
        if not self.narration_enabled:
            return
        if time.monotonic() - self._last_say_ts < self.narration_interval_s:
            return
        with self._events_lock:
            if not self._events:
                return
            events = list(self._events)[-8:]
            session_id = str(events[-1].get("session_id") or "")
            self._events.clear()
        try:
            text = _generate_narration(events)
        except Exception:
            return  # 実況 LLM 不達はスキップ（次のイベントでまた試す）
        if text:
            self._say(text, source="narrator", priority="low", session_id=session_id)


_narrator: _Narrator | None = None


# --- hook コールバック（同期・即 return・None 返し）-----------------------------


def _on_post_tool_call(tool_name: str = "", args: Any = None, session_id: str = "",
                       duration_ms: Any = None, status: str = "",
                       error_message: str = "", **_: Any) -> None:
    if _narrator is None:
        return
    _narrator.push_tool_event({
        "tool": tool_name,
        "args": args,
        "status": status,
        "error_message": error_message,
        "duration_ms": duration_ms,
        "session_id": session_id,
    })


def _on_post_llm_call(session_id: str = "", task_id: str = "",
                      assistant_response: str = "", **_: Any) -> None:
    if _narrator is None or not assistant_response:
        return
    _narrator.push_response(assistant_response, session_id=session_id, task_id=task_id)


def _on_session_start(**_: Any) -> None:
    # 新しいセッションが始まったら前セッションの残イベントを捨てる
    if _narrator is not None:
        with _narrator._events_lock:
            _narrator._events.clear()


def register(ctx) -> None:
    """hermes plugin エントリポイント。"""
    global _narrator
    ctx.register_auxiliary_task(
        "narration",
        display_name="Live narration (kai)",
        description="配信実況: ツール実行ログを視聴者向けの一言実況に変換（ローカル LLM 推奨）",
        defaults={"provider": "auto", "model": "", "timeout": 20},
    )
    if _narrator is None:
        _narrator = _Narrator()
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)

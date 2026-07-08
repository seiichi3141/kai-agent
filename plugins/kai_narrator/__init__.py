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

無音対策（Issue #10 / ハートビート実況）: post_tool_call はツール完了時にしか
発火しないため、長時間コマンドの実行中（CI 待ち・ビルド等）や LLM の思考中は
イベントが無く実況が止まる（リハーサルで 316 秒の無音を実測）。そこで
pre_tool_call / pre_llm_call で「いま何が走っているか」を記録し、最後の発話から
heartbeat_interval_s 経過したら現在状況を一言実況する。実行中ツールがあれば
それを材料に auxiliary LLM で生成（失敗時は定型文）、LLM 思考中は定型文を
ローテーションで発話する。

実況の質（Issue #31）: 等間隔の行動スナップショット（「〜を確認してるよ」の連発）は
視聴者を置いてけぼりにする。第 2 回リハーサルの実測で間隔中央値 9 秒・目的 0%・
結果反応 0% だった。対策として (a) 通常間隔を長め（既定 40 秒）にして弾幕を止め、
(b) テスト結果・コミット・PR 作成・エラーなどの「旗艦イベント」は間隔を無視して
即座に実況し、(c) 実況 LLM に「いまの作業（kai 自身の直近宣言）」と「さっき実況した
こと（繰り返し禁止の材料）」を渡し、目的か結果を必ず 1 つ含め、新しく言うことが
無ければ SKIP させる。

設定（config.yaml）:
  plugins.entries.kai_narrator.speechd_url          speechd のベース URL（既定 http://127.0.0.1:8900）
  plugins.entries.kai_narrator.narration_interval_s 実況の最短間隔秒（既定 40。旗艦イベントは無視）
  plugins.entries.kai_narrator.narration_enabled    ツール実況の有効化（既定 true。false でも応答発話は行う）
  plugins.entries.kai_narrator.heartbeat_enabled    無音時のハートビート実況（既定 true。narration_enabled が前提）
  plugins.entries.kai_narrator.heartbeat_interval_s 最後の発話からハートビートまでの秒数（既定 45）
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


# --- パスの短縮（Issue #9: 実況・字幕にフルパスを流さない）--------------------

# スラッシュ 2 個以上のパス様文字列（読み上げ・字幕に耐えない）。URL を巻き込まない
# よう、直前が英数字・ / ・ : 等のとき（https://github.com/... の途中など）は
# マッチさせない。
_PATH_RE = re.compile(r"(?<![\w./:\-])~?/?(?:[\w.\-]+/){2,}[\w.\-]+")


def _shorten_paths(text: str) -> str:
    """パス様文字列をファイル名（basename）だけに短縮する。

    「/home/kai/kai-agent/kai-services/streaming/vm/broadcast.sh を編集」のような
    実況・発話は聞き取れないため、「broadcast.sh を編集」に落とす。
    """

    def _basename(m: re.Match) -> str:
        base = m.group(0).rstrip("/").rsplit("/", 1)[-1]
        return base or m.group(0)

    return _PATH_RE.sub(_basename, text)


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
    s = _shorten_paths(s)  # フルパスは読み上げ・字幕に耐えない（Issue #9）
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
    # LLM に渡す段階でパスを basename 化しておく（出力に混入する誘因を断つ）
    s = _shorten_paths(_mask(s.strip()))
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
    "視聴者は「みんな」。YouTube ライブの視聴者に向けて作業を実況する。\n"
    "視聴者はコードを読めない人もいて、途中から見始める人も多い。"
    "「〜を確認してるよ」のような操作の羅列は退屈で、置いてけぼりにする。\n"
    "【必ず守る】\n"
    "- 20〜80文字、日本語の話し言葉、1〜2文\n"
    "- **操作の説明だけで終わらせず、目的（なぜやるのか）か結果（どうなったか）を"
    "必ず1つ入れる**。例:『シーン名をコマンドから見たいから、OBS の状態を取ってみるよ』"
    "『よし、テストが全部通った！』『あれ、lint に怒られた。直すね』\n"
    "- 【さっき実況したこと】と同じ内容は繰り返さない。"
    "新しく伝えることが無ければ、実況せず「SKIP」とだけ出力する\n"
    "- 語尾をワンパターンにしない（「〜だよ」ばかりにしない）\n"
    "- ファイルはファイル名だけで呼ぶ。ディレクトリパス・URL・生ログは出さない\n"
    "- ログにない事実は作らない。トークン・パスワード等の秘密は絶対に出さない\n"
    "- 前置き・引用符・記号装飾・改行は不要。実況文だけを出力する"
)


def _is_skip(text: str) -> bool:
    """実況 LLM が「新しく言うことが無い」と判断して SKIP を返したか。"""
    return text.strip().rstrip("。.！!、").upper() == "SKIP"


# 旗艦イベント（テスト結果・コミット・push・PR 作成/マージ・エラー）。視聴者が
# 一番知りたい瞬間なので、通常間隔を無視して即座に実況する（Issue #31）。
_FLAGSHIP_CMD_RE = re.compile(
    r"verify\.sh|run_tests|pytest|node --test|npm test|npm run test"
    r"|git\s+commit|git\s+push|gh\s+pr\s+(create|merge)|gh\s+issue\s+create"
)


def _is_flagship(ev: dict) -> bool:
    status = ev.get("status")
    if status and status not in ("ok", "success"):
        return True  # 失敗・エラーは即報告
    if ev.get("error_message"):
        return True
    return bool(_FLAGSHIP_CMD_RE.search(_digest_args(ev.get("args"))))


# LLM 思考中（実行中ツールなし）のハートビート定型文。dedup（直前と同文は
# speechd/narrator 双方で抑制される）を避けるためローテーションする。
# ログに基づかない内容を作らない原則（ADR-1）に沿い、事実として常に正しい
# 「考え中」表現だけを使う。
_HEARTBEAT_IDLE_LINES = (
    "いま考えを整理してるところ。ちょっと待っててね",
    "うーん、次の一手を考え中だよ",
    "まだ考え中。もうすこしだけ待ってね",
)


def _generate_narration(events: list[dict], context: str = "",
                        recent: list[str] | None = None) -> str:
    """イベント列を auxiliary LLM（task=narration）で一言実況に変換する。

    context: kai 自身の直近宣言（いま何の作業をしているか）。視聴者の文脈維持に使う。
    recent: 直近に実況したテキスト（繰り返し禁止の判断材料）。
    """
    recent = recent or []
    lines = [f"- {_digest_event(ev)}" for ev in events]
    blocks: list[str] = []
    if context:
        blocks.append(f"【いまの作業】{context}")
    if recent:
        blocks.append("【さっき実況したこと（繰り返さない）】\n"
                      + "\n".join(f"- {r}" for r in recent))
    blocks.append("【直近の操作ログ】\n" + "\n".join(lines))
    blocks.append("実況（目的か結果を1つ含める。新しく言うことが無ければ SKIP）:")
    user = "\n\n".join(blocks)
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
    # プロンプト指示が漏れても機械的に短縮する（二段構え — Issue #9）
    return _shorten_paths(_mask(text))[:120]


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
        self.narration_interval_s: float = float(cfg.get("narration_interval_s") or 40)
        self.narration_enabled: bool = bool(cfg.get("narration_enabled", True))
        self.heartbeat_enabled: bool = bool(cfg.get("heartbeat_enabled", True))
        self.heartbeat_interval_s: float = float(cfg.get("heartbeat_interval_s") or 45)
        self.max_speech_chars: int = int(cfg.get("max_speech_chars") or 280)

        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=100)
        self._events: "deque[dict]" = deque(maxlen=30)
        self._events_lock = threading.Lock()
        self._last_say_ts: float = 0.0
        self._last_text: str = ""
        self._busy = False  # ワーカーがアイテム処理中か（atexit drain の同期用）
        # 実況の質（Issue #31）
        self._context: str = ""  # kai 自身の直近宣言（いまの作業。視聴者の文脈維持用）
        self._recent_narrations: "deque[str]" = deque(maxlen=3)  # 繰り返し禁止の材料
        self._flagship_pending: bool = False  # 旗艦イベントが来た → 間隔を無視して即実況
        # ハートビート用の現在状況（pre_tool_call / pre_llm_call が更新）
        self._state_lock = threading.Lock()
        self._running_tool: dict | None = None
        self._thinking: bool = False
        self._heartbeat_idx: int = 0
        # まだ実作業（ツール実行）を1つもしていない冒頭では「考え中」フィラーを
        # 喋らない。毎回冒頭が定型フィラーになるのを防ぐ（最初の発話を実作業由来に）。
        self._had_tool_activity: bool = False
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
            if _is_flagship(ev):
                self._flagship_pending = True
        with self._state_lock:
            self._had_tool_activity = True

    def set_tool_running(self, tool: str, args: Any, session_id: str = "") -> None:
        with self._state_lock:
            self._running_tool = {
                "tool": tool,
                "args": args,
                "session_id": session_id,
                "started_at": time.monotonic(),
            }

    def clear_tool_running(self) -> None:
        with self._state_lock:
            self._running_tool = None

    def set_thinking(self, thinking: bool) -> None:
        with self._state_lock:
            self._thinking = thinking

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
                    self._maybe_heartbeat()
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
            self._flagship_pending = False
        text = _speechify_response(str(item.get("text") or ""), self.max_speech_chars)
        # kai 自身の宣言を「いまの作業」文脈として保持（実況の文脈維持に使う。Issue #31）
        if text:
            self._context = text[:120]
        self._say(text, source="agent_response", priority="normal",
                  session_id=str(item.get("session_id") or ""))

    def _maybe_narrate(self) -> None:
        if not self.narration_enabled:
            return
        with self._events_lock:
            if not self._events:
                return
            # 旗艦イベント（テスト結果・コミット・PR・エラー）は間隔を無視して即実況。
            # それ以外は間隔が経つまでイベントを溜めて材料をまとめる（Issue #31）。
            if not self._flagship_pending and (
                    time.monotonic() - self._last_say_ts < self.narration_interval_s):
                return
            events = list(self._events)[-8:]
            session_id = str(events[-1].get("session_id") or "")
            context = self._context
            recent = list(self._recent_narrations)
            self._events.clear()
            self._flagship_pending = False
        try:
            text = _generate_narration(events, context=context, recent=recent)
        except Exception:
            return  # 実況 LLM 不達はスキップ（次のイベントでまた試す）
        if text and not _is_skip(text):
            self._say(text, source="narrator", priority="low", session_id=session_id)
            self._recent_narrations.append(text)

    def _maybe_heartbeat(self) -> None:
        """無音が続いたら現在状況を一言実況する（Issue #10）。

        post_tool_call 由来のイベントが無い時間帯（長時間コマンドの実行中・
        LLM 思考中）に、最後の発話から heartbeat_interval_s 経過していたら
        「いま何をしているか」を発話する。何も走っていなければ黙る
        （常駐プロセスのアイドル時に喋り続けない）。
        """
        if not (self.narration_enabled and self.heartbeat_enabled):
            return
        if time.monotonic() - self._last_say_ts < self.heartbeat_interval_s:
            return
        with self._state_lock:
            running = dict(self._running_tool) if self._running_tool else None
            thinking = self._thinking
            had_activity = self._had_tool_activity

        if running is not None:
            elapsed_s = int(time.monotonic() - float(running.get("started_at") or 0))
            ev = {
                "tool": running.get("tool"),
                "args": running.get("args"),
                "status": "running",
                "duration_ms": elapsed_s * 1000,
                "session_id": running.get("session_id", ""),
            }
            try:
                text = _generate_narration([ev], context=self._context,
                                           recent=list(self._recent_narrations))
            except Exception:
                # 実況 LLM 不達でも無音は避ける（定型文フォールバック）
                tool = str(running.get("tool") or "コマンド")
                text = f"いま {tool} の完了を待ってるよ。もう {elapsed_s} 秒くらい経ったかな"
            if text and not _is_skip(text):
                self._say(text, source="narrator", priority="low",
                          session_id=str(running.get("session_id") or ""))
                self._recent_narrations.append(text)
            return

        if thinking and had_activity:
            # 冒頭（まだツール未実行）ではフィラーを出さない。最初の発話は実作業
            # 由来の実況にする（毎回冒頭が定型フィラーになるのを防ぐ）。
            text = _HEARTBEAT_IDLE_LINES[self._heartbeat_idx % len(_HEARTBEAT_IDLE_LINES)]
            self._heartbeat_idx += 1
            self._say(text, source="narrator", priority="low")


_narrator: _Narrator | None = None


# --- hook コールバック（同期・即 return・None 返し）-----------------------------


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "",
                      **_: Any) -> None:
    # 観測のみ（block ディレクティブは返さない）。ハートビート用に
    # 「いま実行中のツール」を記録する
    if _narrator is not None:
        _narrator.set_tool_running(tool_name, args, session_id=session_id)


def _on_post_tool_call(tool_name: str = "", args: Any = None, session_id: str = "",
                       duration_ms: Any = None, status: str = "",
                       error_message: str = "", **_: Any) -> None:
    if _narrator is None:
        return
    _narrator.clear_tool_running()
    _narrator.push_tool_event({
        "tool": tool_name,
        "args": args,
        "status": status,
        "error_message": error_message,
        "duration_ms": duration_ms,
        "session_id": session_id,
    })


def _on_pre_llm_call(**_: Any) -> None:
    # LLM 応答待ち（思考中）に入った。ハートビートの定型文発話の対象になる
    if _narrator is not None:
        _narrator.set_thinking(True)


def _on_post_llm_call(session_id: str = "", task_id: str = "",
                      assistant_response: str = "", **_: Any) -> None:
    if _narrator is None:
        return
    _narrator.set_thinking(False)
    if assistant_response:
        _narrator.push_response(assistant_response, session_id=session_id, task_id=task_id)


def _on_session_start(**_: Any) -> None:
    # 新しいセッションが始まったら前セッションの残イベント・状況・文脈を捨てる
    if _narrator is not None:
        with _narrator._events_lock:
            _narrator._events.clear()
            _narrator._flagship_pending = False
        _narrator._context = ""
        _narrator._recent_narrations.clear()
        _narrator.clear_tool_running()
        _narrator.set_thinking(False)


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
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)

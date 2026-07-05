#!/usr/bin/env python3
"""speechd: kai の発話・字幕キュー（独立プロセス、VM 上で常駐）。

producer（kai の応答 / narrator の実況 / チャット返信）から `POST /say` で
テキストを受け取り、単一 FIFO キューに直列で積む。ワーカースレッドが1件ずつ
取り出し、Mac の TTS サーバー（AquesTalk10）へ合成要求 → 得られた WAV を
VM のスピーカー sink（PipeWire null-sink）へ paplay で同期再生しつつ、
`GET /events`（SSE）で購読中の Web オーバーレイ（kai-services/overlay/）へ
字幕の表示・クリアを push する。

字幕は当初 OBS が読むファイル方式だったが、将来アバター・コメント・進捗も
同じオーバーレイで表現できるよう SSE 配信に変更した（OBS ソースを増やさない
方針）。音声・キュー・縮退・マスク・トレースのロジックは変更していない。

設計の正典: docs/kai/design/00-system.md
  - §3 ADR-3: 発話・字幕・同期は speechd の単一 FIFO キューに集約する
  - §4 発話・字幕同期メカニズム: 字幕クリアは再生プロセス終了が一次トリガー、
    TTS 不達時は字幕のみ文字数ベース表示で縮退する
  - §5.1 トレースイベントの共通エンベロープ
  - §5.3 秘匿情報のマスク方針（producer 側と speechd 側の二層防御）

標準ライブラリのみで完結させる（requests 等の外部依存なし）。
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# --- 設定（環境変数。README 参照）-------------------------------------------

def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


PORT = int(_env("SPEECHD_PORT", "8900"))
BIND = _env("SPEECHD_BIND", "127.0.0.1")
TTS_URL = _env("TTS_URL", "http://100.106.136.117:8890")
AUDIO_SINK = _env("AUDIO_SINK", "kai_speaker")
# オーバーレイページ（GET /overlay/ で配信。将来の OBS ブラウザソース用）の実体
OVERLAY_DIR = Path(_env("OVERLAY_DIR", str(Path(__file__).resolve().parent.parent / "overlay")))

# 字幕ファイル（OBS の text-freetype2 ソースが「ファイルからの読み取り」で表示する。
# 配信への字幕合成の正典。tmpfs 上に置いてディスク書き込みを避ける）
_default_subtitle_file = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "kai-subtitle.txt")
SUBTITLE_FILE = Path(_env("SUBTITLE_FILE", _default_subtitle_file))

CONNECT_TIMEOUT = 3.0
TOTAL_TIMEOUT = 30.0
# 文間の無音（ms）。一気読み感を消す「息継ぎ」（docs/kai/design/tts-reading-rules.md §5.3）
SENTENCE_GAP_MS = float(_env("SPEECHD_SENTENCE_GAP_MS", "300"))
LOW_PRIORITY_QUEUE_THRESHOLD = 5  # この件数を超えて滞留していたら priority:low を drop
PLAYBACK_TIMEOUT = 120.0  # paplay の残留防止ウォッチドッグ（設計 §4 の watchdog 相当）
SSE_KEEPALIVE_INTERVAL = 15.0  # /events 購読者に keep-alive コメントを送る間隔（秒）
SSE_CLIENT_QUEUE_MAXSIZE = 50  # 購読者ごとのバッファ。溢れたら best-effort で drop


# --- hermes home（トレース出力先。kai_trace plugin と同じ規約）--------------

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - speechd は hermes 本体と別 venv・別 cwd で動く想定
    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path(os.path.expanduser("~/.hermes"))


# --- 秘匿マスク（設計 §5.3。kai_trace plugin の _mask と同じ方針）-----------
# producer 側（kai 応答 / narrator）でも実施済みの二層目。speechd 自身の
# ログ・字幕・トレースに秘密が漏れないよう、送出直前にもう一度マスクする。

_TOKEN_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[posur]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
]


def _collect_env_secrets() -> list[str]:
    """秘密っぽいキー名の env 値を、長いものから順にマスク対象として集める。"""
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


# --- トレース（JSONL 追記。best-effort。設計 §5.1 の共通エンベロープ）------


def _iso_now() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int((now % 1) * 1000):03d}Z"


class _TraceWriter:
    def __init__(self) -> None:
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
        self._dir = get_hermes_home() / "kai_trace"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # トレース初期化失敗は起動を阻害しない
        self._thread = threading.Thread(target=self._run, name="speechd-trace-writer", daemon=True)
        self._thread.start()

    def _path(self) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        return self._dir / f"{day}.jsonl"

    def emit(self, event: dict) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            pass  # キュー飽和時はドロップ（配信を止めない）

    def _run(self) -> None:
        while True:
            ev = self._q.get()
            self._write(ev)

    def _write(self, ev: dict) -> None:
        try:
            with self._path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass  # ロギング失敗はメイン処理を壊さない


_trace = _TraceWriter()


def _emit_trace(kind: str, session_id: str | None, work_thread_id: str | None, payload: dict) -> None:
    try:
        _trace.emit({
            "v": 1,
            "ts": _iso_now(),
            "session_id": session_id or None,
            "work_thread_id": work_thread_id or None,
            "component": "speechd",
            "kind": kind,
            "payload": payload,
        })
    except Exception:
        pass  # best-effort


# --- 字幕配信（SSE。設計 §4: 字幕は常に1件のみ・履歴なし）------------------
# ファイル方式（OBS text source）から Web オーバーレイ + SSE 方式へ変更した。
# 将来アバター・コメント・進捗も同じオーバーレイ・同じ /events で配信できる
# よう、イベントは {"type": ..., ...} の汎用形式にしてある（今は "subtitle" のみ）。

_sse_lock = threading.Lock()
_sse_clients: "set[queue.Queue[dict]]" = set()
_subtitle_lock = threading.Lock()
_current_subtitle: dict[str, Any] = {"text": ""}  # late-join 用に直近の字幕状態を保持


def _sse_register() -> "queue.Queue[dict]":
    """新規 /events 購読者を登録し、現在表示中の字幕を即座にキューへ積む（late-join）。"""
    client_q: "queue.Queue[dict]" = queue.Queue(maxsize=SSE_CLIENT_QUEUE_MAXSIZE)
    with _sse_lock:
        _sse_clients.add(client_q)
    with _subtitle_lock:
        current = dict(_current_subtitle)
    try:
        client_q.put_nowait({"type": "subtitle", **current})
    except queue.Full:
        pass
    return client_q


def _sse_unregister(client_q: "queue.Queue[dict]") -> None:
    with _sse_lock:
        _sse_clients.discard(client_q)


def _sse_broadcast(event: dict) -> None:
    """全購読者へ event を push する。ラグっているクライアントは best-effort で drop。"""
    with _sse_lock:
        clients = list(_sse_clients)
    for client_q in clients:
        try:
            client_q.put_nowait(event)
        except queue.Full:
            pass  # 配信失敗（クライアント側が詰まっている）で発話処理は止めない


def _write_subtitle_file(text: str) -> None:
    """字幕ファイルを原子的に更新する（OBS text ソースが読む。空文字でクリア）。"""
    try:
        tmp = SUBTITLE_FILE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(SUBTITLE_FILE)
    except Exception as e:
        print(f"[speechd] WARN subtitle file write failed: {e}")


def _set_subtitle(text: str, source: str | None = None, emotion: str | None = None) -> None:
    """字幕の表示/クリアを字幕ファイル + SSE の両方へ反映し、現在状態を更新する。

    配信への合成は OBS text ソース（字幕ファイル読み取り）が正典。SSE は
    Web オーバーレイ（プレビュー・将来のアバター/コメント拡張）向けに維持する。
    """
    event: dict[str, Any] = {"type": "subtitle", "text": text}
    if source:
        event["source"] = source
    if emotion:
        event["emotion"] = emotion
    with _subtitle_lock:
        _current_subtitle.clear()
        _current_subtitle.update({k: v for k, v in event.items() if k != "type"})
    _write_subtitle_file(text)
    try:
        _sse_broadcast(event)
    except Exception as e:
        print(f"[speechd] WARN sse broadcast failed: {e}")


def _clear_subtitle(
    beat_id: str, session_id: str | None, work_thread_id: str | None,
    source: str | None = None,
) -> None:
    _set_subtitle("", source=source)
    _emit_trace("subtitle_cleared", session_id, work_thread_id, {"beat_id": beat_id})


# --- TTS 呼び出し（NDJSON ストリーミング）------------------------------------
# 接続タイムアウト 3 秒 / 全体タイムアウト 30 秒（要件どおり）。
# http.client を直接使い、connect() 後にソケットの読み取りタイムアウトを
# 残り予算に付け替えることで、接続と全体で異なるタイムアウトを実現する。


def _stream_synthesize(text: str, voice: str, speed: int):
    """Mac TTS サーバーへ POST し、NDJSON の各行を dict として順に yield する。

    接続不能・タイムアウト・非 200 応答は例外を送出する（呼び出し側が縮退を判断）。
    """
    parsed = urllib.parse.urlsplit(TTS_URL)
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid TTS_URL (no host): {TTS_URL!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body = json.dumps({"text": text, "voice": voice, "speed": speed}).encode("utf-8")
    deadline = time.monotonic() + TOTAL_TIMEOUT

    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
        remaining = max(1.0, deadline - time.monotonic())
        if conn.sock is not None:
            conn.sock.settimeout(remaining)
        conn.request(
            "POST",
            "/synthesize",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"tts http {resp.status}")
        for raw_line in resp:
            if time.monotonic() > deadline:
                raise TimeoutError("tts total timeout exceeded")
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        conn.close()


# --- 1ビートの処理（設計 §4 の同期・縮退ロジック）---------------------------


def _play_segment(
    beat_id: str, session_id: str | None, work_thread_id: str | None, text: str, wav_base64: str,
    source: str | None = None, emotion: str | None = None,
) -> None:
    """字幕を出す → WAV を一時ファイルに書く → paplay で同期再生 → 再生完了後に字幕クリア。"""
    tmp_path: str | None = None
    try:
        wav_bytes = base64.b64decode(wav_base64)
        fd, tmp_path = tempfile.mkstemp(prefix="speechd-", suffix=".wav")
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes)

        _set_subtitle(text, source=source, emotion=emotion)

        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        try:
            subprocess.run(
                ["paplay", f"--device={AUDIO_SINK}", tmp_path],
                check=False,
                env=env,
                timeout=PLAYBACK_TIMEOUT,
            )
            _emit_trace("speech_finished", session_id, work_thread_id, {"beat_id": beat_id})
        except subprocess.TimeoutExpired:
            _emit_trace("speech_failed", session_id, work_thread_id, {
                "beat_id": beat_id, "reason": "paplay watchdog timeout",
            })
        except Exception as e:
            _emit_trace("speech_failed", session_id, work_thread_id, {
                "beat_id": beat_id, "reason": f"playback error: {e}",
            })
            print(f"[speechd] WARN playback failed: {e}")
    except Exception as e:
        _emit_trace("speech_failed", session_id, work_thread_id, {
            "beat_id": beat_id, "reason": f"decode/write error: {e}",
        })
        print(f"[speechd] WARN wav decode/write failed: {e}")
    finally:
        # 字幕クリアは再生プロセスの終了が一次トリガー（設計 §4）。
        _clear_subtitle(beat_id, session_id, work_thread_id, source=source)
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _degrade_segment(
    beat_id: str, session_id: str | None, work_thread_id: str | None, text: str, reason: str,
    source: str | None = None, emotion: str | None = None,
) -> None:
    """TTS 不達時の縮退: 音声なしで字幕だけを文字数ベースの時間表示してクリアする。"""
    _emit_trace("speech_failed", session_id, work_thread_id, {
        "beat_id": beat_id, "reason": _mask(str(reason)),
    })
    _set_subtitle(text, source=source, emotion=emotion)
    duration = max(2.0, min(8.0, len(text) / 8.5))
    time.sleep(duration)
    _clear_subtitle(beat_id, session_id, work_thread_id, source=source)


def _process_item(item: dict) -> None:
    beat_id = item["beat_id"]
    session_id = item.get("session_id")
    work_thread_id = item.get("work_thread_id")
    source = item.get("source", "agent_response")
    emotion = item.get("emotion")
    voice = item.get("voice", "F1")
    speed = item.get("speed", 120)
    text = _mask(item["text"])

    _emit_trace("speech_started", session_id, work_thread_id, {
        "beat_id": beat_id,
        "source": source,
        "voice": voice,
        "speed": speed,
        "text_preview": text[:80],
    })

    got_any_line = False
    try:
        for line in _stream_synthesize(text, voice, speed):
            got_any_line = True
            seg_text = _mask(str(line.get("text", "")))
            wav_b64 = line.get("wav_base64")
            if wav_b64:
                _play_segment(beat_id, session_id, work_thread_id, seg_text, wav_b64, source=source, emotion=emotion)
                # 息継ぎ: 文の再生後に短い無音を挟む（次の文・次の発話との間）
                if SENTENCE_GAP_MS > 0:
                    time.sleep(SENTENCE_GAP_MS / 1000.0)
            else:
                err = line.get("error", "synthesis failed")
                _degrade_segment(beat_id, session_id, work_thread_id, seg_text, err, source=source, emotion=emotion)
    except Exception as e:
        if not got_any_line:
            # TTS サーバーへ到達できなかった／ストリーム開始前にタイムアウト
            # → 元の全文を字幕のみで縮退表示する（設計 §4）。
            _degrade_segment(beat_id, session_id, work_thread_id, text, f"tts unreachable: {e}", source=source, emotion=emotion)
        else:
            # 一部の文は既に再生済み。残りは諦めてログのみ（配信は止めない）。
            _emit_trace("speech_failed", session_id, work_thread_id, {
                "beat_id": beat_id, "reason": f"stream interrupted: {e}",
            })
            print(f"[speechd] WARN tts stream interrupted mid-utterance: {e}")


# --- キュー（FIFO 単一コンシューマ。設計 §4）--------------------------------

_queue: "queue.Queue[dict]" = queue.Queue()
_enqueue_lock = threading.Lock()
_last_enqueued_text: str | None = None


def _enqueue(item: dict) -> tuple[bool, str | None]:
    """重複抑制・priority:low の滞留 drop を行ってキューに積む。

    戻り値: (accepted, drop_reason)
    """
    global _last_enqueued_text
    with _enqueue_lock:
        if _last_enqueued_text is not None and item["text"] == _last_enqueued_text:
            return False, "duplicate"
        if item.get("priority") == "low" and _queue.qsize() > LOW_PRIORITY_QUEUE_THRESHOLD:
            return False, "dropped_low_priority_queue_full"
        _last_enqueued_text = item["text"]
        _queue.put(item)
        return True, None


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        try:
            _process_item(item)
        except Exception as e:  # ワーカースレッドは死なせない
            print(f"[speechd] ERROR processing item {item.get('beat_id')}: {e}")
        finally:
            _queue.task_done()


# --- HTTP サーバー -----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "speechd/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # アクセスログは抑制（必要なら別途 systemd journal で見る）

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # overlay は file:// オリジンで動く（EventSource の CORS 対策）。
        # bind は 127.0.0.1 限定なので "*" でも外部には露出しない。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        elif self.path == "/events":
            self._handle_sse()
        elif self.path == "/overlay" or self.path.startswith("/overlay/"):
            self._handle_overlay()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_overlay(self) -> None:
        """オーバーレイページの静的配信（OBS ブラウザソース用）。

        kai-services/overlay/ の 3 ファイルを配信する。speechd と同一オリジンに
        なるため、SSE（/events）へ相対パスで CORS なしに接続できる。
        許可リスト方式（パストラバーサル防止）。
        """
        if self.path == "/overlay":
            # 相対参照（app.js / style.css）を効かせるため末尾スラッシュへ寄せる
            self.send_response(301)
            self.send_header("Location", "/overlay/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = self.path[len("/overlay/"):] or "index.html"
        allowed = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
        }
        if name not in allowed:
            self._send_json(404, {"error": "not found"})
            return
        path = OVERLAY_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": f"overlay file missing: {name}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", allowed[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self) -> None:
        """`GET /events`: SSE 購読。接続を握ったまま、字幕イベントを push し続ける。

        複数クライアント購読可（`_sse_register`/`_sse_unregister` で管理）。
        新規購読者には現在表示中の字幕を即座に送る（late-join）。keep-alive
        コメントを SSE_KEEPALIVE_INTERVAL 秒間隔で送って接続を維持する。
        クライアント切断（書き込み失敗）を検知したら登録解除して抜ける
        （speechd 自体の発話処理には影響しない。best-effort）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        # overlay は file:// オリジン（Origin: null）から EventSource で接続する。
        # このヘッダがないとブラウザが CORS で購読をブロックする（実機で確認済み）。
        # bind は 127.0.0.1 限定なので "*" でも外部には露出しない。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        client_q = _sse_register()
        try:
            while True:
                try:
                    event = client_q.get(timeout=SSE_KEEPALIVE_INTERVAL)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # クライアント切断。発話処理は継続する
        finally:
            _sse_unregister(client_q)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/say":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(400, {"error": "text is required"})
            return

        item = {
            "beat_id": str(uuid.uuid4()),
            "text": text,
            "voice": data.get("voice", "F1"),
            "speed": data.get("speed", 120),
            "source": data.get("source", "agent_response"),
            "priority": data.get("priority", "normal"),
            "work_thread_id": data.get("work_thread_id"),
            "session_id": data.get("session_id"),
            "emotion": data.get("emotion"),
        }
        accepted, reason = _enqueue(item)
        response: dict[str, Any] = {"queued": accepted, "queue_depth": _queue.qsize()}
        if reason:
            response["reason"] = reason
        self._send_json(202, response)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # 再起動時に TIME_WAIT 中のポートへ即再バインドできるようにする
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # SSE 購読者の切断・EventSource の自動再接続で頻発する正常系の
        # 例外はログを汚さないよう黙らせる（それ以外は標準の traceback を出す）。
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def main() -> None:
    _write_subtitle_file("")  # 前回の残留字幕をクリア
    threading.Thread(target=_worker_loop, name="speechd-worker", daemon=True).start()
    server = _Server((BIND, PORT), _Handler)
    print(f"[speechd] listening on {BIND}:{PORT} (TTS_URL={TTS_URL}, AUDIO_SINK={AUDIO_SINK})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Local browser-source overlay for live stream captions."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class LiveOverlayConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    caption_max_chars: int = 160
    partial_ttl_seconds: float = 2.0
    final_ttl_seconds: float = 8.0

    @property
    def overlay_url(self) -> str:
        return f"http://{self.host}:{self.port}/overlay"


class LiveOverlayState:
    def __init__(self, *, caption_max_chars: int = 160) -> None:
        self._caption_max_chars = max(1, int(caption_max_chars))
        self._lock = threading.Lock()
        self._captions: dict[str, dict[str, Any]] = {
            "host": self._empty_caption(),
            "assistant": self._empty_caption(),
            "chat": self._empty_caption(),
        }
        self._live_coding: dict[str, Any] = self._empty_live_coding_state()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []

    @staticmethod
    def _empty_caption(*, updated_at: float = 0.0) -> dict[str, Any]:
        return {
            "text": "",
            "kind": "clear",
            "updated_at": updated_at,
            "expires_at": 0.0,
            "speaker": "",
        }

    @staticmethod
    def _empty_live_coding_state(*, updated_at: float = 0.0) -> dict[str, Any]:
        return {
            "enabled": False,
            "status": "idle",
            "current_task": "",
            "codex_status": "idle",
            "build_status": "idle",
            "test_status": "idle",
            "active_file": "",
            "error_summary": "",
            "next_step": "",
            "updated_at": updated_at,
        }

    @staticmethod
    def _speaker(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"assistant", "chat"}:
            return normalized
        return "host"

    def publish_caption(
        self,
        text: str,
        *,
        final: bool,
        ttl_seconds: float,
        speaker: str = "host",
    ) -> dict[str, Any]:
        cleaned = self._clean_text(text)
        now = time.time()
        caption_speaker = self._speaker(speaker)
        state = {
            "text": cleaned,
            "kind": "final" if final else "partial",
            "updated_at": now,
            "expires_at": now + max(0.1, float(ttl_seconds)),
            "speaker": caption_speaker,
        }
        with self._lock:
            self._captions[caption_speaker] = state
            snapshot = self.snapshot_locked(now=now)
            subscribers = list(self._subscribers)
        self._broadcast(subscribers, snapshot)
        return snapshot

    def clear(self, kind: str = "caption") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if kind in {"caption", "all"}:
                self._captions["host"] = self._empty_caption(updated_at=now)
                self._captions["assistant"] = self._empty_caption(updated_at=now)
                self._captions["chat"] = self._empty_caption(updated_at=now)
                if kind == "all":
                    self._live_coding = self._empty_live_coding_state(updated_at=now)
            elif kind in {"host", "assistant", "chat"}:
                self._captions[kind] = self._empty_caption(updated_at=now)
            elif kind in {"live_coding", "coding"}:
                self._live_coding = self._empty_live_coding_state(updated_at=now)
            snapshot = self.snapshot_locked(now=now)
            subscribers = list(self._subscribers)
        self._broadcast(subscribers, snapshot)
        return snapshot

    def publish_live_coding(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        cleaned = self._clean_live_coding_fields(fields)
        with self._lock:
            state = dict(self._live_coding)
            state.update(cleaned)
            state["enabled"] = True
            state["updated_at"] = now
            self._live_coding = state
            snapshot = self.snapshot_locked(now=now)
            subscribers = list(self._subscribers)
        self._broadcast(subscribers, snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.snapshot_locked(now=time.time())

    def snapshot_locked(self, *, now: float) -> dict[str, Any]:
        captions = {
            "host": self._active_caption("host", now=now),
            "assistant": self._active_caption("assistant", now=now),
            "chat": self._active_caption("chat", now=now),
        }
        return {
            "caption": captions["host"],
            "captions": captions,
            "live_coding": dict(self._live_coding),
            "server_time": now,
        }

    def _active_caption(self, speaker: str, *, now: float) -> dict[str, Any]:
        caption = dict(self._captions.get(speaker) or self._empty_caption())
        if caption.get("expires_at", 0) and caption["expires_at"] <= now:
            return self._empty_caption(updated_at=caption.get("updated_at", 0.0))
        return caption

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        with self._lock:
            self._subscribers.append(subscriber)
            snapshot = self.snapshot_locked(now=time.time())
        self._safe_put(subscriber, snapshot)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(subscriber)
            except ValueError:
                return

    def _clean_text(self, text: str) -> str:
        cleaned = _CONTROL_CHARS_RE.sub("", str(text or ""))
        cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
        if len(cleaned) <= self._caption_max_chars:
            return cleaned
        return cleaned[-self._caption_max_chars :].lstrip()

    def _clean_live_coding_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "status",
            "current_task",
            "codex_status",
            "build_status",
            "test_status",
            "active_file",
            "error_summary",
            "next_step",
        }
        cleaned: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            text = _CONTROL_CHARS_RE.sub("", str(value or ""))
            text = _WHITESPACE_RE.sub(" ", text).strip()
            if len(text) > self._caption_max_chars:
                text = text[: self._caption_max_chars].rstrip()
            cleaned[key] = text
        return cleaned

    @staticmethod
    def _safe_put(subscriber: queue.Queue[dict[str, Any]], payload: dict[str, Any]) -> None:
        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                pass

    def _broadcast(self, subscribers: list[queue.Queue[dict[str, Any]]], payload: dict[str, Any]) -> None:
        for subscriber in subscribers:
            self._safe_put(subscriber, payload)


class _OverlayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, request_handler_class, *, state: LiveOverlayState):
        super().__init__(server_address, request_handler_class)
        self.overlay_state = state


class LiveOverlayServer:
    def __init__(self, config: LiveOverlayConfig) -> None:
        self.config = config
        self.state = LiveOverlayState(caption_max_chars=config.caption_max_chars)
        self._httpd: _OverlayHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def overlay_url(self) -> str:
        if self._httpd is None:
            return self.config.overlay_url
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/overlay"

    def start(self) -> None:
        with self._lock:
            if self._httpd is not None:
                return
            self._httpd = _OverlayHTTPServer(
                (self.config.host, self.config.port),
                _make_handler(),
                state=self.state,
            )
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="hermes-live-overlay",
                daemon=True,
            )
            self._thread.start()
        logger.info("live overlay server started: %s", self.overlay_url)

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            thread = self._thread
            self._httpd = None
            self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def publish_caption(
        self,
        text: str,
        *,
        final: bool,
        speaker: str = "host",
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        ttl = (
            float(ttl_seconds)
            if ttl_seconds is not None
            else self.config.final_ttl_seconds if final else self.config.partial_ttl_seconds
        )
        return self.state.publish_caption(text, final=final, ttl_seconds=ttl, speaker=speaker)

    def publish_live_coding(self, **fields: Any) -> dict[str, Any]:
        return self.state.publish_live_coding(**fields)


def load_live_overlay_config(config: dict | None) -> LiveOverlayConfig:
    root = config if isinstance(config, dict) else {}
    overlay = root.get("live_overlay")
    overlay = overlay if isinstance(overlay, dict) else {}
    caption = overlay.get("caption")
    caption = caption if isinstance(caption, dict) else {}

    def _int(name: str, default: int, *, source: dict = overlay) -> int:
        value = source.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _float(name: str, default: float, *, source: dict = caption) -> float:
        value = source.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    host = overlay.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        host = "127.0.0.1"

    return LiveOverlayConfig(
        enabled=bool(overlay.get("enabled", False)),
        host=host.strip(),
        port=_int("port", 8765),
        caption_max_chars=_int("max_chars", 160, source=caption),
        partial_ttl_seconds=_float("partial_ttl_seconds", 2.0),
        final_ttl_seconds=_float("final_ttl_seconds", 8.0),
    )


_SERVER_LOCK = threading.Lock()
_SERVER: LiveOverlayServer | None = None
_SERVER_KEY: tuple[str, int, int, float, float] | None = None


def ensure_live_overlay_server(config: dict | None) -> LiveOverlayServer | None:
    overlay_config = load_live_overlay_config(config)
    if not overlay_config.enabled:
        return None
    key = (
        overlay_config.host,
        overlay_config.port,
        overlay_config.caption_max_chars,
        overlay_config.partial_ttl_seconds,
        overlay_config.final_ttl_seconds,
    )
    global _SERVER, _SERVER_KEY
    with _SERVER_LOCK:
        if _SERVER is not None and _SERVER_KEY == key:
            return _SERVER
        if _SERVER is not None:
            _SERVER.stop()
        server = LiveOverlayServer(overlay_config)
        server.start()
        _SERVER = server
        _SERVER_KEY = key
        return server


def publish_caption(
    config: dict | None,
    text: str,
    *,
    final: bool,
    speaker: str = "host",
    ttl_seconds: float | None = None,
) -> dict[str, Any] | None:
    server = ensure_live_overlay_server(config)
    if server is None:
        return None
    return server.publish_caption(text, final=final, speaker=speaker, ttl_seconds=ttl_seconds)


def publish_live_coding_state(config: dict | None, **fields: Any) -> dict[str, Any] | None:
    server = ensure_live_overlay_server(config)
    if server is None:
        return None
    return server.publish_live_coding(**fields)


def stop_live_overlay_server() -> None:
    global _SERVER, _SERVER_KEY
    with _SERVER_LOCK:
        server = _SERVER
        _SERVER = None
        _SERVER_KEY = None
    if server is not None:
        server.stop()


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        server: _OverlayHTTPServer

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in {"/", "/overlay"}:
                self._send_bytes(HTTPStatus.OK, _OVERLAY_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/state.json":
                self._send_json(self.server.overlay_state.snapshot())
            elif path == "/events":
                self._send_events()
            elif path == "/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if path == "/api/caption":
                text = str(payload.get("text") or "")
                final = bool(payload.get("final"))
                speaker = str(payload.get("speaker") or "host")
                ttl = self.server.overlay_state.publish_caption(
                    text,
                    final=final,
                    ttl_seconds=8.0 if final else 2.0,
                    speaker=speaker,
                )
                self._send_json(ttl)
            elif path == "/api/clear":
                self._send_json(self.server.overlay_state.clear(str(payload.get("kind") or "caption")))
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("live overlay http: " + format, *args)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("content-length") or "0")
            except ValueError as exc:
                raise ValueError("invalid content-length") from exc
            if length > 64_000:
                raise ValueError("request too large")
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid json") from exc
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(int(status))
            self.send_header("content-type", content_type)
            self.send_header("cache-control", "no-store")
            self.send_header("access-control-allow-origin", "*")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_events(self) -> None:
            subscriber = self.server.overlay_state.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "keep-alive")
            self.send_header("access-control-allow-origin", "*")
            self.end_headers()
            try:
                while True:
                    try:
                        payload = subscriber.get(timeout=15)
                        data = json.dumps(payload, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                self.server.overlay_state.unsubscribe(subscriber)

    return Handler


_OVERLAY_HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Live Overlay</title>
  <style>
    :root {
      --caption-final: #f5fbff;
      --caption-muted: #d7e0e8;
      --host-accent: #00d6a3;
      --assistant-accent: #7cc7ff;
      --chat-accent: #ffd166;
      --coding-accent: #b6f36b;
      --box-bg: rgba(8, 13, 18, 0.58);
      --box-border: rgba(245, 251, 255, 0.18);
      --shadow: rgba(0, 0, 0, 0.86);
    }
    * { box-sizing: border-box; }
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: transparent;
      font-family: "Hiragino Sans", "Yu Gothic", "Noto Sans JP", sans-serif;
    }
    body {
      position: relative;
    }
    #coding-panel {
      position: fixed;
      left: 2vw;
      top: 3vh;
      width: min(560px, 45vw);
      min-height: 118px;
      padding: 12px 14px;
      opacity: 0;
      border: 1px solid var(--box-border);
      border-left: 5px solid var(--coding-accent);
      border-radius: 8px;
      background: rgba(8, 13, 18, 0.58);
      color: var(--caption-final);
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.30);
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.75);
      transition: opacity 140ms ease;
    }
    #coding-panel.visible {
      opacity: 1;
    }
    .codingTitle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--caption-muted);
      font-size: 12px;
      font-weight: 800;
      line-height: 1;
    }
    #coding-status {
      color: var(--coding-accent);
    }
    .codingTask {
      margin-bottom: 8px;
      font-size: 18px;
      font-weight: 800;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .codingGrid {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 5px 10px;
      font-size: 13px;
      line-height: 1.25;
    }
    .codingKey {
      color: var(--caption-muted);
      font-weight: 700;
    }
    .codingValue {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    #captions {
      position: fixed;
      left: 2vw;
      right: 2vw;
      bottom: 3.5vh;
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      width: auto;
    }
    .lane {
      display: flex;
      min-width: 0;
    }
    .lane.host {
      justify-content: flex-start;
    }
    .lane.assistant {
      justify-content: flex-end;
    }
    .lane.chat {
      justify-content: center;
    }
    .captionBox {
      width: min(1560px, 96vw);
      height: 96px;
      padding: 12px 16px 14px;
      opacity: 1;
      transform: translateY(0);
      border: 1px solid var(--box-border);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(12, 20, 28, 0.68), var(--box-bg));
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.34);
      color: var(--caption-final);
      font-size: clamp(18px, 1.78vw, 30px);
      font-weight: 780;
      letter-spacing: 0;
      line-height: 1.3;
      overflow-wrap: anywhere;
      text-shadow:
        0 2px 2px var(--shadow),
        0 0 14px rgba(0, 0, 0, 0.64),
        0 0 3px rgba(0, 0, 0, 0.95);
      transition: background-color 120ms ease, border-color 120ms ease;
    }
    .captionBox.visible {
      opacity: 1;
      transform: translateY(0);
    }
    .captionBox.host {
      text-align: left;
      border-left: 5px solid var(--host-accent);
    }
    .captionBox.assistant {
      text-align: right;
      border-right: 5px solid var(--assistant-accent);
    }
    .captionBox.chat {
      width: min(1180px, 82vw);
      height: 74px;
      text-align: center;
      border-top: 4px solid var(--chat-accent);
      font-size: clamp(15px, 1.32vw, 22px);
      background: linear-gradient(180deg, rgba(22, 18, 10, 0.68), rgba(12, 10, 6, 0.54));
    }
    .captionBox:not(.visible) {
      background: rgba(8, 13, 18, 0.32);
      border-color: rgba(245, 251, 255, 0.12);
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.22);
    }
    .captionBox.thinking .text::after {
      content: "";
      display: inline-block;
      width: 1.2em;
      margin-left: 0.18em;
      text-align: left;
      animation: thinkingDots 1.2s steps(4, end) infinite;
    }
    .label {
      display: block;
      margin-bottom: 5px;
      color: var(--caption-muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      line-height: 1;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.75);
    }
    .text {
      display: block;
      overflow: hidden;
      overflow-wrap: anywhere;
    }
    @keyframes thinkingDots {
      0% { content: ""; }
      25% { content: "."; }
      50% { content: ".."; }
      75%, 100% { content: "..."; }
    }
    @media (max-width: 780px) {
      #captions {
        left: 3vw;
        right: 3vw;
        bottom: 4vh;
      }
      .captionBox {
        width: 100%;
        height: 92px;
      }
      .captionBox.assistant {
        text-align: left;
        border-left: 5px solid var(--assistant-accent);
        border-right-width: 1px;
      }
      .captionBox.chat {
        width: 100%;
        height: 70px;
      }
      #coding-panel {
        left: 3vw;
        right: 3vw;
        top: 3vh;
        width: auto;
      }
    }
  </style>
</head>
<body>
  <div id="coding-panel" aria-live="polite">
    <div class="codingTitle">
      <span>ライブコーディング</span>
      <span id="coding-status">idle</span>
    </div>
    <div id="coding-task" class="codingTask"></div>
    <div class="codingGrid">
      <span class="codingKey">Codex</span><span id="coding-codex" class="codingValue"></span>
      <span class="codingKey">Build</span><span id="coding-build" class="codingValue"></span>
      <span class="codingKey">Test</span><span id="coding-test" class="codingValue"></span>
      <span class="codingKey">Error</span><span id="coding-error" class="codingValue"></span>
      <span class="codingKey">Next</span><span id="coding-next" class="codingValue"></span>
    </div>
  </div>
  <div id="captions">
    <div class="lane chat">
      <div id="caption-chat" class="captionBox chat" aria-live="polite">
        <span class="label">YouTubeチャット</span>
        <span class="text"></span>
      </div>
    </div>
    <div class="lane host">
      <div id="caption-host" class="captionBox host" aria-live="polite">
        <span class="label">実況者</span>
        <span class="text"></span>
      </div>
    </div>
    <div class="lane assistant">
      <div id="caption-assistant" class="captionBox assistant" aria-live="polite">
        <span class="label">AIアシスタント</span>
        <span class="text"></span>
      </div>
    </div>
  </div>
  <script>
    const boxes = {
      host: document.getElementById('caption-host'),
      assistant: document.getElementById('caption-assistant'),
      chat: document.getElementById('caption-chat')
    };
    const expiresAt = { host: 0, assistant: 0, chat: 0 };
    const codingEls = {
      panel: document.getElementById('coding-panel'),
      status: document.getElementById('coding-status'),
      task: document.getElementById('coding-task'),
      codex: document.getElementById('coding-codex'),
      build: document.getElementById('coding-build'),
      test: document.getElementById('coding-test'),
      error: document.getElementById('coding-error'),
      next: document.getElementById('coding-next')
    };

    function applyState(state) {
      const captions = state && state.captions ? state.captions : { host: state && state.caption ? state.caption : {} };
      applyCaption('host', captions.host || {});
      applyCaption('assistant', captions.assistant || {});
      applyCaption('chat', captions.chat || {});
      applyLiveCoding(state && state.live_coding ? state.live_coding : {});
    }

    function applyLiveCoding(item) {
      if (!codingEls.panel) return;
      const visible = !!item.enabled && !!(item.current_task || item.status !== 'idle' || item.codex_status !== 'idle');
      codingEls.panel.className = visible ? 'visible' : '';
      codingEls.status.textContent = item.status || 'idle';
      codingEls.task.textContent = item.current_task || '';
      codingEls.codex.textContent = item.codex_status || 'idle';
      codingEls.build.textContent = item.build_status || 'idle';
      codingEls.test.textContent = item.test_status || 'idle';
      codingEls.error.textContent = item.error_summary || '';
      codingEls.next.textContent = item.next_step || '';
    }

    function applyCaption(speaker, item) {
      const box = boxes[speaker];
      if (!box) return;
      const textEl = box.querySelector('.text');
      const text = item.text || '';
      expiresAt[speaker] = Number(item.expires_at || 0) * 1000;
      textEl.textContent = text;
      const thinking = text === '考え中' ? ' thinking' : '';
      box.className = 'captionBox ' + speaker + (text ? ' visible ' + (item.kind || 'partial') + thinking : '');
      fitCaption(box);
    }

    function fitCaption(box) {
      const textEl = box.querySelector('.text');
      const labelEl = box.querySelector('.label');
      if (!textEl || !labelEl) return;
      if (!textEl.textContent) {
        box.style.fontSize = '';
        return;
      }

      box.style.fontSize = '';
      const baseSize = parseFloat(window.getComputedStyle(box).fontSize) || 24;
      const minSize = 13;
      const step = 1;
      const boxStyle = window.getComputedStyle(box);
      const verticalPadding =
        parseFloat(boxStyle.paddingTop || '0') +
        parseFloat(boxStyle.paddingBottom || '0');
      const availableHeight = Math.max(
        18,
        box.clientHeight - verticalPadding - labelEl.offsetHeight - 5
      );

      let size = baseSize;
      while (size > minSize) {
        box.style.fontSize = size + 'px';
        if (textEl.scrollHeight <= availableHeight && textEl.scrollWidth <= textEl.clientWidth + 1) {
          return;
        }
        size -= step;
      }
      box.style.fontSize = minSize + 'px';
    }

    function expireLoop() {
      const now = Date.now();
      for (const speaker of Object.keys(boxes)) {
        if (expiresAt[speaker] && now > expiresAt[speaker]) {
          const box = boxes[speaker];
          box.querySelector('.text').textContent = '';
          box.className = 'captionBox ' + speaker;
          box.style.fontSize = '';
          expiresAt[speaker] = 0;
        }
      }
      requestAnimationFrame(expireLoop);
    }

    window.addEventListener('resize', () => {
      for (const speaker of Object.keys(boxes)) fitCaption(boxes[speaker]);
    });

    function startEvents() {
      if (!window.EventSource) return false;
      const events = new EventSource('/events');
      events.onmessage = (event) => {
        try { applyState(JSON.parse(event.data)); } catch (_) {}
      };
      events.onerror = () => {
        events.close();
        startPolling();
      };
      return true;
    }

    function startPolling() {
      setInterval(async () => {
        try {
          const response = await fetch('/state.json', { cache: 'no-store' });
          applyState(await response.json());
        } catch (_) {}
      }, 180);
    }

    fetch('/state.json', { cache: 'no-store' })
      .then((response) => response.json())
      .then(applyState)
      .catch(() => {});
    if (!startEvents()) startPolling();
    expireLoop();
  </script>
</body>
</html>
"""

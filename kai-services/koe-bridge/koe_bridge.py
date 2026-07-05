#!/usr/bin/env python3
"""koe-bridge — hermes の auxiliary LLM を OpenAI 互換 API として中継する。

目的: aquestalk-server（Mac・Node）の koe 生成に Codex の gpt-5.5 を使う。
gpt-5.5 は Codex OAuth 経由でしか呼べず、生の OpenAI 互換エンドポイントが
存在しないため、hermes の auxiliary クライアント（メインプロバイダ =
OpenAI Codex を解決できる）を薄い HTTP サーバーで包む。

- kai-vm 上で hermes の venv を使って常駐する（install.sh 参照）
- 対応 API は POST /v1/chat/completions のみ（koe 生成に必要な最小限）
- per-task 設定は auxiliary.koe.*（config.yaml）。未設定ならメイン
  プロバイダ + メインモデル（= Codex / gpt-5.5）に解決される
- bind は既定で 0.0.0.0（Tailscale / UTM NAT 内のみ。公開ポートなし）

使い方（Mac 側 aquestalk-server の .env）:
  KOE_LLM_BASE_URL=http://<kai-vm の Tailscale IP>:8930/v1
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# hermes（リポジトリ直下）を import パスに載せる（venv は hermes のものを使う）
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

PORT = int(os.environ.get("KOE_BRIDGE_PORT", "8930"))
BIND = os.environ.get("KOE_BRIDGE_BIND", "0.0.0.0")
TASK = os.environ.get("KOE_BRIDGE_TASK", "koe")


def _complete(body: dict) -> str:
    """OpenAI 互換リクエストを auxiliary クライアントで実行しテキストを返す。"""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages is required")
    resp = call_llm(
        task=TASK,
        messages=messages,
        max_tokens=int(body.get("max_tokens") or 500),
        temperature=float(body.get("temperature") or 0),
    )
    text = extract_content_or_reasoning(resp) or ""
    if not text:
        raise ValueError("empty completion")
    return text


class _Handler(BaseHTTPRequestHandler):
    server_version = "koe-bridge/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server の規約
        if self.path == "/health":
            self._send_json({"ok": True, "task": TASK})
            return
        self._send_json({"error": "not found"}, code=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server の規約
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json({"error": "not found"}, code=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            text = _complete(body)
            self._send_json({
                "object": "chat.completion",
                "model": str(body.get("model") or ""),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
            })
        except Exception as e:  # クライアント（aquestalk-server）側がフォールバックする
            self._send_json({"error": str(e)}, code=500)


def main() -> None:
    server = ThreadingHTTPServer((BIND, PORT), _Handler)
    print(f"[koe-bridge] listening on http://{BIND}:{PORT} (task: {TASK})")
    server.serve_forever()


if __name__ == "__main__":
    main()

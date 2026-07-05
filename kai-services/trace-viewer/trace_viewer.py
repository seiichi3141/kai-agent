#!/usr/bin/env python3
"""kai trace viewer — セッションログ（発話・字幕・作業イベント）の Web ビューア。

kai-vm 上で常駐し、`<HERMES_HOME>/kai_trace/YYYY-MM-DD.jsonl`
（kai_trace plugin と speechd が書く共通エンベロープの JSONL）をブラウザで
時系列表示する。Tailscale 内の別マシン（Mac 等）から
`http://<kai-vm の Tailscale IP>:8910/` で閲覧する想定。

- 読み取り専用（トレースへの書き込み・変更は一切しない）
- 依存は Python 標準ライブラリのみ
- トレース本文は書き込み側（kai_trace / narrator / speechd の三層）で
  秘匿マスク済みの前提。本サーバーは新たな秘匿情報を持たない

API:
  GET /                 ビューア HTML
  GET /api/dates        利用可能な日付一覧（ファイル名由来、UTC 日付）
  GET /api/events?date=YYYY-MM-DD[&after=N]
                        events: 行番号 n 付きイベント配列（after より後のみ）
                        next: 次回ポーリングで渡す after 値（ライブ追従用）
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("TRACE_VIEWER_PORT", "8910"))
# Tailscale/NAT 内からの閲覧のため既定で全インターフェースに bind する。
# VM は UTM の NAT 配下 + Tailscale のみで、公開ポートにはならない（README 参照）
BIND = os.environ.get("TRACE_VIEWER_BIND", "0.0.0.0")


def _hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val) if val else Path(os.path.expanduser("~/.hermes"))


TRACE_DIR = Path(os.environ.get("TRACE_DIR", str(_hermes_home() / "kai_trace")))

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_dates() -> list[str]:
    if not TRACE_DIR.is_dir():
        return []
    dates = []
    for p in TRACE_DIR.glob("*.jsonl"):
        if _DATE_RE.match(p.stem):
            dates.append(p.stem)
    return sorted(dates)


def read_events(date: str, after: int) -> tuple[list[dict], int]:
    """date の JSONL を読み、行番号 after より後のイベントを返す。

    行番号（1 始まり）をカーソルとして返すことで、クライアントは追記分だけを
    ポーリングできる。壊れた行（書き込み途中等）は読み飛ばす。
    """
    path = TRACE_DIR / f"{date}.jsonl"
    events: list[dict] = []
    n = 0
    if not path.is_file():
        return events, after
    with path.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, start=1):
            if n <= after:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            events.append({
                "n": n,
                "ts": e.get("ts"),
                "component": e.get("component"),
                "kind": e.get("kind"),
                "session_id": e.get("session_id"),
                "payload": e.get("payload") or {},
            })
    return events, max(n, after)


# --- HTML（自己完結・外部リソースなし）---------------------------------------

_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kai trace viewer</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #14161a; color: #d8dce2;
         font: 14px/1.5 -apple-system, "Hiragino Sans", "Noto Sans CJK JP", sans-serif; }
  header { position: sticky; top: 0; background: #1b1e24; padding: 10px 16px;
           display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
           border-bottom: 1px solid #2a2e36; }
  header h1 { font-size: 15px; margin: 0 8px 0 0; color: #9ecbff; }
  select, label { font-size: 13px; }
  select { background: #23272f; color: inherit; border: 1px solid #3a3f49;
           border-radius: 4px; padding: 3px 6px; }
  label { user-select: none; cursor: pointer; opacity: .9; }
  #timeline { padding: 10px 16px 60px; max-width: 1100px; margin: 0 auto; }
  .row { display: flex; gap: 10px; padding: 3px 6px; border-radius: 4px; }
  .row:hover { background: #1d2129; }
  .t { color: #7d8590; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .k { white-space: nowrap; min-width: 9.5em; }
  .b { overflow-wrap: anywhere; }
  .speech .k { color: #7ee787; }
  .subtitle-ev .k { color: #56d4dd; }
  .tool .k { color: #d2a8ff; }
  .llm .k { color: #79c0ff; }
  .turnrow .k { color: #ffa657; }
  .session .k { color: #ff7b72; }
  .err { color: #ff7b72; }
  .dim { color: #7d8590; }
  .badge { background: #23272f; border-radius: 3px; padding: 0 5px; margin-left: 6px;
           font-size: 12px; color: #9da7b3; }
  #status { margin-left: auto; font-size: 12px; color: #7d8590; }
</style>
</head>
<body>
<header>
  <h1>kai trace</h1>
  <select id="date"></select>
  <label><input type="checkbox" id="f-speech" checked> 🗣 発話</label>
  <label><input type="checkbox" id="f-subtitle"> 💬 字幕詳細</label>
  <label><input type="checkbox" id="f-tool" checked> 🔧 ツール</label>
  <label><input type="checkbox" id="f-llm" checked> 🤖 LLM</label>
  <label><input type="checkbox" id="f-session" checked> ⏻ セッション</label>
  <label><input type="checkbox" id="follow" checked> ライブ追従</label>
  <span id="status"></span>
</header>
<div id="timeline"></div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
let after = 0;
let timer = null;

const GROUPS = {
  speech_started: "speech", speech_failed: "speech",
  speech_finished: "subtitle", subtitle_cleared: "subtitle",
  tool_call: "tool", llm_call: "llm", turn: "turn",
  session_start: "session", session_end: "session",
  subagent_start: "session", subagent_stop: "session",
};

function enabled(group) {
  if (group === "turn") return $("f-llm").checked;
  const el = $("f-" + group);
  return el ? el.checked : true;
}

function jst(ts) {
  if (!ts) return "--:--:--";
  const d = new Date(ts);
  return d.toLocaleTimeString("ja-JP", { hour12: false });
}

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

const ARG_KEYS = ["command", "cmd", "path", "file_path", "filename", "pattern", "query", "url", "prompt"];
function argDigest(args) {
  if (args == null) return "";
  if (typeof args === "string") return args.slice(0, 120);
  if (typeof args === "object") {
    for (const k of ARG_KEYS) {
      if (typeof args[k] === "string" && args[k].trim()) return args[k].slice(0, 120);
    }
    try { return JSON.stringify(args).slice(0, 120); } catch { return ""; }
  }
  return String(args).slice(0, 120);
}

function describe(e) {
  const p = e.payload || {};
  switch (e.kind) {
    case "speech_started":
      return `<span class="badge">${esc(p.source || "")}</span> 「${esc(p.text_preview || "")}」`;
    case "speech_failed":
      return `<span class="err">失敗: ${esc(p.reason || "")}</span>`;
    case "speech_finished": return `<span class="dim">再生完了</span>`;
    case "subtitle_cleared": return `<span class="dim">字幕クリア</span>`;
    case "tool_call": {
      let s = `<b>${esc(p.tool || "?")}</b> ${esc(argDigest(p.args))}`;
      if (p.status && p.status !== "ok" && p.status !== "success")
        s += ` <span class="err">status=${esc(p.status)}</span>`;
      if (p.error_message) s += ` <span class="err">${esc(String(p.error_message).slice(0, 80))}</span>`;
      if (p.duration_ms >= 1000) s += `<span class="badge">${(p.duration_ms / 1000).toFixed(1)}s</span>`;
      return s;
    }
    case "llm_call": {
      let s = `${esc(p.model || "")}`;
      if (p.duration_ms) s += `<span class="badge">${(p.duration_ms / 1000).toFixed(1)}s</span>`;
      if (p.tool_calls) s += `<span class="badge">tools:${esc(p.tool_calls)}</span>`;
      if (p.finish_reason && p.finish_reason !== "stop")
        s += `<span class="badge">${esc(p.finish_reason)}</span>`;
      return s;
    }
    case "turn":
      return `応答: ${esc(String(p.assistant_response || "").slice(0, 160))}`;
    case "session_start":
      return `セッション開始 <span class="badge">${esc(p.model || "")}</span><span class="badge">${esc(p.platform || "")}</span>`;
    case "session_end":
      return `セッション終了 <span class="badge">${p.completed ? "completed" : (p.interrupted ? "interrupted" : "?")}</span>`;
    default:
      return esc(JSON.stringify(p).slice(0, 160));
  }
}

const ICONS = { speech: "🗣", subtitle: "💬", tool: "🔧", llm: "🤖", turn: "💡", session: "⏻" };

function render(events) {
  const tl = $("timeline");
  const frag = document.createDocumentFragment();
  for (const e of events) {
    const group = GROUPS[e.kind] || "session";
    const row = document.createElement("div");
    row.className = `row ${group === "subtitle" ? "subtitle-ev" : group === "turn" ? "turnrow" : group}`;
    row.dataset.group = group;
    row.style.display = enabled(group) ? "" : "none";
    row.title = e.session_id || "";
    row.innerHTML = `<span class="t">${jst(e.ts)}</span>` +
      `<span class="k">${ICONS[group] || ""} ${esc(e.kind)}</span>` +
      `<span class="b">${describe(e)}</span>`;
    frag.appendChild(row);
  }
  tl.appendChild(frag);
}

function applyFilters() {
  for (const row of document.querySelectorAll("#timeline .row")) {
    row.style.display = enabled(row.dataset.group) ? "" : "none";
  }
}

async function poll(reset) {
  const date = $("date").value;
  if (!date) return;
  if (reset) { after = 0; $("timeline").innerHTML = ""; }
  try {
    const r = await fetch(`/api/events?date=${date}&after=${after}`);
    const data = await r.json();
    after = data.next;
    if (data.events.length) {
      const stick = window.innerHeight + window.scrollY >= document.body.offsetHeight - 60;
      render(data.events);
      if ($("follow").checked && stick) window.scrollTo(0, document.body.scrollHeight);
    }
    $("status").textContent = `${after} events`;
  } catch (err) {
    $("status").textContent = `取得失敗: ${err}`;
  }
}

async function init() {
  const dates = await (await fetch("/api/dates")).json();
  const sel = $("date");
  sel.innerHTML = dates.map((d) => `<option>${d}</option>`).join("");
  if (dates.length) sel.value = dates[dates.length - 1];
  sel.addEventListener("change", () => poll(true));
  for (const id of ["f-speech", "f-subtitle", "f-tool", "f-llm", "f-session"])
    $(id).addEventListener("change", applyFilters);
  await poll(true);
  timer = setInterval(() => { if ($("follow").checked) poll(false); }, 2000);
}
init();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "kai-trace-viewer/0.1"

    def log_message(self, fmt: str, *args: object) -> None:  # 静かに
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: object, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - http.server の規約
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if url.path == "/api/dates":
            self._send_json(list_dates())
            return
        if url.path == "/api/events":
            q = parse_qs(url.query)
            date = (q.get("date") or [""])[0]
            if not _DATE_RE.match(date):
                self._send_json({"error": "invalid date"}, code=400)
                return
            try:
                after = max(0, int((q.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            events, nxt = read_events(date, after)
            self._send_json({"events": events, "next": nxt})
            return
        self._send_json({"error": "not found"}, code=404)


def main() -> None:
    server = ThreadingHTTPServer((BIND, PORT), _Handler)
    print(f"[trace-viewer] listening on http://{BIND}:{PORT} (dir: {TRACE_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    main()

// クライアント/サーバー共用の表示ヘルパー（fs 非依存）。
import type { TraceEvent } from "./trace";

export function fmtTime(ts: string | null): string {
  if (!ts) return "--:--:--";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString("ja-JP", { hour12: false });
}

const ARG_KEYS = ["command", "cmd", "path", "file_path", "filename", "pattern", "query", "url", "prompt"];

export function argDigest(args: unknown): string {
  if (args == null) return "";
  if (typeof args === "string") return args.slice(0, 140);
  if (typeof args === "object") {
    const o = args as Record<string, unknown>;
    for (const k of ARG_KEYS) {
      const v = o[k];
      if (typeof v === "string" && v.trim()) return v.slice(0, 140);
    }
    try {
      return JSON.stringify(o).slice(0, 140);
    } catch {
      return "";
    }
  }
  return String(args).slice(0, 140);
}

export type Side = "ops" | "say" | "full" | "skip";

/** イベントを操作側/実況側に振り分ける（操作 vs 実況の対比用）。 */
export function sideOf(e: TraceEvent): Side {
  switch (e.kind) {
    case "tool_call":
    case "llm_call":
      return "ops";
    case "speech_started":
    case "speech_failed":
      return "say";
    case "session_start":
    case "session_end":
      return "full";
    default:
      return "skip"; // 字幕クリア・turn 等は対比表では出さない
  }
}

export interface Rendered {
  name: string;
  body: string;
  isError: boolean;
  faded: boolean;
}

export function renderEvent(e: TraceEvent): Rendered {
  const p = e.payload || {};
  const s = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
  switch (e.kind) {
    case "tool_call": {
      const dur = typeof p.duration_ms === "number" ? p.duration_ms : 0;
      const status = s(p.status);
      const err = status && status !== "ok" && status !== "success";
      let body = argDigest(p.args);
      if (dur >= 1000) body += `  (${(dur / 1000).toFixed(1)}s)`;
      if (err) body += `  [${status}]`;
      if (p.error_message) body += `  ${s(p.error_message).slice(0, 80)}`;
      return { name: s(p.tool) || "tool", body, isError: !!err || !!p.error_message, faded: false };
    }
    case "llm_call": {
      const dur = typeof p.duration_ms === "number" ? p.duration_ms : 0;
      let body = s(p.model);
      if (dur) body += `  (${(dur / 1000).toFixed(1)}s)`;
      if (p.tool_calls) body += `  tools:${s(p.tool_calls)}`;
      return { name: "LLM", body, isError: false, faded: true };
    }
    case "speech_started":
      return { name: s(p.source) || "say", body: s(p.text_preview), isError: false, faded: false };
    case "speech_failed":
      return { name: "発話失敗", body: s(p.reason), isError: true, faded: false };
    case "session_start":
      return {
        name: "▶ セッション開始",
        body: `${s(p.model)} ${s(p.platform)} ${s(p.prompt) || s(p.task) || ""}`.trim(),
        isError: false,
        faded: false,
      };
    case "session_end":
      return {
        name: "■ セッション終了",
        body: p.completed ? "completed" : p.interrupted ? "interrupted" : "",
        isError: !!p.interrupted,
        faded: false,
      };
    default:
      return { name: s(e.kind), body: "", isError: false, faded: true };
  }
}

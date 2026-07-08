// kai_trace の JSONL を読み、セッション単位に集約する（読み取り専用）。
// データ元: <HERMES_HOME>/kai_trace/YYYY-MM-DD.jsonl（kai_trace plugin と speechd が書く）。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface TraceEvent {
  n: number; // 行番号（1 始まり）。SSE のカーソルに使う
  ts: string | null;
  component: string | null;
  kind: string | null;
  session_id: string | null;
  payload: Record<string, unknown>;
}

export interface SessionSummary {
  id: string;
  startTs: string | null;
  endTs: string | null; // 終了イベントが無ければ null（実行中）
  lastTs: string | null;
  running: boolean;
  model: string;
  platform: string;
  toolCount: number;
  speechCount: number;
  speechFailCount: number;
  llmCount: number;
  firstTask: string; // 最初の指示/タスク（session_start payload か最初の応答から）
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function traceDir(): string {
  if (process.env.TRACE_DIR) return process.env.TRACE_DIR;
  const home = process.env.HERMES_HOME?.trim() || path.join(os.homedir(), ".hermes");
  return path.join(home, "kai_trace");
}

export function isValidDate(d: string): boolean {
  return DATE_RE.test(d);
}

export function listDates(): string[] {
  const dir = traceDir();
  let names: string[] = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return names
    .filter((f) => f.endsWith(".jsonl") && DATE_RE.test(f.slice(0, -6)))
    .map((f) => f.slice(0, -6))
    .sort();
}

/** date の JSONL を読み、行番号 after より後のイベントを返す（after=0 で全件）。 */
export function readEvents(date: string, after = 0): { events: TraceEvent[]; next: number } {
  const file = path.join(traceDir(), `${date}.jsonl`);
  let text: string;
  try {
    text = fs.readFileSync(file, "utf-8");
  } catch {
    return { events: [], next: after };
  }
  const events: TraceEvent[] = [];
  const lines = text.split("\n");
  let n = 0;
  for (const line of lines) {
    if (line === "") continue; // 末尾の空行など
    n += 1;
    if (n <= after) continue;
    let e: Record<string, unknown>;
    try {
      e = JSON.parse(line);
    } catch {
      continue; // 書き込み途中の壊れた行は飛ばす
    }
    events.push({
      n,
      ts: (e.ts as string) ?? null,
      component: (e.component as string) ?? null,
      kind: (e.kind as string) ?? null,
      session_id: (e.session_id as string) ?? null,
      payload: (e.payload as Record<string, unknown>) ?? {},
    });
  }
  return { events, next: Math.max(n, after) };
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/** date のイベントをセッション単位に集約する（新しい順）。 */
export function listSessions(date: string): SessionSummary[] {
  const { events } = readEvents(date, 0);
  const map = new Map<string, SessionSummary>();
  for (const e of events) {
    const id = e.session_id;
    if (!id) continue;
    let s = map.get(id);
    if (!s) {
      s = {
        id,
        startTs: e.ts,
        endTs: null,
        lastTs: e.ts,
        running: true,
        model: "",
        platform: "",
        toolCount: 0,
        speechCount: 0,
        speechFailCount: 0,
        llmCount: 0,
        firstTask: "",
      };
      map.set(id, s);
    }
    if (e.ts) s.lastTs = e.ts;
    const p = e.payload;
    switch (e.kind) {
      case "session_start":
        s.startTs = e.ts ?? s.startTs;
        s.model = str(p.model) || s.model;
        s.platform = str(p.platform) || s.platform;
        if (!s.firstTask) s.firstTask = str(p.prompt) || str(p.task) || str(p.instruction);
        break;
      case "session_end":
        s.endTs = e.ts;
        s.running = false;
        break;
      case "tool_call":
        s.toolCount += 1;
        break;
      case "speech_started":
        s.speechCount += 1;
        if (!s.firstTask) s.firstTask = "";
        break;
      case "speech_failed":
        s.speechFailCount += 1;
        break;
      case "llm_call":
        s.llmCount += 1;
        break;
      case "turn":
        if (!s.firstTask) s.firstTask = str(p.assistant_response).slice(0, 80);
        break;
    }
  }
  return [...map.values()].sort((a, b) => str(b.startTs).localeCompare(str(a.startTs)));
}

/** 特定セッションのイベントだけを返す（after より後・SSE 追従用のカーソル付き）。 */
export function sessionEvents(date: string, id: string, after = 0): { events: TraceEvent[]; next: number } {
  const { events, next } = readEvents(date, after);
  return { events: events.filter((e) => e.session_id === id), next };
}

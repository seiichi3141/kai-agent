"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionSummary } from "@/lib/trace";
import { fmtTime } from "@/lib/format";

export default function Home() {
  const [dates, setDates] = useState<string[]>([]);
  const [date, setDate] = useState<string>("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [live, setLive] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadSessions = useCallback(async (d: string) => {
    if (!d) return;
    try {
      const r = await fetch(`/api/sessions?date=${d}`);
      const data = await r.json();
      setSessions(data.sessions ?? []);
    } catch {
      /* 取得失敗は次回に */
    }
  }, []);

  // 日付一覧
  useEffect(() => {
    fetch("/api/dates")
      .then((r) => r.json())
      .then((ds: string[]) => {
        setDates(ds);
        if (ds.length) setDate(ds[ds.length - 1]);
      })
      .catch(() => undefined);
  }, []);

  // 日付が決まったらセッション取得 + SSE 購読（新イベントで一覧を再取得）
  useEffect(() => {
    if (!date) return;
    void loadSessions(date);
    esRef.current?.close();
    const es = new EventSource(`/api/stream?date=${date}`);
    esRef.current = es;
    es.addEventListener("hello", () => setLive(true));
    es.addEventListener("events", () => {
      if (debounce.current) clearTimeout(debounce.current);
      debounce.current = setTimeout(() => void loadSessions(date), 400);
    });
    es.onerror = () => setLive(false);
    return () => es.close();
  }, [date, loadSessions]);

  return (
    <>
      <header className="top">
        <h1>kai trace</h1>
        <select value={date} onChange={(e) => setDate(e.target.value)}>
          {dates.map((d) => (
            <option key={d}>{d}</option>
          ))}
        </select>
        <span className={`live ${live ? "on" : ""}`}>
          <span className="dot" />
          {live ? "ライブ" : "停止"}
        </span>
        <span className="spacer" />
        <span className="live">{sessions.length} セッション</span>
      </header>
      <main>
        {sessions.length === 0 ? (
          <div className="empty">この日のセッションはありません</div>
        ) : (
          <div className="sessions">
            {sessions.map((s) => (
              <a key={s.id} className="card" href={`/sessions/${s.id}?date=${date}`}>
                <div className="id">
                  {fmtTime(s.startTs)}
                  {s.endTs ? ` – ${fmtTime(s.endTs)}` : ""} · {s.id.slice(0, 20)}
                </div>
                <div className="task">{s.firstTask || "（タスク説明なし）"}</div>
                <div className="meta">
                  {s.running && <span className="pill run">実行中</span>}
                  {s.model && <span className="pill">{s.model}</span>}
                  <span className="pill tool">🔧 {s.toolCount}</span>
                  <span className="pill speech">🗣 {s.speechCount}</span>
                  {s.speechFailCount > 0 && <span className="pill err">失敗 {s.speechFailCount}</span>}
                  <span className="pill">🤖 {s.llmCount}</span>
                </div>
              </a>
            ))}
          </div>
        )}
      </main>
    </>
  );
}

"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { TraceEvent } from "@/lib/trace";
import { fmtTime, renderEvent, sideOf } from "@/lib/format";

function Item({ e }: { e: TraceEvent }) {
  const r = renderEvent(e);
  const cls = e.kind === "tool_call" ? "tool" : e.kind?.startsWith("speech") ? "say" : "llm";
  return (
    <div className={`item ${cls} ${r.isError ? "err" : ""} ${r.faded ? "llm" : ""}`}>
      <span className="t">{fmtTime(e.ts)}</span>
      <span className="body">
        <span className="name">{r.name}</span> {r.body}
      </span>
    </div>
  );
}

function SessionInner({ id }: { id: string }) {
  const date = useSearchParams().get("date") ?? "";
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [live, setLive] = useState(false);
  const afterRef = useRef(0);
  const seen = useRef<Set<number>>(new Set());

  useEffect(() => {
    if (!date || !id) return;
    seen.current = new Set();
    setEvents([]);
    let es: EventSource | null = null;

    (async () => {
      try {
        const r = await fetch(`/api/sessions/${id}?date=${date}&after=0`);
        const data = await r.json();
        const evs: TraceEvent[] = data.events ?? [];
        evs.forEach((e) => seen.current.add(e.n));
        setEvents(evs);
        afterRef.current = data.next ?? 0;
      } catch {
        /* noop */
      }
      // 以降の追記を SSE で追う（このセッションのみ）
      es = new EventSource(`/api/stream?date=${date}&session=${id}&after=${afterRef.current}`);
      es.addEventListener("hello", () => setLive(true));
      es.addEventListener("events", (ev) => {
        const { events: incoming } = JSON.parse((ev as MessageEvent).data) as { events: TraceEvent[] };
        const fresh = incoming.filter((e) => !seen.current.has(e.n));
        if (fresh.length) {
          fresh.forEach((e) => seen.current.add(e.n));
          setEvents((prev) => [...prev, ...fresh]);
        }
      });
      es.onerror = () => setLive(false);
    })();

    return () => es?.close();
  }, [date, id]);

  const shown = events.filter((e) => sideOf(e) !== "skip");

  return (
    <>
      <header className="top">
        <a className="backlink" href="/">
          ← 一覧
        </a>
        <h1>{id.slice(0, 28)}</h1>
        <span className={`live ${live ? "on" : ""}`}>
          <span className="dot" />
          {live ? "ライブ" : "停止"}
        </span>
        <span className="spacer" />
        <span className="live">{date}</span>
      </header>
      <main>
        {shown.length === 0 ? (
          <div className="empty">イベントがありません</div>
        ) : (
          <div className="cmp">
            <div className="heads">
              <div className="h-ops">🔧 操作（ツール・LLM）</div>
              <div className="h-say">🗣 実況（発話）</div>
            </div>
            {shown.map((e) => {
              const side = sideOf(e);
              if (side === "full") {
                return (
                  <div className="full" key={e.n}>
                    <span className="t">{fmtTime(e.ts)}</span>
                    <Item e={e} />
                  </div>
                );
              }
              return (
                <div className="line" key={e.n}>
                  <div className="cell ops">{side === "ops" && <Item e={e} />}</div>
                  <div className="cell say">{side === "say" && <Item e={e} />}</div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </>
  );
}

export default function SessionPage({ params }: { params: { id: string } }) {
  return (
    <Suspense fallback={<div className="empty">読み込み中…</div>}>
      <SessionInner id={params.id} />
    </Suspense>
  );
}

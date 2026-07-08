// SSE: kai_trace JSONL の追記分をリアルタイムに push する（配信中の監視用）。
// クエリ: date=YYYY-MM-DD, after=行番号（この後から）, session=絞り込み（任意）。
// 追記のみのファイルを一定間隔でポーリングし、新規イベントだけを送る。
import { isValidDate, readEvents, type TraceEvent } from "@/lib/trace";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const POLL_MS = 1500;

export function GET(req: Request) {
  const url = new URL(req.url);
  const date = url.searchParams.get("date") ?? "";
  const session = url.searchParams.get("session");
  let after = Math.max(0, parseInt(url.searchParams.get("after") ?? "0", 10) || 0);
  if (!isValidDate(date)) return new Response("invalid date", { status: 400 });

  const encoder = new TextEncoder();
  let timer: ReturnType<typeof setInterval> | null = null;

  const stream = new ReadableStream({
    start(controller) {
      const send = (event: string, data: unknown) => {
        controller.enqueue(
          encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`),
        );
      };
      // 接続直後にハートビート（プロキシのバッファリング対策）
      send("hello", { after });

      const tick = () => {
        try {
          const { events, next } = readEvents(date, after);
          after = next;
          const out: TraceEvent[] = session
            ? events.filter((e) => e.session_id === session)
            : events;
          if (out.length) send("events", { events: out, next });
          else send("ping", { next }); // 生存確認 + カーソル更新
        } catch {
          // 読み取り失敗は次回に回す（ファイル未生成など）
        }
      };
      tick();
      timer = setInterval(tick, POLL_MS);
    },
    cancel() {
      if (timer) clearInterval(timer);
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

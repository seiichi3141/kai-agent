import { NextResponse } from "next/server";
import { isValidDate, sessionEvents } from "@/lib/trace";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET(req: Request, { params }: { params: { id: string } }) {
  const url = new URL(req.url);
  const date = url.searchParams.get("date") ?? "";
  const after = Math.max(0, parseInt(url.searchParams.get("after") ?? "0", 10) || 0);
  if (!isValidDate(date)) return NextResponse.json({ error: "invalid date" }, { status: 400 });
  const { events, next } = sessionEvents(date, params.id, after);
  return NextResponse.json({ events, next });
}

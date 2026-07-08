import { NextResponse } from "next/server";
import { isValidDate, listSessions } from "@/lib/trace";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET(req: Request) {
  const date = new URL(req.url).searchParams.get("date") ?? "";
  if (!isValidDate(date)) return NextResponse.json({ error: "invalid date" }, { status: 400 });
  return NextResponse.json({ sessions: listSessions(date) });
}

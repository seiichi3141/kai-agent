import { NextResponse } from "next/server";
import { listDates } from "@/lib/trace";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET() {
  return NextResponse.json(listDates());
}

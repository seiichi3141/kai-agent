// 全リクエストに TRACE_WEB_TOKEN 認証をかける（Issue #77 H-a）。
// 判定ロジックは lib/auth.ts（テスト済みの純関数）。ここは Next.js の
// Request/Response と Cookie の配線だけを行う。
import { NextRequest, NextResponse } from "next/server";
import { checkAuth } from "@/lib/auth.mjs";

export const config = {
  // 静的アセット・favicon 以外の全パスに適用（API・ページ・SSE・画像を含む）
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export function middleware(req: NextRequest) {
  const expected = process.env.TRACE_WEB_TOKEN ?? "";
  const result = checkAuth({
    expected,
    bearer: req.headers.get("authorization"),
    cookie: req.cookies.get("trace_token")?.value ?? null,
    query: req.nextUrl.searchParams.get("token"),
  });

  if (!result.authorized) {
    return new NextResponse("unauthorized", { status: 401 });
  }
  const res = NextResponse.next();
  if (result.setCookie) {
    res.cookies.set("trace_token", result.setCookie, {
      httpOnly: true,
      sameSite: "strict",
      maxAge: 60 * 60 * 24 * 30, // 30 日
    });
  }
  return res;
}

// trace-web の認証判定（Issue #77 H-a）。
//
// trace-web は生に近い作業ログ（cat の結果・diff・エラー本文）とスクショを配信する
// ため、tailnet/LAN 上の他端末に素で見せてはならない。TRACE_WEB_TOKEN を設定すると
// 全リクエストにトークンを要求する（未設定ならローカル開発向けに素通し）。
//
// トークンの受け渡し（ブラウザ閲覧を実用的にする）:
//   1. 初回は `?token=<値>` を URL に付けてアクセス
//   2. middleware が一致を確認したら Cookie `trace_token` にセットし、以後は省略可
//   3. API/fetch からは `Authorization: Bearer <値>` でも可
//
// 純 JS（.mjs）にして next 非依存のユニットテスト（node --test）を可能にする。
// middleware.ts はここを import する。

/**
 * 定数時間の文字列一致（タイミング攻撃を避ける。長さ不一致は即 false）。
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
export function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * トークン検証。expected が空なら常に許可（認証無効）。
 * @param {{expected: string, bearer?: string|null, cookie?: string|null, query?: string|null}} input
 * @returns {{authorized: boolean, setCookie: string|null}}
 */
export function checkAuth({ expected, bearer, cookie, query }) {
  if (!expected) return { authorized: true, setCookie: null };

  const fromBearer = (bearer ?? "").replace(/^Bearer\s+/i, "");
  if (fromBearer && safeEqual(fromBearer, expected)) {
    return { authorized: true, setCookie: null };
  }
  if (cookie && safeEqual(cookie, expected)) {
    return { authorized: true, setCookie: null };
  }
  if (query && safeEqual(query, expected)) {
    // 次回から省略できるよう Cookie に焼く
    return { authorized: true, setCookie: expected };
  }
  return { authorized: false, setCookie: null };
}

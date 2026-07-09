import { test } from "node:test";
import assert from "node:assert/strict";
import { checkAuth, safeEqual } from "./auth.mjs";

test("expected が空なら認証無効（常に許可）", () => {
  assert.deepEqual(checkAuth({ expected: "" }), { authorized: true, setCookie: null });
});

test("Bearer トークン一致で許可（Cookie は焼かない）", () => {
  const r = checkAuth({ expected: "tok123", bearer: "Bearer tok123" });
  assert.deepEqual(r, { authorized: true, setCookie: null });
});

test("Cookie トークン一致で許可", () => {
  const r = checkAuth({ expected: "tok123", cookie: "tok123" });
  assert.deepEqual(r, { authorized: true, setCookie: null });
});

test("クエリトークン一致で許可し Cookie に焼く", () => {
  const r = checkAuth({ expected: "tok123", query: "tok123" });
  assert.deepEqual(r, { authorized: true, setCookie: "tok123" });
});

test("不一致・未提示は拒否", () => {
  assert.equal(checkAuth({ expected: "tok123", bearer: "Bearer wrong" }).authorized, false);
  assert.equal(checkAuth({ expected: "tok123", cookie: "wrong" }).authorized, false);
  assert.equal(checkAuth({ expected: "tok123" }).authorized, false);
});

test("safeEqual: 長さ・内容の一致", () => {
  assert.equal(safeEqual("abc", "abc"), true);
  assert.equal(safeEqual("abc", "abd"), false);
  assert.equal(safeEqual("abc", "ab"), false);
});

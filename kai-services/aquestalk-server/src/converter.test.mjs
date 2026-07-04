import { test } from "node:test";
import assert from "node:assert/strict";
import { initTokenizer, toKoe, preprocessSymbols, formatKoe } from "./converter.mjs";
import { splitSentences } from "./text-splitter.mjs";

// kuromoji の辞書読み込みには数秒かかることがあるため、テスト全体を待ってから実行する。
await initTokenizer();

test("preprocessSymbols: 技術用語を日本語読みに置換する", () => {
  const result = preprocessSymbols("Issue #42 を確認");
  assert.match(result, /いっしゅー/);
  assert.match(result, /<NUMK VAL=42>/);
});

test("preprocessSymbols: 空文字はそのまま空文字を返す", () => {
  assert.equal(preprocessSymbols(""), "");
});

test("formatKoe: 空文字は句点のみを返す", () => {
  assert.equal(formatKoe(""), "。");
});

test("formatKoe: カタカナをひらがなに変換し文末に句点を付与する", () => {
  assert.equal(formatKoe("コンニチハ"), "こんにちは。");
});

test("formatKoe: 既に句点・疑問符で終わる場合は追加しない", () => {
  assert.equal(formatKoe("ソウデスカ？"), "そうですか？");
  assert.equal(formatKoe("ソウデス。"), "そうです。");
});

test("toKoe: ひらがな入力をそのまま音声記号列に変換する", async () => {
  const koe = await toKoe("こんにちは");
  assert.equal(koe, "こんにちは。");
});

test("toKoe: 技術用語を含むテキストを変換する", async () => {
  const koe = await toKoe("Issue #42 の実装を開始します");
  assert.match(koe, /いっしゅー/);
  assert.match(koe, /<NUMK VAL=42>/);
  assert.ok(koe.endsWith("。"));
});

test("toKoe: 空文字は空文字を返す", async () => {
  assert.equal(await toKoe(""), "");
});

test("splitSentences: 句点・感嘆符・疑問符・改行で分割する", () => {
  const sentences = splitSentences("こんにちは。元気ですか？さようなら！\nまた明日。");
  assert.deepEqual(sentences, ["こんにちは。", "元気ですか？", "さようなら！", "また明日。"]);
});

test("splitSentences: 空文字や空白のみの入力は空配列を返す", () => {
  assert.deepEqual(splitSentences(""), []);
  assert.deepEqual(splitSentences("   "), []);
});

test("splitSentences: 句切記号のないテキストは1文として返す", () => {
  assert.deepEqual(splitSentences("句点なしテキスト"), ["句点なしテキスト"]);
});

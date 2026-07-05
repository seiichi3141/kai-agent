import { test } from "node:test";
import assert from "node:assert/strict";
import {
  sanitizeLlmKoe,
  ensureSentenceEnd,
  buildTermsSection,
  buildSystemPrompt,
  generateKoeLlm,
} from "./koe-llm.mjs";
import { validateKoe } from "./koe-validate.mjs";

// ---------------------------------------------------------------------------
// sanitizeLlmKoe（旧プロジェクトの主要ケースを移植・適応）
// ---------------------------------------------------------------------------

test("sanitizeLlmKoe: 空白（半角・全角）を除去する", () => {
  assert.equal(sanitizeLlmKoe("てすと ぜんつうか　です。"), "てすとぜんつうかです。");
});

test("sanitizeLlmKoe: / と ; は保持する（単語区切り・強調は正典 §4.1 の有効記号）", () => {
  // 旧実装は ; を除去していたが、設計書 §4.1 の正典化で保持に変更
  assert.equal(sanitizeLlmKoe("てすと/ぜんつうか;かくにん。"), "てすと/ぜんつうか;かくにん。");
});

test("sanitizeLlmKoe: _ ` { } * + を除去する", () => {
  assert.equal(sanitizeLlmKoe("てすと_かんりょう+です*。"), "てすとかんりょうです。");
});

test("sanitizeLlmKoe: ぢ・づ を じ・ず に置換する", () => {
  assert.equal(sanitizeLlmKoe("ちぢみとひづけ。"), "ちじみとひずけ。");
});

test("sanitizeLlmKoe: 促音の不正な並びを直す", () => {
  assert.equal(sanitizeLlmKoe("いっったー。"), "いったー。");
  assert.equal(sanitizeLlmKoe("あっー。"), "あっ。".replace("っ。", "。"));
  assert.equal(sanitizeLlmKoe("まって、"), "まって、");
});

test("sanitizeLlmKoe: 長音の不正な位置を直す", () => {
  assert.equal(sanitizeLlmKoe("ーてすとーー。"), "てすとー。");
  assert.equal(sanitizeLlmKoe("かんりょう。ーつぎへ。"), "かんりょう。つぎへ。");
  assert.equal(sanitizeLlmKoe("たんご/ーくぎり。"), "たんご/くぎり。");
});

test("sanitizeLlmKoe: 括弧・鉤括弧は内容ごと除去する", () => {
  assert.equal(sanitizeLlmKoe("てすと（ちゅうしゃく）かんりょう。"), "てすとかんりょう。");
  assert.equal(sanitizeLlmKoe("「いんよう」ですね。"), "ですね。");
});

test("sanitizeLlmKoe: 感嘆符は句点に、コロン・ハイフンは除去する", () => {
  assert.equal(sanitizeLlmKoe("できた！"), "できた。");
  assert.equal(sanitizeLlmKoe("じかん:てすと-けっか。"), "じかんてすとけっか。");
});

test("sanitizeLlmKoe: ALPHA タグの値を正規化し、空になったらタグごと削除する", () => {
  assert.equal(sanitizeLlmKoe("<ALPHA VAL=pr>ばんごう。"), "<ALPHA VAL=PR>ばんごう。");
  assert.equal(sanitizeLlmKoe("<ALPHA VAL=PR#609>です。"), "<ALPHA VAL=PR609>です。");
  assert.equal(sanitizeLlmKoe("<ALPHA VAL=、、>です。"), "です。");
});

test("sanitizeLlmKoe: 閉じられていない不完全タグを除去する", () => {
  assert.equal(sanitizeLlmKoe("すうじわ<NUMK VAL=42"), "すうじわ");
});

test("sanitizeLlmKoe: タグ内の値は変換しない", () => {
  assert.equal(sanitizeLlmKoe("<NUMK VAL=42>この<ALPHA VAL=CI>。"), "<NUMK VAL=42>この<ALPHA VAL=CI>。");
});

test("sanitizeLlmKoe: AquesTalk10 非対応の Unicode 記号を除去する", () => {
  assert.equal(sanitizeLlmKoe("かんりょう✓です★。"), "かんりょうです。");
});

test("sanitizeLlmKoe: ゔ・ぐ行拗音を代替する", () => {
  assert.equal(sanitizeLlmKoe("ゔぁいおりんとぐぃたー。"), "ばいおりんとぎたー。");
});

test("sanitizeLlmKoe: を は お に確定させる（LLM の指示取りこぼし対策）", () => {
  assert.equal(sanitizeLlmKoe("まーじを/まちます。"), "まーじお/まちます。");
});

test("sanitizeLlmKoe: 文頭の句切記号を除去する（LLM が出力しがち）", () => {
  assert.equal(sanitizeLlmKoe("/ゆーちゅーぶの/がめん。"), "ゆーちゅーぶの/がめん。");
});

test("ensureSentenceEnd: 文末に句切記号がなければ。を付ける", () => {
  assert.equal(ensureSentenceEnd("てすと"), "てすと。");
  assert.equal(ensureSentenceEnd("てすと。"), "てすと。");
  assert.equal(ensureSentenceEnd("いいの？"), "いいの？");
  assert.equal(ensureSentenceEnd("  "), "");
});

// ---------------------------------------------------------------------------
// プロンプト構築
// ---------------------------------------------------------------------------

test("buildTermsSection: テキストに含まれる例外辞書語だけを列挙する", () => {
  const terms = { kai: "かい", OBS: "おーびーえす" };
  const section = buildTermsSection("kai の配信です", terms);
  assert.match(section, /kai → かい/);
  assert.doesNotMatch(section, /OBS/);
  assert.equal(buildTermsSection("辞書語なし", terms), "");
});

test("buildSystemPrompt: 人物設定（kai=かい）と TERMS_SECTION を含む", () => {
  const prompt = buildSystemPrompt({ text: "kai です", terms: { kai: "かい" } });
  assert.match(prompt, /kai は配信者の名前で「かい」と読む/);
  assert.match(prompt, /この発話に含まれる語の読み/);
  // 該当語がなければ節ごと消える
  const noTerms = buildSystemPrompt({ text: "こんにちは", terms: { kai: "かい" } });
  assert.doesNotMatch(noTerms, /この発話に含まれる語の読み/);
});

// ---------------------------------------------------------------------------
// generateKoeLlm（fetch モック）
// ---------------------------------------------------------------------------

function mockFetch(content, { status = 200 } = {}) {
  return async () => ({
    ok: status === 200,
    status,
    json: async () => ({ choices: [{ message: { content } }] }),
  });
}

const LLM_CONFIG = { baseUrl: "http://mock", model: "test", timeoutMs: 1000 };

test("generateKoeLlm: LLM の応答テキストを返す", async () => {
  const raw = await generateKoeLlm("テスト", { ...LLM_CONFIG, fetchImpl: mockFetch("てすと。") });
  assert.equal(raw, "てすと。");
});

test("generateKoeLlm: HTTP エラー・空応答は例外を投げる（フォールバック契機）", async () => {
  await assert.rejects(
    generateKoeLlm("テスト", { ...LLM_CONFIG, fetchImpl: mockFetch("", { status: 500 }) }),
    /LLM HTTP 500/,
  );
  await assert.rejects(
    generateKoeLlm("テスト", { ...LLM_CONFIG, fetchImpl: mockFetch("") }),
    /LLM 応答が空/,
  );
});

test("generateKoeLlm: sanitize + validate の統合で正常系が通る", async () => {
  const raw = await generateKoeLlm("テスト", {
    ...LLM_CONFIG,
    fetchImpl: mockFetch("てすと/ぜんつうか、もんだいなし。"),
  });
  const koe = sanitizeLlmKoe(raw);
  assert.deepEqual(validateKoe(koe), []);
});

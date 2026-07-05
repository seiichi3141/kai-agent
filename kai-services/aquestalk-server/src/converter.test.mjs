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

test("toKoe: 挨拶の感動詞は末尾の「は」を「わ」に読み分ける（品詞の汎用規則）", async () => {
  // 仕様変更（Issue #7 / design/tts-reading-rules.md §4.3-2）:
  // 感動詞で「は」で終わる語は末尾ハ→ワ。個別語の辞書ではなく品詞規則で行う
  assert.equal(await toKoe("こんにちは"), "こんにちわ。");
  // 汎用性の証明: こんばんは は辞書にもコードにも列挙されていないが同じ規則で通る
  assert.equal(await toKoe("こんばんは"), "こんばんわ。");
});

test("toKoe: 単独の接続詞「では」「それでは」は「でわ」と読む", async () => {
  assert.match(await toKoe("では、始めます"), /^でわ、/);
  assert.match(await toKoe("それでは次に進みます"), /^それでわ/);
});

test("toKoe: 係助詞「は」の既存挙動が回帰していない", async () => {
  assert.match(await toKoe("今日はいい天気です"), /きょうわ/);
  assert.match(await toKoe("駅では電車を待つ"), /えきでわ/);
});

test("toKoe: 格助詞「へ」は「え」、助詞「を」は「お」と読む", async () => {
  // 語中の「へ」（部屋）は変化せず、格助詞の「へ」だけが「え」になる
  assert.equal(await toKoe("部屋へ行く"), "へやえいく。");
  assert.equal(await toKoe("本を読む"), "ほんおよむ。");
});

test("toKoe: 例外辞書の kai はどの表記でも「かい」と読む（スペルアウトしない）", async () => {
  // 第 1 回リハーサルで「けーえーあい」を観測 → 例外辞書に登録（§4.4）
  for (const variant of ["kai", "Kai", "KAI"]) {
    const koe = await toKoe(`${variant} です`);
    assert.match(koe, /かい/, `${variant} が「かい」と読まれること`);
    assert.doesNotMatch(koe, /<ALPHA/, `${variant} がスペルアウトに落ちないこと`);
  }
});

test("toKoe: 2 文字以下の頭字語エントリは一般語に誤マッチしない（照合ガード）", async () => {
  // AI（えーあい）が air の一部に適用されてはいけない
  const koe = await toKoe("air を確認");
  assert.doesNotMatch(koe, /えーあい/);
  assert.match(koe, /<ALPHA VAL=AIR>/);
});

test("toKoe: 読点のない長文は接続助詞の直後に読点を補う（息継ぎ）", async () => {
  const koe = await toKoe("今日はとても天気が良いので散歩に出かけて公園で休みます");
  assert.match(koe, /ので、/);
  assert.match(koe, /でかけて、/);
});

test("toKoe: 短い文には読点を補わない", async () => {
  assert.equal(await toKoe("散歩に出かけて休む"), "さんぽにでかけてやすむ。");
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

test("formatKoe: づ・ぢを AquesTalk10 が解釈できる ず・じ に正規化する", () => {
  // AquesTalk10 の音声記号列では づ・ぢ が未定義で合成失敗する（実機確認済み）
  assert.equal(formatKoe("ヒヅケ"), "ひずけ。");
  assert.equal(formatKoe("チヂミ"), "ちじみ。");
});

test("formatKoe: ゔ（ヴ）はば行で代替する", () => {
  // ゔ は AquesTalk10 未対応。従来は最終 allowlist を素通りして合成失敗していた
  assert.equal(formatKoe("ヴァイオリン"), "ばいおりん。");
  assert.equal(formatKoe("レヴュー"), "れびゅー。");
  assert.equal(formatKoe("ラヴ"), "らぶ。");
});

test("formatKoe: ぐ行拗音は直音で代替する", () => {
  assert.equal(formatKoe("グィード"), "ぎーど。");
});

test("formatKoe: ハイフン類は読点（短ポーズ）に正規化する", () => {
  // 素通しすると合成失敗する（実機確認済み）
  assert.equal(formatKoe("テスト-テスト"), "てすと、てすと。");
  assert.equal(formatKoe("テスト–テスト"), "てすと、てすと。");
});

test("formatKoe: 読めずに残った漢字・ASCII 残渣は除去して発話を継続する", () => {
  // 許可リスト方式の最終サニタイズ。文まるごと合成失敗より一部欠落を取る
  assert.equal(formatKoe("漢テスト"), "てすと。");
  assert.equal(formatKoe("テストabc"), "てすと。");
});

test("toKoe: 英単語と数字はタグとして保持される（除去しない）", async () => {
  const koe = await toKoe("uname -a を 7 秒で実行");
  assert.match(koe, /<ALPHA VAL=UNAME>/);
  assert.match(koe, /<ALPHA VAL=A>/);
  assert.match(koe, /<NUMK VAL=7>/);
  assert.doesNotMatch(koe, /-/); // ハイフンは読点化され残らない
});

// ---------------------------------------------------------------------------
// LLM 主経路（設計書 §5.2 — モックで検証。実 LLM は使わない）
// ---------------------------------------------------------------------------

function mockLlm(content, { status = 200, fail = false } = {}) {
  return {
    baseUrl: "http://mock",
    model: "test",
    timeoutMs: 1000,
    fetchImpl: async () => {
      if (fail) throw new Error("connection refused");
      return {
        ok: status === 200,
        status,
        json: async () => ({ choices: [{ message: { content } }] }),
      };
    },
  };
}

test("toKoe: LLM が正常な koe を返せばそれを採用する", async () => {
  const koe = await toKoe("テスト全通過です", { llm: mockLlm("てすと/ぜんつうか/です。") });
  assert.equal(koe, "てすと/ぜんつうか/です。");
});

test("toKoe: LLM 出力の係助詞「は」はプロンプト指示が漏れても安全網で補正される", async () => {
  const koe = await toKoe("今日は晴れ", { llm: mockLlm("きょうは/はれ。") });
  assert.equal(koe, "きょうわ/はれ。");
});

test("toKoe: LLM 出力の感動詞（こんにちは等）も安全網で補正される", async () => {
  const koe = await toKoe("こんにちは、テスト", { llm: mockLlm("こんにちは、てすと。") });
  assert.equal(koe, "こんにちわ、てすと。");
});

test("toKoe: LLM 出力がバリデーション違反ならルールベースへフォールバックする", async () => {
  // 漢字が残っている = 合成に失敗する出力
  const koe = await toKoe("こんにちは", { llm: mockLlm("こんにちわ、漢字が残った。") });
  assert.equal(koe, "こんにちわ。"); // ルールベース経路の出力
});

test("toKoe: LLM 不達（接続失敗）ならルールベースへフォールバックする", async () => {
  const koe = await toKoe("こんにちは", { llm: mockLlm("", { fail: true }) });
  assert.equal(koe, "こんにちわ。");
});

test("toKoe: 文末の句切記号がない LLM 出力には。を補う", async () => {
  const koe = await toKoe("テスト", { llm: mockLlm("てすと") });
  assert.equal(koe, "てすと。");
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

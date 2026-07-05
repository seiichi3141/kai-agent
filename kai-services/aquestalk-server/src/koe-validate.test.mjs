import { test } from "node:test";
import assert from "node:assert/strict";
import { validateKoe } from "./koe-validate.mjs";

test("validateKoe: 正常な koe（タグ・区切り付き）は違反なし", () => {
  assert.deepEqual(validateKoe("てすと/ぜんつうか、りんとも/もんだいなし。"), []);
  assert.deepEqual(validateKoe("ぴーあーる<NUMK VAL=651>の/かいしょう。"), []);
  assert.deepEqual(validateKoe("<ALPHA VAL=CI>が/みどり；？".replace("；", ";")), []);
});

test("validateKoe: 禁止項目をそれぞれ検出する", () => {
  const cases = [
    ["「いんよう」です。", /括弧/],
    ["abc です。", /タグ外に半角英字/],
    ["ＡＢＣです。", /全角英字/],
    ["漢字です。", /漢字/],
    ["カタカナです。", /タグ外にカタカナ/],
    ["42 です。", /タグ外に半角数字/],
    ["ゔぁいおりん。", /ゔ/],
    ["できた！", /感嘆符/],
    ["じかん:です。", /コロン/],
    ["てすと-けっか。", /ハイフン/],
  ];
  for (const [koe, expected] of cases) {
    const issues = validateKoe(koe);
    assert.ok(
      issues.some((i) => expected.test(i)),
      `${koe} で ${expected} が検出されること（実際: ${JSON.stringify(issues)}）`,
    );
  }
});

test("validateKoe: タグ内の英数字は違反にしない", () => {
  assert.deepEqual(validateKoe("<NUMK VAL=42>と<ALPHA VAL=PR609>。"), []);
});

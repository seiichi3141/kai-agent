/**
 * LLM koe 生成の実機評価ハーネス（設計書 §9）。
 *
 * 実 LLM（KOE_LLM_BASE_URL）に配信想定のテキストを投げ、
 * sanitize → validate → 実 AquesTalk CLI 合成まで通して
 * 成功率とレイテンシを測る。プロンプト改善の判断材料にする。
 *
 * 使い方（Mac、aquestalk-server ディレクトリで）:
 *   KOE_LLM_BASE_URL=http://100.98.225.44:8080/v1 node src/eval-koe-llm.mjs [texts.json]
 *   texts.json は文字列配列。省略時は内蔵サンプル（リハーサルで問題になった文型）。
 */

import { readFileSync } from "node:fs";
import { generateKoeLlm, sanitizeLlmKoe, ensureSentenceEnd } from "./koe-llm.mjs";
import { validateKoe } from "./koe-validate.mjs";
import { synthesize } from "./aquestalk.mjs";
import { toKoeRuleBased } from "./converter.mjs";

try {
  process.loadEnvFile();
} catch {
  // .env が無ければ環境変数がそのまま使われる
}

const SAMPLES = [
  "こんにちは、kai です。今日はライブ配信で Issue を実装します",
  "では broadcast.sh の status コマンドを修正します",
  "テストが全部通ったので、PR #6 を作成して CI の結果を待ちます",
  "obs-websocket の接続でエラーが出たため、リトライ処理を追加しました",
  "verify.sh が緑になりました。git push して merge を待ちます",
  "次は speechd の字幕キューを確認して、overlay の表示を直します",
  "YouTube の配信画面に字幕が乗っているか、スクリーンショットで確認しますね",
  "うまくいった！これで読み上げの品質がだいぶ良くなるはず",
];

const baseUrl = process.env.KOE_LLM_BASE_URL;
if (!baseUrl) {
  console.error("KOE_LLM_BASE_URL を設定してください");
  process.exit(2);
}
const config = {
  baseUrl,
  model: process.env.KOE_LLM_MODEL ?? "qwen3.6-35b-a3b",
  timeoutMs: Number(process.env.KOE_LLM_TIMEOUT_MS ?? 10000), // 評価時は長めに取り実レイテンシを観測する
  promptVersion: process.env.KOE_PROMPT_VERSION ?? "v2",
  terms: JSON.parse(
    readFileSync(new URL("./technical-terms.json", import.meta.url), "utf-8"),
  ),
};

const texts = process.argv[2] ? JSON.parse(readFileSync(process.argv[2], "utf-8")) : SAMPLES;

let llmOk = 0;
let validOk = 0;
let synthOk = 0;
const latencies = [];

for (const text of texts) {
  const row = { text };
  try {
    const referenceKana = await toKoeRuleBased(text);
    const t0 = performance.now();
    const raw = await generateKoeLlm(text, { ...config, referenceKana });
    row.latencyMs = Math.round(performance.now() - t0);
    latencies.push(row.latencyMs);
    llmOk++;
    row.koe = ensureSentenceEnd(sanitizeLlmKoe(raw));
    row.issues = validateKoe(row.koe);
    if (row.issues.length === 0) {
      validOk++;
      try {
        const wav = await synthesize(row.koe, { voice: "F1", speed: 120 });
        row.wavBytes = wav.length;
        synthOk++;
      } catch (err) {
        row.synthError = String(err.message ?? err);
      }
    }
  } catch (err) {
    row.llmError = String(err.message ?? err);
  }
  console.log(JSON.stringify(row, null, 0));
}

const sorted = [...latencies].sort((a, b) => a - b);
const p50 = sorted[Math.floor(sorted.length / 2)] ?? null;
console.log(
  JSON.stringify({
    summary: {
      total: texts.length,
      llmOk,
      validOk,
      synthOk,
      latencyMsP50: p50,
      latencyMsMax: sorted.at(-1) ?? null,
    },
  }),
);
